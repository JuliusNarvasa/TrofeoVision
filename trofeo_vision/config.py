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
class AirQualityConfig:
    """Open-Meteo Air Quality API settings for the air-quality top tile.

    Open-Meteo's Air Quality API is backed by the Copernicus Atmosphere
    Monitoring Service (CAMS) model and gives global coverage — including
    locations (like Davao) where neither OpenWeatherMap nor WAQI have a
    ground station. No API key required, free for non-commercial use.

    Earlier revisions of this module used the OWM Air Pollution API (sparse
    Mindanao coverage → reported AQI 1 on hazy days) and WAQI (no Davao
    station registered). Open-Meteo was the third attempt and stuck.

    US AQI breakpoints (used for the bucket label):
        0-50    Good  ·  51-100  Moderate  ·  101-150 Unhealthy (Sensitive)
        151-200 Unhealthy  ·  201-300 Very Unhealthy  ·  301+  Hazardous
    """
    enabled: bool = False
    # City name + ISO-3166-1 alpha2 country code (e.g. "PH") for the
    # Open-Meteo geocoder. ``country`` is what keeps "Davao" resolving
    # to Davao City, Philippines instead of Davao, Costa Rica.
    city: str = ""
    country: str = ""
    refresh_seconds: int = 1800  # 30 min cache — pollutants move slower than weather
    # Directory holding <aqi_state>_<bucket>.png backgrounds for the tile.
    # Default: <repo>/assets/air_quality/ (see air_quality.py).
    assets_dir: str = ""


@dataclass(frozen=True)
class TopTileConfig:
    """Which payload the top tile shows.

    ``mode`` is one of:
        "weather"     — greeting + weather (the original layout).
        "air_quality" — greeting + air quality (AQI + PM2.5/PM10).

    Tray menu flips this and persists to config.toml.
    """
    mode: str = "weather"  # weather | air_quality


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
    air_quality: AirQualityConfig = field(default_factory=AirQualityConfig)
    top_tile: TopTileConfig = field(default_factory=TopTileConfig)
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
        # Resolve air_quality location fallback against weather, and drop
        # the legacy ``api_key`` field from older configs (the WAQI client
        # that needed it was replaced by Open-Meteo — keyless). Keeping
        # the field silently swallowed here means existing config.toml
        # files don't crash the loader on upgrade.
        raw_aq = dict(data.get("air_quality", {}))
        raw_aq.pop("api_key", None)
        if not raw_aq.get("city"):
            raw_aq["city"] = weather.city
        if not raw_aq.get("country"):
            raw_aq["country"] = weather.country
        air_quality = AirQualityConfig(**raw_aq)
        top_tile = TopTileConfig(**data.get("top_tile", {}))
        greeting = GreetingConfig(**data.get("greeting", {}))
        display = DisplayConfig(**data.get("display", {}))
        theme = ThemeConfig(**data.get("theme", {}))
        return cls(usb=usb, weather=weather, air_quality=air_quality,
                   top_tile=top_tile, greeting=greeting,
                   display=display, theme=theme)

    def save(self, path: Path | None = None) -> Path:
        """Write the current config back to disk.

        Used by the tray menu when the user flips ``[top_tile].mode``
        between "weather" and "air_quality" — the choice persists across
        restarts so the tile comes up in the same mode next launch.

        Format mirrors ``write_default``'s canonical template (so the
        generated file is human-readable and well-commented). Hand edits
        to the file survive until the next ``save()`` call.
        """
        out = path or CONFIG_PATH
        out.write_text(self._to_toml_template(), encoding="utf-8")
        return out

    def _to_toml_template(self) -> str:
        """Render the config as a human-readable TOML document.

        Single source of truth for both ``write_default`` (bootstrap) and
        ``save`` (round-trip the live state). Keeping the format identical
        means a user who hand-edits config.toml and then later flips the
        top-tile mode from the tray sees a familiar file come back.
        """
        lines: list[str] = []

        def section(name: str, body: list[str], *, header: str = "") -> None:
            if lines:
                lines.append("")
            if header:
                lines.append(f"# {header}")
            lines.append(f"[{name}]")
            lines.extend(body)

        # ── usb ──────────────────────────────────────────────────────
        section("usb", [
            f"vendor_id = 0x{self.usb.vendor_id:04x}",
            f"product_id = 0x{self.usb.product_id:04x}",
            f"interface = {self.usb.interface}",
            f"endpoint_out = {self.usb.endpoint_out}",
            f"endpoint_in = {self.usb.endpoint_in}   # 0x{self.usb.endpoint_in:02x}",
            f"target_fps = {self.usb.target_fps}",
            f"sensor_refresh_seconds = {self.usb.sensor_refresh_seconds}",
        ])

        # ── weather ──────────────────────────────────────────────────
        w = self.weather
        section("weather", [
            f"enabled = {str(w.enabled).lower()}",
            "# Get a key at https://home.openweathermap.org/api_keys then paste it here",
            "# and flip ``enabled`` to true once you've pasted it.",
            f'api_key = "{w.api_key}"',
            f'city = "{w.city}"',
            f'country = "{w.country}"',
            f'units = "{w.units}"',
            f"refresh_seconds = {w.refresh_seconds}",
            f"# assets_dir = \"{w.assets_dir}\"   # leave blank to use <repo>/assets/weather/",
        ])

        # ── air_quality ──────────────────────────────────────────────
        a = self.air_quality
        # City/country inherit from weather when blank. No api_key needed
        # — Open-Meteo is free + keyless (CAMS-backed).
        aq_city = a.city or w.city
        aq_country = a.country or w.country
        section("air_quality", [
            f"enabled = {str(a.enabled).lower()}",
            "# Open-Meteo Air Quality API (CAMS model, global coverage, no API key).",
            "# https://open-meteo.com/en/docs/air-quality-api",
            f'city = "{aq_city}"   # blank → inherits from [weather].city',
            f'country = "{aq_country}"   # blank → inherits from [weather].country',
            f"refresh_seconds = {a.refresh_seconds}",
            f"# assets_dir = \"{a.assets_dir}\"   # leave blank to use <repo>/assets/air_quality/",
        ])

        # ── top_tile ─────────────────────────────────────────────────
        section("top_tile", [
            '# "weather" → greeting + weather (the original layout).',
            '# "air_quality" → greeting + AQI + PM2.5/PM10.',
            '# The tray menu flips this; the choice is persisted here.',
            f'mode = "{self.top_tile.mode}"',
        ], header="Top tile selection (flipped from the tray menu).")

        # ── greeting ─────────────────────────────────────────────────
        section("greeting", [
            f'name = "{self.greeting.name}"     # shown after "Good <morning|noon|afternoon|evening|night>, "',
        ])

        # ── display ──────────────────────────────────────────────────
        section("display", [
            f"width = {self.display.width}",
            f"height = {self.display.height}",
            f"jpeg_quality = {self.display.jpeg_quality}",
        ])

        # ── theme ────────────────────────────────────────────────────
        section("theme", [
            '# "auto" switches between light + dark based on local time (below).',
            '# "light" or "dark" forces a single mode.',
            f'mode = "{self.theme.mode}"',
            "# When mode = \"auto\", dark theme is active from dark_start_hour (inclusive)",
            "# through dark_end_hour (exclusive). The window may wrap midnight: defaults",
            "# of 18-6 mean dark from 18:00 to 05:59 and light from 06:00 to 17:59.",
            f"dark_start_hour = {self.theme.dark_start_hour}",
            f"dark_end_hour = {self.theme.dark_end_hour}",
        ])

        return "\n".join(lines) + "\n"

    @classmethod
    def write_default(cls, path: Path | None = None) -> Path:
        """Write a default config.toml so the user can see all options."""
        out = path or CONFIG_PATH
        # Default doc uses default values, which the constructor produces.
        out.write_text(cls()._to_toml_template(), encoding="utf-8")
        return out
