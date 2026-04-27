#!/usr/bin/env python3
"""
pi_health.py
System, Bluetooth, service and Raspotify health checks for Raspberry Pi.
"""

import subprocess
import shutil
import socket
import time
import re
from dataclasses import dataclass
from typing import Optional


# ─── Shared utility ───────────────────────────────────────────────────────────

def run(cmd: str, timeout: int = 5) -> tuple[str, int]:
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip(), r.returncode
    except Exception as e:
        return str(e), 1


# ─── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class BluetoothDevice:
    name: str
    mac: str
    connected: bool
    battery: Optional[int]          # percentage, or None if not reported


@dataclass
class SystemStats:
    uptime_seconds: int
    cpu_percent: float
    memory_total_mb: float
    memory_available_mb: float
    disk_total_gb: float
    disk_available_gb: float
    cpu_temp_celsius: Optional[float]
    throttled: bool


@dataclass
class ServiceStats:
    name: str
    active: bool
    state: str                      # active / inactive / failed / activating
    uptime_seconds: Optional[int]
    restart_count: int
    last_exit_code: Optional[int]


@dataclass
class RaspotifyStats:
    service: ServiceStats
    sink_state: str                 # RUNNING / SUSPENDED / unknown
    currently_playing: Optional[str]
    last_error: Optional[str]
    spotify_reachable: bool
    internet_reachable: bool


# ─── Bluetooth ────────────────────────────────────────────────────────────────

class BluetoothMonitor:
    """
    Queries BlueZ via bluetoothctl for connected audio devices.
    Battery is read from bluetoothctl / UPower where the device supports it.
    """

    def _get_paired_macs(self) -> list[str]:
        out, _ = run("bluetoothctl devices")
        macs = []
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] == "Device":
                macs.append(parts[1])
        return macs

    def _get_device_info(self, mac: str) -> Optional[BluetoothDevice]:
        out, code = run(f"bluetoothctl info {mac}")
        if code != 0 or not out:
            return None

        def extract(label: str) -> Optional[str]:
            for line in out.splitlines():
                if label in line:
                    return line.split(":", 1)[1].strip()
            return None

        if extract("Connected") != "yes":
            return None

        # Filter to audio devices only by checking UUID labels
        audio_uuids = {"Audio Sink", "Audio Source", "Headset", "Handsfree", "A/V Remote"}
        if not any(u in out for u in audio_uuids):
            return None

        return BluetoothDevice(
            name=extract("Name") or mac,
            mac=mac,
            connected=True,
            battery=self._get_battery(mac, out),
        )

    def _get_battery(self, mac: str, info_out: str) -> Optional[int]:
        # Some devices expose battery directly in bluetoothctl info
        for line in info_out.splitlines():
            if "Battery Percentage" in line:
                match = re.search(r"(\d+)", line)
                if match and int(match.group(1)) > 0:
                    return int(match.group(1))

        # Fall back to UPower — try both common path formats
        mac_under = mac.replace(":", "_")
        for path in [
            f"/org/freedesktop/UPower/devices/headset_dev_{mac_under}",
            f"/org/freedesktop/UPower/devices/bluez_dev_{mac_under}",
        ]:
            out, code = run(f"upower -i {path}")
            if code == 0:
                for line in out.splitlines():
                    if "percentage" in line.lower():
                        match = re.search(r"(\d+)%", line)
                        if match and int(match.group(1)) > 0:
                            return int(match.group(1))

        return None

    def get_connected_audio_devices(self) -> list[BluetoothDevice]:
        """Returns all currently connected Bluetooth audio devices."""
        devices = []
        for mac in self._get_paired_macs():
            device = self._get_device_info(mac)
            if device:
                devices.append(device)
        return devices


# ─── System ───────────────────────────────────────────────────────────────────

class SystemMonitor:
    """
    Collects Pi system stats: CPU, memory, disk, uptime, temperature, throttle.
    Reads directly from /proc where possible to avoid shelling out unnecessarily.
    """

    def _get_uptime_seconds(self) -> int:
        try:
            with open("/proc/uptime") as f:
                return int(float(f.read().split()[0]))
        except Exception:
            return 0

    def _get_cpu_percent(self) -> float:
        """Samples /proc/stat twice 250ms apart for an accurate idle ratio."""
        def read_cpu():
            with open("/proc/stat") as f:
                line = f.readline()
            vals = list(map(int, line.split()[1:]))
            return vals[3], sum(vals)  # idle, total

        idle1, total1 = read_cpu()
        time.sleep(0.25)
        idle2, total2 = read_cpu()
        total_delta = total2 - total1
        if total_delta == 0:
            return 0.0
        return round((1 - (idle2 - idle1) / total_delta) * 100, 1)

    def _get_memory(self) -> tuple[float, float]:
        """Returns (total_mb, available_mb) from /proc/meminfo."""
        total = available = 0.0
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        total = int(line.split()[1]) / 1024
                    elif line.startswith("MemAvailable:"):
                        available = int(line.split()[1]) / 1024
        except Exception:
            pass
        return round(total, 1), round(available, 1)

    def _get_disk(self, path: str = "/") -> tuple[float, float]:
        """Returns (total_gb, free_gb) for the given mount path."""
        try:
            usage = shutil.disk_usage(path)
            return round(usage.total / 1e9, 2), round(usage.free / 1e9, 2)
        except Exception:
            return 0.0, 0.0

    def _get_cpu_temp(self) -> Optional[float]:
        out, code = run("vcgencmd measure_temp")
        if code != 0:
            return None
        match = re.search(r"temp=([\d.]+)", out)
        return float(match.group(1)) if match else None

    def _get_throttled(self) -> bool:
        out, code = run("vcgencmd get_throttled")
        if code != 0:
            return False
        match = re.search(r"throttled=(0x[0-9a-fA-F]+)", out)
        return bool(match and int(match.group(1), 16) != 0)

    def get_stats(self) -> SystemStats:
        mem_total, mem_available = self._get_memory()
        disk_total, disk_available = self._get_disk()
        return SystemStats(
            uptime_seconds=self._get_uptime_seconds(),
            cpu_percent=self._get_cpu_percent(),
            memory_total_mb=mem_total,
            memory_available_mb=mem_available,
            disk_total_gb=disk_total,
            disk_available_gb=disk_available,
            cpu_temp_celsius=self._get_cpu_temp(),
            throttled=self._get_throttled(),
        )

    @staticmethod
    def format_uptime(seconds: int) -> str:
        days, r = divmod(seconds, 86400)
        hours, r = divmod(r, 3600)
        minutes, _ = divmod(r, 60)
        parts = []
        if days:
            parts.append(f"{days}d")
        if hours:
            parts.append(f"{hours}h")
        parts.append(f"{minutes}m")
        return " ".join(parts)


# ─── Service ──────────────────────────────────────────────────────────────────

class ServiceMonitor:
    """
    Queries systemd for the health of a named service.
    Provides state, uptime, restart count, and last exit code.
    """

    def _get_property(self, service: str, prop: str) -> str:
        out, _ = run(f"systemctl show {service} --property={prop} --value")
        return out.strip()

    def get_stats(self, service: str) -> ServiceStats:
        state = self._get_property(service, "ActiveState")
        active = state == "active"

        # Uptime: calculate from ActiveEnterTimestamp (microseconds since epoch)
        uptime_seconds: Optional[int] = None
        ts_usec = self._get_property(service, "ActiveEnterTimestampMonotonic")
        monotonic_usec = self._get_property(service, "ActiveEnterTimestampMonotonic")
        if active and monotonic_usec:
            try:
                # Use the wall-clock timestamp instead for simplicity
                ts_out = self._get_property(service, "ActiveEnterTimestamp")
                # systemd format: "Mon 2024-01-01 12:00:00 UTC"
                # Easier to just calculate from the service start via systemctl status
                status_out, _ = run(f"systemctl show {service} --property=ActiveEnterTimestampMonotonic --value")
                mono_us = int(status_out.strip())
                # /proc/uptime gives us current monotonic time in seconds
                with open("/proc/uptime") as f:
                    system_mono_s = float(f.read().split()[0])
                system_mono_us = system_mono_s * 1_000_000
                uptime_seconds = max(0, int((system_mono_us - mono_us) / 1_000_000))
            except Exception:
                uptime_seconds = None

        restart_count = 0
        try:
            restart_count = int(self._get_property(service, "NRestarts"))
        except Exception:
            pass

        last_exit_code: Optional[int] = None
        try:
            code = int(self._get_property(service, "ExecMainCode"))
            last_exit_code = code if code != 0 else None
        except Exception:
            pass

        return ServiceStats(
            name=service,
            active=active,
            state=state,
            uptime_seconds=uptime_seconds,
            restart_count=restart_count,
            last_exit_code=last_exit_code,
        )


# ─── Raspotify ────────────────────────────────────────────────────────────────

class RaspotifyMonitor:
    """
    Checks Raspotify/librespot health: service state, PipeWire sink activity,
    currently playing track, recent errors, and network reachability.
    """

    SINK_PATTERN = re.compile(r"bluez_output\.[0-9A-Fa-f_]+\.\d")

    def __init__(self):
        self._service_monitor = ServiceMonitor()

    def _get_sink_state(self) -> str:
        out, code = run("pactl list sinks short")
        if code != 0:
            return "unknown"
        for line in out.splitlines():
            if self.SINK_PATTERN.search(line):
                # Format: id  name  driver  format  state
                parts = line.split()
                return parts[-1] if parts else "unknown"
        return "unknown"

    def _parse_journal(self) -> tuple[Optional[str], Optional[str]]:
        """
        Scrapes the last 100 librespot journal lines for the most recently
        loaded track and the most recent error.
        Returns (currently_playing, last_error).
        """
        out, code = run("journalctl -u raspotify -n 100 --no-pager --output=short", timeout=8)
        if code != 0:
            return None, None

        currently_playing: Optional[str] = None
        last_error: Optional[str] = None

        for line in reversed(out.splitlines()):
            if currently_playing is None:
                # librespot logs: Loading track: "Track Name" by Artist
                match = re.search(r'Loading track "(.+?)"', line)
                if match:
                    currently_playing = match.group(1)

            if last_error is None and "ERROR" in line:
                # Strip the systemd timestamp prefix and return the message
                parts = line.split("librespot", 1)
                last_error = parts[-1].strip().lstrip("[]0123456789: ") if parts else line

            if currently_playing and last_error:
                break

        return currently_playing, last_error

    def _check_host(self, host: str, port: int = 443) -> bool:
        try:
            socket.setdefaulttimeout(3)
            socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect((host, port))
            return True
        except Exception:
            return False

    def get_stats(self) -> RaspotifyStats:
        service = self._service_monitor.get_stats("raspotify")
        currently_playing, last_error = self._parse_journal()
        return RaspotifyStats(
            service=service,
            sink_state=self._get_sink_state(),
            currently_playing=currently_playing,
            last_error=last_error,
            spotify_reachable=self._check_host("ap.spotify.com"),
            internet_reachable=self._check_host("www.google.com"),
        )


# ─── Report ───────────────────────────────────────────────────────────────────

def print_report():
    bt_mon    = BluetoothMonitor()
    sys_mon   = SystemMonitor()
    rsp_mon   = RaspotifyMonitor()

    print("\n┌─────────────────────────────────────┐")
    print("│         Raspberry Pi Health         │")
    print("└─────────────────────────────────────┘")

    # ── System ────────────────────────────────
    stats = sys_mon.get_stats()
    mem_used_pct  = round((1 - stats.memory_available_mb / stats.memory_total_mb) * 100, 1)
    disk_used_pct = round((1 - stats.disk_available_gb / stats.disk_total_gb) * 100, 1)

    print("\n── System ───────────────────────────────")
    print(f"  Uptime       {SystemMonitor.format_uptime(stats.uptime_seconds)}")
    print(f"  CPU Usage    {stats.cpu_percent}%")
    print(f"  Memory       {stats.memory_available_mb:.0f} MB free of {stats.memory_total_mb:.0f} MB  ({mem_used_pct}% used)")
    print(f"  Disk         {stats.disk_available_gb:.1f} GB free of {stats.disk_total_gb:.1f} GB  ({disk_used_pct}% used)")
    if stats.cpu_temp_celsius is not None:
        warn = "  ⚠ high" if stats.cpu_temp_celsius >= 75 else ""
        print(f"  CPU Temp     {stats.cpu_temp_celsius}°C{warn}")
    if stats.throttled:
        print("  Throttled    ⚠ yes — check power supply or cooling")

    # ── Bluetooth ─────────────────────────────
    print("\n── Bluetooth ────────────────────────────")
    devices = bt_mon.get_connected_audio_devices()
    if not devices:
        print("  No connected audio devices found.")
    else:
        for d in devices:
            print(f"  Name         {d.name}")
            print(f"  MAC          {d.mac}")
            if d.battery:
                print(f"  Battery      {d.battery}%")

    # ── Raspotify ─────────────────────────────
    rsp = rsp_mon.get_stats()
    svc = rsp.service

    print("\n── Raspotify ────────────────────────────")
    state_flag = "" if svc.active else "  ⚠"
    print(f"  Service      {svc.state}{state_flag}")

    if svc.active and svc.uptime_seconds is not None:
        print(f"  Uptime       {SystemMonitor.format_uptime(svc.uptime_seconds)}")

    if svc.restart_count > 0:
        print(f"  Restarts     {svc.restart_count}  ⚠ service has crashed and recovered")

    sink_flag = "" if rsp.sink_state == "RUNNING" else f"  ({rsp.sink_state.lower()})"
    print(f"  Audio sink   {rsp.sink_state}{sink_flag}")

    if rsp.currently_playing:
        print(f"  Now playing  {rsp.currently_playing}")

    if rsp.last_error:
        print(f"  Last error   {rsp.last_error}")

    # ── Network ───────────────────────────────
    print("\n── Network ──────────────────────────────")
    internet = "✓ reachable" if rsp.internet_reachable else "✗ unreachable"
    spotify  = "✓ reachable" if rsp.spotify_reachable  else "✗ unreachable"
    print(f"  Internet     {internet}  (google.com)")
    print(f"  Spotify      {spotify}  (ap.spotify.com)")

    if not rsp.internet_reachable:
        print("  ⚠ No internet — check your network connection")
    elif not rsp.spotify_reachable:
        print("  ⚠ Internet up but Spotify unreachable — possible block or outage")

    print()


if __name__ == "__main__":
    print_report()
