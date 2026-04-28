#!/usr/bin/env python3
"""
app.py
Daemon that streams Pi health events to Supabase.
"""

import hashlib
import json
import logging
import re
import subprocess
import threading
import time
from typing import Optional

from config import POLL_INTERVAL
from monitors import BluetoothMonitor, RaspotifyMonitor, SystemMonitor, run
from supabase import SupabaseClient


# ─── Bluetooth event watcher ──────────────────────────────────────────────────

class BluetoothEventWatcher:
    """
    Streams bluetoothctl in monitor mode and pushes connect/disconnect/battery
    events to Supabase in real time.
    """

    _CONNECTED_RE = re.compile(r"\[CHG\] Device ([0-9A-Fa-f:]{17}) Connected: (yes|no)")
    _BATTERY_RE   = re.compile(r"\[CHG\] Device ([0-9A-Fa-f:]{17}) Battery Percentage: 0x[0-9a-f]+ \((\d+)\)")

    def __init__(self, client: SupabaseClient) -> None:
        self._client = client
        self._name_cache: dict[str, str] = {}

    def _resolve_name(self, mac: str) -> str:
        if mac in self._name_cache:
            return self._name_cache[mac]
        out, code = run(f"bluetoothctl info {mac}")
        if code == 0:
            for line in out.splitlines():
                if "Name:" in line:
                    name = line.split(":", 1)[1].strip()
                    self._name_cache[mac] = name
                    return name
        return mac

    def run(self) -> None:
        while True:
            try:
                self._watch()
            except Exception as e:
                logging.error("bluetooth watcher crashed: %s", e)
            time.sleep(5)

    def _watch(self) -> None:
        proc = subprocess.Popen(
            ["bluetoothctl"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        try:
            for line in proc.stdout:
                line = line.strip()

                m = self._CONNECTED_RE.search(line)
                if m:
                    mac, state = m.group(1), m.group(2)
                    name = self._resolve_name(mac)
                    event = "bluetooth_connected" if state == "yes" else "bluetooth_disconnected"
                    self._client.push_event(event, {"mac": mac, "name": name})
                    continue

                m = self._BATTERY_RE.search(line)
                if m:
                    mac, pct = m.group(1), int(m.group(2))
                    name = self._resolve_name(mac)
                    self._client.push_event("bluetooth_battery", {"mac": mac, "name": name, "battery": pct})
        finally:
            proc.terminate()
            proc.wait()


# ─── Stats poller ─────────────────────────────────────────────────────────────

class StatsPoller:
    """Collects system and raspotify stats on an interval and pushes them as events."""

    def __init__(self, client: SupabaseClient, interval: int = POLL_INTERVAL) -> None:
        self._client = client
        self._interval = interval
        self._sys_mon = SystemMonitor()
        self._rsp_mon = RaspotifyMonitor()
        self._last_hash: Optional[str] = None

    def run(self) -> None:
        while True:
            try:
                self._poll()
            except Exception as e:
                logging.error("stats poll failed: %s", e)
            time.sleep(self._interval)

    def _poll(self) -> None:
        sys_stats = self._sys_mon.get_stats()
        rsp_stats = self._rsp_mon.get_stats()
        svc = rsp_stats.service

        # Hash only stable state fields — noisy metrics (cpu, temp, memory)
        # don't trigger a push on their own but ride along when state changes.
        state_signature = {
            "system_throttled":             sys_stats.throttled,
            "raspotify_active":             svc.active,
            "raspotify_state":              svc.state,
            "raspotify_restart_count":      svc.restart_count,
            "raspotify_sink_state":         rsp_stats.sink_state,
            "raspotify_currently_playing":  rsp_stats.currently_playing,
            "raspotify_last_error":         rsp_stats.last_error,
            "raspotify_spotify_reachable":  rsp_stats.spotify_reachable,
            "raspotify_internet_reachable": rsp_stats.internet_reachable,
        }

        digest = hashlib.md5(json.dumps(state_signature, sort_keys=True).encode()).hexdigest()
        if digest == self._last_hash:
            logging.debug("status unchanged, skipping push")
            return

        payload = {
            "system": {
                "uptime_seconds":      sys_stats.uptime_seconds,
                "cpu_percent":         sys_stats.cpu_percent,
                "memory_available_mb": sys_stats.memory_available_mb,
                "disk_available_gb":   sys_stats.disk_available_gb,
                "cpu_temp_celsius":    sys_stats.cpu_temp_celsius,
                "throttled":           sys_stats.throttled,
            },
            "raspotify": {
                "uptime_seconds":     svc.uptime_seconds,
                "active":             svc.active,
                "state":              svc.state,
                "restart_count":      svc.restart_count,
                "last_exit_code":     svc.last_exit_code,
                "sink_state":         rsp_stats.sink_state,
                "currently_playing":  rsp_stats.currently_playing,
                "last_error":         rsp_stats.last_error,
                "spotify_reachable":  rsp_stats.spotify_reachable,
                "internet_reachable": rsp_stats.internet_reachable,
            },
        }

        self._client.push_event("status_snapshot", payload)
        self._last_hash = digest


# ─── Entry point ──────────────────────────────────────────────────────────────

def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    client = SupabaseClient()

    bt_thread = threading.Thread(
        target=BluetoothEventWatcher(client).run, daemon=True, name="bt-watcher"
    )
    bt_thread.start()

    StatsPoller(client).run()


if __name__ == "__main__":
    main()
