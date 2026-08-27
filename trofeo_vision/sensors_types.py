"""Live sensor snapshot.

All values are presentable strings + raw numbers. The renderer just
consumes the strings; numbers are kept for downstream logging/UI.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class CpuStats:
    usage_percent: float = 0.0          # overall %
    temp_c: float = 0.0                 # package temp °C
    power_w: float = 0.0                # package power W
    fan_rpm: float = 0.0                # CPU fan RPM
    clock_mhz: float = 0.0              # current clock MHz
    pump_rpm: float = 0.0               # AIO pump RPM
    voltage_v: float = 0.0              # VCore voltage V

    @property
    def usage_str(self) -> str:   return f"{int(round(self.usage_percent))}%"
    @property
    def temp_str(self) -> str:    return f"{int(round(self.temp_c))}°"
    @property
    def power_str(self) -> str:   return f"{int(round(self.power_w))}W"
    @property
    def fan_str(self) -> str:
        # Unit (RPM) is rendered separately by draw_side_grid — value is bare number.
        return f"{int(round(self.fan_rpm))}" if self.fan_rpm > 0 else "-"
    @property
    def clock_str(self) -> str:
        if self.clock_mhz <= 0:
            return "-"
        return f"{int(round(self.clock_mhz))}"
    @property
    def pump_str(self) -> str:
        # Unit (RPM) is rendered separately by draw_side_grid.
        return f"{int(round(self.pump_rpm))}" if self.pump_rpm > 0 else "-"
    @property
    def voltage_str(self) -> str: return f"{self.voltage_v:.2f}V"


@dataclass(frozen=True)
class GpuStats:
    usage_percent: float = 0.0
    temp_c: float = 0.0
    power_w: float = 0.0
    fan_rpm: float = 0.0
    clock_mhz: float = 0.0
    vram_used_mb: float = 0.0
    vram_total_mb: float = 0.0
    voltage_v: float = 0.0

    @property
    def usage_str(self) -> str:   return f"{int(round(self.usage_percent))}%"
    @property
    def temp_str(self) -> str:    return f"{int(round(self.temp_c))}°"
    @property
    def power_str(self) -> str:   return f"{int(round(self.power_w))}W"
    @property
    def fan_str(self) -> str:
        return f"{int(round(self.fan_rpm))}" if self.fan_rpm > 0 else "-"
    @property
    def clock_str(self) -> str:
        if self.clock_mhz <= 0:
            return "-"
        # Bare MHz number — the renderer appends " MHz" via draw_side_grid's
        # unit suffix (matching how CpuStats.clock_str is rendered).
        return f"{int(round(self.clock_mhz))}"
    @property
    def vram_percent(self) -> float:
        if self.vram_total_mb <= 0:
            return 0.0
        return 100.0 * self.vram_used_mb / self.vram_total_mb
    @property
    def vram_used_str(self) -> str:
        """MB used — renderer just shows the string verbatim."""
        if self.vram_used_mb <= 0:
            return "-"
        return f"{int(round(self.vram_used_mb))} MB"
    @property
    def vram_str(self) -> str:
        # Kept for back-compat / dashboard progress bar; new layout uses
        # `vram_used_str` for the side grid.
        return f"{int(round(self.vram_percent))}%"
    @property
    def voltage_str(self) -> str: return f"{self.voltage_v:.2f}V"


@dataclass(frozen=True)
class MemDiskStats:
    ram_percent: float = 0.0
    ram_used_gb: float = 0.0
    ram_total_gb: float = 0.0
    disk_percent: float = 0.0
    disk_used_gb: float = 0.0
    disk_total_gb: float = 0.0

    @property
    def ram_str(self) -> str:     return f"{int(round(self.ram_percent))}%"
    @property
    def disk_str(self) -> str:    return f"{int(round(self.disk_percent))}%"


@dataclass(frozen=True)
class NetStats:
    up_bps: float = 0.0
    down_bps: float = 0.0

    def _fmt(self, bps: float) -> str:
        units = ["B", "K", "M", "G"]
        val = float(bps)
        idx = 0
        while val >= 1024 and idx < len(units) - 1:
            val /= 1024.0
            idx += 1
        return f"{val:.0f}{units[idx]}/s"

    @property
    def up_str(self) -> str:   return f"{self._fmt(self.up_bps):>7s}"
    @property
    def down_str(self) -> str: return f"{self._fmt(self.down_bps):>7s}"


@dataclass(frozen=True)
class FpsStats:
    fps: float = 0.0

    @property
    def fps_str(self) -> str:
        return f"{int(round(self.fps))}" if self.fps > 0 else "0"


@dataclass(frozen=True)
class WeatherDay:
    label: str       # "Today", "Tue", "Wed"
    high_c: float
    low_c: float
    icon_code: str   # OpenWeatherMap icon id (e.g. "01d", "10n")


@dataclass(frozen=True)
class WeatherStats:
    available: bool = False
    location: str = ""
    current_temp_c: float = 0.0
    current_icon: str = ""
    forecast: list[WeatherDay] = field(default_factory=list)
    error: str = ""


@dataclass(frozen=True)
class SensorSnapshot:
    """All live data needed to render one frame."""

    timestamp: float = field(default_factory=time.time)
    cpu: CpuStats = field(default_factory=CpuStats)
    gpu: GpuStats = field(default_factory=GpuStats)
    mem: MemDiskStats = field(default_factory=MemDiskStats)
    net: NetStats = field(default_factory=NetStats)
    fps: FpsStats = field(default_factory=FpsStats)
    weather: WeatherStats = field(default_factory=WeatherStats)
