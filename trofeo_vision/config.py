"""Runtime configuration for Trofeo Vision dashboard.

Loads from `config.toml` next to this file, falling back to defaults.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


CONFIG_PATH = Path(__file__).parent / "config.toml"


@dataclass(frozen=True)
class UsbConfig:
    vendor_id: int = 0x0416
    product_id: int = 0x5408
    interface: int = 0
    endpoint_out: int = 0x09
    endpoint_in: int = 0x81
    # How many jpeg frames the device expects per second.
    target_fps: float = 4.0
    # How often to refresh sensor data (decoupled from frame push).
    sensor_refresh_seconds: float = 1.0


@dataclass(frozen=True)
class WeatherConfig:
    enabled: bool = False
    api_key: str = ""
    city: str = "Manila"
    country: str = "PH"
    units: str = "metric"  # metric | imperial
    refresh_seconds: int = 600  # 10 min cache


@dataclass(frozen=True)
class DisplayConfig:
    width: int = 462
    height: int = 1920
    jpeg_quality: int = 85


@dataclass(frozen=True)
class Config:
    usb: UsbConfig = field(default_factory=UsbConfig)
    weather: WeatherConfig = field(default_factory=WeatherConfig)
    display: DisplayConfig = field(default_factory=DisplayConfig)

    @classmethod
    def load(cls) -> "Config":
        """Load config.toml if present; otherwise return defaults."""
        if not CONFIG_PATH.exists():
            return cls()
        with CONFIG_PATH.open("rb") as f:
            data = tomllib.load(f)
        usb = UsbConfig(**data.get("usb", {}))
        weather = WeatherConfig(**data.get("weather", {}))
        display = DisplayConfig(**data.get("display", {}))
        return cls(usb=usb, weather=weather, display=display)

    @classmethod
    def write_default(cls, path: Path | None = None) -> Path:
        """Write a default config.toml so the user can see all options."""
        out = path or CONFIG_PATH
        out.write_text(
            """# Trofeo Vision dashboard configuration

[usb]
vendor_id = 0x0416
product_id = 0x5408
interface = 0
endpoint_out = 9
endpoint_in = 129   # 0x81
target_fps = 4.0
sensor_refresh_seconds = 1.0

[weather]
enabled = false
api_key = ""        # https://openweathermap.org/api/one-call-3
city = "Manila"
country = "PH"
units = "metric"
refresh_seconds = 600

[display]
width = 462
height = 1920
jpeg_quality = 85
""",
            encoding="utf-8",
        )
        return out
