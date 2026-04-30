import re
import shutil
import subprocess
import time
from typing import Optional

from models import BluetoothDevice, RaspotifyStats, ServiceStats, SystemStats


# ─── Shared utility ───────────────────────────────────────────────────────────

def run(cmd: str, timeout: int = 5) -> tuple[str, int]:
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip(), r.returncode
    except Exception as e:
        return str(e), 1


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

        uptime_seconds: Optional[int] = None
        monotonic_usec = self._get_property(service, "ActiveEnterTimestampMonotonic")
        if active and monotonic_usec:
            try:
                status_out, _ = run(f"systemctl show {service} --property=ActiveEnterTimestampMonotonic --value")
                mono_us = int(status_out.strip())
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
    Checks Raspotify/librespot health: service state, PulseAudio sink
    activity, and recent errors from the journal.
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
                parts = line.split()
                return parts[-1] if parts else "unknown"
        return "unknown"

    def _get_last_error(self) -> Optional[str]:
        """Returns the most recent ERROR line from the raspotify journal."""
        out, code = run("journalctl -u raspotify -n 100 --no-pager --output=short", timeout=8)
        if code != 0:
            return None
        for line in reversed(out.splitlines()):
            if "ERROR" in line:
                parts = line.split("librespot", 1)
                return parts[-1].strip().lstrip("[]0123456789: ") if parts else line
        return None

    def get_stats(self) -> RaspotifyStats:
        return RaspotifyStats(
            service=self._service_monitor.get_stats("raspotify"),
            sink_state=self._get_sink_state(),
            last_error=self._get_last_error(),
        )


# ─── Report ───────────────────────────────────────────────────────────────────

def print_report() -> None:
    bt_mon  = BluetoothMonitor()
    sys_mon = SystemMonitor()
    rsp_mon = RaspotifyMonitor()

    print("\n┌─────────────────────────────────────┐")
    print("│         Raspberry Pi Health         │")
    print("└─────────────────────────────────────┘")

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
    if rsp.last_error:
        print(f"  Last error   {rsp.last_error}")
    print()


if __name__ == "__main__":
    print_report()
