#!/usr/bin/env python3
"""
app.py
Daemon that streams Pi health events to Supabase.
"""

import dbus
import dbus.mainloop.glib
import hashlib
import json
import logging
import threading
import time
from gi.repository import GLib
from typing import Optional

from config import POLL_INTERVAL
from monitors import RaspotifyMonitor
from postgrest import PostgRESTClient


# ─── Bluetooth event watcher ──────────────────────────────────────────────────

BLUEZ_SERVICE        = "org.bluez"
BLUEZ_DEVICE_IFACE   = "org.bluez.Device1"
BLUEZ_BATTERY_IFACE  = "org.bluez.Battery1"
DBUS_PROPS_IFACE     = "org.freedesktop.DBus.Properties"
DBUS_OBJMANAGER_IFACE = "org.freedesktop.DBus.ObjectManager"

# Audio UUID presence indicates this is an audio device
AUDIO_UUIDS = {
    "0000110b-0000-1000-8000-00805f9b34fb",  # Audio Sink
    "0000110a-0000-1000-8000-00805f9b34fb",  # Audio Source
    "00001108-0000-1000-8000-00805f9b34fb",  # Headset
    "0000111e-0000-1000-8000-00805f9b34fb",  # Handsfree
    "0000110e-0000-1000-8000-00805f9b34fb",  # A/V Remote Control
}


class BluetoothEventWatcher:
    """
    Subscribes to BlueZ D-Bus PropertiesChanged signals and pushes
    connect/disconnect/battery events in real time.
    Runs a GLib main loop on a dedicated daemon thread.
    """

    def __init__(self, client: PostgRESTClient) -> None:
        self._client = client

    def _is_audio_device(self, bus: dbus.SystemBus, path: str) -> bool:
        try:
            props = dbus.Interface(bus.get_object(BLUEZ_SERVICE, path), DBUS_PROPS_IFACE)
            uuids = props.Get(BLUEZ_DEVICE_IFACE, "UUIDs")
            return bool(AUDIO_UUIDS & {str(u).lower() for u in uuids})
        except Exception:
            return False

    def _get_name(self, bus: dbus.SystemBus, path: str) -> str:
        try:
            props = dbus.Interface(bus.get_object(BLUEZ_SERVICE, path), DBUS_PROPS_IFACE)
            return str(props.Get(BLUEZ_DEVICE_IFACE, "Name"))
        except Exception:
            return path.split("/")[-1]

    def _on_properties_changed(self, interface, changed, invalidated, path, bus):
        if interface == BLUEZ_DEVICE_IFACE:
            if "Connected" in changed:
                if not self._is_audio_device(bus, path):
                    return
                name = self._get_name(bus, path)
                mac  = name_from_path(path)
                if bool(changed["Connected"]):
                    self._client.push_event("bluetooth_connected", {"mac": mac, "name": name})
                    logging.info("BT connected: %s (%s)", name, mac)
                else:
                    self._client.push_event("bluetooth_disconnected", {"mac": mac, "name": name})
                    logging.info("BT disconnected: %s (%s)", name, mac)

        elif interface == BLUEZ_BATTERY_IFACE:
            if "Percentage" in changed:
                pct  = int(changed["Percentage"])
                mac  = name_from_path(path)
                name = self._get_name(bus, path)
                self._client.push_event("bluetooth_battery", {"mac": mac, "name": name, "battery": pct})
                logging.info("BT battery: %s (%s) %d%%", name, mac, pct)

    def run(self) -> None:
        bus = dbus.SystemBus()

        bus.add_signal_receiver(
            lambda iface, changed, inv, path: self._on_properties_changed(iface, changed, inv, path, bus),
            signal_name="PropertiesChanged",
            dbus_interface=DBUS_PROPS_IFACE,
            path_keyword="path",
        )

        logging.info("bluetooth D-Bus watcher started")
        GLib.MainLoop().run()


def name_from_path(path: str) -> str:
    """Extracts and reformats a MAC address from a BlueZ D-Bus object path.
    e.g. /org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF -> AA:BB:CC:DD:EE:FF
    """
    return path.split("/")[-1].replace("dev_", "").replace("_", ":")


# ─── Stats poller ─────────────────────────────────────────────────────────────

class StatsPoller:
    """Polls raspotify state and pushes events on change. System metrics are
    handled by node_exporter and excluded here to avoid duplication."""

    def __init__(self, client: PostgRESTClient, interval: int = POLL_INTERVAL) -> None:
        self._client = client
        self._interval = interval
        self._rsp_mon = RaspotifyMonitor()
        self._last_hash: Optional[str] = self._current_hash()

    def _current_hash(self) -> str:
        rsp_stats = self._rsp_mon.get_stats()
        state_signature = {
            "sink_state":     rsp_stats.sink_state,
            "last_error":     rsp_stats.last_error,
            "last_exit_code": rsp_stats.service.last_exit_code,
        }
        return hashlib.md5(json.dumps(state_signature, sort_keys=True).encode()).hexdigest()

    def run(self) -> None:
        while True:
            try:
                self._poll()
            except Exception as e:
                logging.error("stats poll failed: %s", e)
            time.sleep(self._interval)

    def _poll(self) -> None:
        rsp_stats = self._rsp_mon.get_stats()

        state_signature = {
            "sink_state":     rsp_stats.sink_state,
            "last_error":     rsp_stats.last_error,
            "last_exit_code": rsp_stats.service.last_exit_code,
        }

        digest = hashlib.md5(json.dumps(state_signature, sort_keys=True).encode()).hexdigest()
        if digest == self._last_hash:
            logging.debug("status unchanged, skipping push")
            return

        self._client.push_event("status_snapshot", state_signature)
        self._last_hash = digest


# ─── Entry point ──────────────────────────────────────────────────────────────

def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    # Must be called on the main thread before any threads are started
    dbus.mainloop.glib.threads_init()
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

    client = PostgRESTClient()

    bt_thread = threading.Thread(
        target=BluetoothEventWatcher(client).run, daemon=True, name="bt-watcher"
    )
    bt_thread.start()

    StatsPoller(client).run()


if __name__ == "__main__":
    main()
