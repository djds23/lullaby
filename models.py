from dataclasses import dataclass
from typing import Optional


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
