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
    # Default 2 FPS — the LCD is a passive glanceable display, not a
    # gaming monitor. Halving from 4 FPS cuts frame-push CPU + USB I/O
    # roughly in half without any visible difference to the eye at
    # glance distance. Bump this in config.toml if you want smoother.
    target_fps: float = 2.0
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
    # Directory holding <state>_<time_bucket>.png backgrounds for the greeting tile.
    # Default: <repo>/assets/weather/ (resolved at runtime; see weather.py).
    assets_dir: str = ""


@dataclass(frozen=True)
class GreetingConfig:
    """User-facing greeting tile configuration."""

    # Name shown after "Good <time of day>, ".
    name: str = "Bambaw"


@dataclass(frozen=True)
class DisplayConfig:
    width: int = 462
    height: int = 1920
    jpeg_quality: int = 85


@dataclass(frozen=True)
class ThemeConfig:
    """Color theme for the dashboard.

    ``mode`` is one of:
        "auto"  - follow ``dark_start_hour`` / ``dark_end_hour`` using local
                  time (default; dark 18:00-06:00 by default).
        "light" - always light mode (the original look).
        "dark"  - always dark mode.

    The auto window can wrap midnight: with ``dark_start_hour=18`` and
    ``dark_end_hour=6``, dark mode is active from 18:00 through 05:59 and
    light mode from 06:00 through 17:59. A non-wrapping window (e.g.
    ``dark_start_hour=20, dark_end_hour=23``) is also supported.
    """

    mode: str = "auto"
    dark_start_hour: int = 18
    dark_end_hour: int = 6


@dataclass(frozen=True)
class Config:
    usb: UsbConfig = field(default_factory=UsbConfig)
    weather: WeatherConfig = field(default_factory=WeatherConfig)
    greeting: GreetingConfig = field(default_factory=GreetingConfig)
    display: DisplayConfig = field(default_factory=DisplayConfig)
    theme: ThemeConfig = field(default_factory=ThemeConfig)

    @classmethod
    def load(cls) -> "Config":
        """Load config.toml if present; otherwise return defaults."""
        if not CONFIG_PATH.exists():
            return cls()
        with CONFIG_PATH.open("rb") as f:
            data = tomllib.load(f)
        usb = UsbConfig(**data.get("usb", {}))
        weather = WeatherConfig(**data.get("weather", {}))
        greeting = GreetingConfig(**data.get("greeting", {}))
        display = DisplayConfig(**data.get("display", {}))
        theme = ThemeConfig(**data.get("theme", {}))
        return cls(usb=usb, weather=weather, greeting=greeting,
                   display=display, theme=theme)

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
target_fps = 2.0
sensor_refresh_seconds = 1.0

[weather]
enabled = false
# Get a key at https://home.openweathermap.org/api_keys then paste it here
# and flip ``enabled`` to true.
api_key = ""  # paste your OWM key here (https://home.openweathermap.org/api_keys)
city = "Davao"
country = "PH"
units = "metric"
refresh_seconds = 600
# assets_dir = ""   # leave blank to use <repo>/assets/weather/

[greeting]
name = "Bambaw"     # shown after "Good <morning|noon|afternoon|evening|night>, "

[display]
width = 462
height = 1920
jpeg_quality = 85

[theme]
# "auto" switches between light + dark based on local time (below).
# "light" or "dark" forces a single mode.
mode = "auto"
# When mode = "auto", dark theme is active from dark_start_hour (inclusive)
# through dark_end_hour (exclusive). The window may wrap midnight: defaults
# of 18-6 mean dark from 18:00 to 05:59 and light from 06:00 to 17:59.
dark_start_hour = 18
dark_end_hour = 6
""",
            encoding="utf-8",
        )
        return out
