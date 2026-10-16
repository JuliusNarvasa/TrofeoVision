"""Weather data fetcher (OpenWeatherMap).

Cached in memory for `refresh_seconds`. Disabled by default — needs an API key.
Activate by setting [weather] enabled = true and api_key = "<your key>" in config.toml.

Free tier: https://openweathermap.org/api/one-call-3 (1000 calls/day free).

Also provides `WeatherImages`, a tiny loader for the greeting-tile background
images under `<repo>/assets/weather/<state>_<bucket>.png`.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from PIL import Image

from .config import WeatherConfig
from .sensors_types import WeatherDay, WeatherStats


log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Greeting-tile background images
# ─────────────────────────────────────────────────────────────────────────────
#
# 8 weather states × 5 time-of-day buckets = 40 files, named:
#     <state>_<bucket>.png  (e.g. "clear_morning.png", "rain_night.png")
#
# Bucket bounds (local time, see `time_bucket` below):
#     morning    5–11   noon      12      afternoon  13–17
#     evening   18–21   night     22–4   (wraps)
#
# All images must be 462×280 to match the greeting tile exactly.

# 8 weather states in canonical order. The mapping from OpenWeatherMap icon
# codes (e.g. "01d", "10n") to these states lives in `icon_to_state` below.
WEATHER_STATES = (
    "clear",
    "partly_cloudy",
    "cloudy",
    "drizzle",
    "rain",
    "thunder",
    "snow",
    "fog",
)

TIME_BUCKETS = ("morning", "noon", "afternoon", "evening", "night")

# Map OWM icon prefix → weather state. We keep day/night codes merged so
# the same image can be reused; the renderer overlays a dark gradient for
# night buckets anyway.
_ICON_PREFIX_TO_STATE: dict[str, str] = {
    "01": "clear",
    "02": "partly_cloudy",
    "03": "cloudy",
    "04": "cloudy",
    "09": "drizzle",
    "10": "rain",
    "11": "thunder",
    "13": "snow",
    "50": "fog",
}

_FALLBACK_STATE = "clear"
_FALLBACK_BUCKET = "morning"


def icon_to_state(icon_code: str) -> str:
    """Map an OpenWeatherMap icon code (e.g. "01d") to one of WEATHER_STATES."""
    prefix = icon_code[:2] if icon_code else ""
    return _ICON_PREFIX_TO_STATE.get(prefix, _FALLBACK_STATE)


def time_bucket(dt: datetime) -> str:
    """Map a local-time datetime to one of TIME_BUCKETS.

        morning    05:00–11:59
        noon       12:00–12:59   (single hour)
        afternoon  13:00–17:59
        evening    18:00–21:59
        night      22:00–04:59   (wraps midnight)
    """
    h = dt.hour
    if 5 <= h < 12:
        return "morning"
    if h == 12:
        return "noon"
    if 13 <= h < 18:
        return "afternoon"
    if 18 <= h < 22:
        return "evening"
    return "night"


def greeting_for_bucket(bucket: str) -> str:
    """The word(s) that prefix the user's name on the greeting tile.

    Night falls back to "Good evening" — common UX convention (apps rarely
    say "Good night" since it sounds like a farewell).
    """
    return {
        "morning":   "Good morning",
        "noon":      "Good noon",
        "afternoon": "Good afternoon",
        "evening":   "Good evening",
        "night":     "Good evening",
    }.get(bucket, "Hello")


class WeatherImages:
    """Lazy loader + cache for greeting-tile background PNGs.

    Resolves (state, bucket) → a PIL.Image by reading `<state>_<bucket>.png`
    out of `assets_dir`. Missing files fall back to `clear_morning.png`,
    then to a solid dark-blue placeholder so the renderer never crashes on
    a partial asset set. All images are kept at their native resolution
    (the renderer resizes / pastes them as-is onto the 462×280 tile).
    """

    def __init__(self, assets_dir: Path):
        self.assets_dir = Path(assets_dir)
        self._cache: dict[tuple[str, str], Image.Image] = {}
        # Pre-rendered solid fallback if even clear_morning.png is missing.
        self._placeholder = Image.new("RGB", (462, 280), (24, 64, 110))

    def get(self, state: str, bucket: str) -> Image.Image:
        """Return the background image for the given (state, bucket) combo.

        Cache key is the requested combo so we still attempt fresh lookups
        after a missing-file fallback, but once a real file is loaded we
        keep reusing it. State is normalized to a known set so an unknown
        OWM prefix can't poison the cache.
        """
        state = state if state in WEATHER_STATES else _FALLBACK_STATE
        bucket = bucket if bucket in TIME_BUCKETS else _FALLBACK_BUCKET
        key = (state, bucket)
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        path = self.assets_dir / f"{state}_{bucket}.png"
        if not path.exists():
            log.warning("Weather image missing: %s — using fallback", path)
            img = self._load_or_placeholder(_FALLBACK_STATE, _FALLBACK_BUCKET)
        else:
            try:
                img = Image.open(path).convert("RGB")
            except Exception as e:  # noqa: BLE001
                log.warning("Weather image unreadable: %s (%s) — using placeholder",
                            path, e)
                img = self._placeholder
        self._cache[key] = img
        return img

    def _load_or_placeholder(self, state: str, bucket: str) -> Image.Image:
        """Internal: load a fallback combo, recursing at most once."""
        path = self.assets_dir / f"{state}_{bucket}.png"
        if path.exists():
            try:
                return Image.open(path).convert("RGB")
            except Exception:  # noqa: BLE001
                pass
        return self._placeholder

    @staticmethod
    def default_assets_dir() -> Path:
        """The canonical assets folder: <repo>/assets/weather/."""
        # config.py lives in <repo>/trofeo_vision/. parents[1] is the repo root.
        return Path(__file__).resolve().parent.parent / "assets" / "weather"


class WeatherClient:
    BASE_URL = "https://api.openweathermap.org/data/2.5/forecast"
    # Short timeout so a dead API stalls one frame for ~5 s max instead of
    # 10 s+. Per-frame refetch callers (main/tray/offline) only refetch
    # while `not available`, and the failure backoff below keeps a down
    # API from stalling every frame.
    REQUEST_TIMEOUT_S = 5.0
    # Negative cache: after a failure, serve the placeholder (or last-good
    # cache) immediately without hitting the network for this long.
    FAILURE_BACKOFF_S = 60.0

    def __init__(self, cfg: WeatherConfig):
        self.cfg = cfg
        self._cached: Optional[WeatherStats] = None
        self._last_fetch = 0.0
        self._last_failure = 0.0
        self._last_error = ""

    def get(self) -> WeatherStats:
        """Return cached weather, refreshing when stale. Never raises —
        on any failure path returns last-good cache or an
        ``available=False`` placeholder so the frame loop keeps pushing.
        """
        try:
            if not self.cfg.enabled or not self.cfg.api_key:
                return WeatherStats(available=False, error="weather disabled")
            now = time.time()
            if self._cached and (now - self._last_fetch) < self.cfg.refresh_seconds:
                return self._cached
            # Negative cache: don't hammer a dead API every frame.
            if (self._last_failure
                    and (now - self._last_failure) < self.FAILURE_BACKOFF_S):
                if self._cached:
                    return self._cached
                return WeatherStats(available=False, error=self._last_error or "backing off")
            return self._fetch()
        except Exception as e:  # noqa: BLE001 — get() must never raise
            log.warning("Weather get() unexpected error: %s", e)
            if self._cached:
                return self._cached
            return WeatherStats(available=False, error=f"unexpected: {e}")

    def _fetch(self) -> WeatherStats:
        params = {
            "q": f"{self.cfg.city},{self.cfg.country}",
            "appid": self.cfg.api_key,
            "units": self.cfg.units,
        }
        try:
            r = requests.get(self.BASE_URL, params=params,
                             timeout=self.REQUEST_TIMEOUT_S)
        except requests.RequestException as e:
            log.warning("Weather fetch failed: %s", e)
            return self._fail(f"network error: {e}")
        except Exception as e:  # noqa: BLE001 — never let _fetch raise
            log.warning("Weather fetch unexpected error: %s", e)
            return self._fail(f"unexpected: {e}")

        if r.status_code != 200:
            try:
                body = r.text[:120]
            except Exception:  # noqa: BLE001
                body = "<unreadable body>"
            return self._fail(f"HTTP {r.status_code}: {body}")

        try:
            data = r.json()
            self._cached = self._parse(data)
        except Exception as e:  # noqa: BLE001
            return self._fail(f"parse error: {e}")

        self._last_fetch = time.time()
        self._last_failure = 0.0
        return self._cached

    def _parse(self, data: dict) -> WeatherStats:
        city = data.get("city", {}).get("name", self.cfg.city)
        forecast_list = data.get("list", [])
        if not forecast_list:
            return WeatherStats(available=False, error="no forecast data")

        # Current = first entry.
        first = forecast_list[0]
        main = first.get("main", {})
        weather = first.get("weather", [{}])[0]
        current_temp_c = float(main.get("temp", 0.0))
        current_feels_like_c = float(main.get("feels_like", 0.0))
        current_icon = weather.get("icon", "")
        # OpenWeatherMap returns °C only when units=metric; we always ask metric.

        # Build a 3-day forecast by grouping entries into days (Asia/Manila local).
        today = datetime.now().date()
        by_day: dict[str, list[dict]] = {}
        for entry in forecast_list:
            dt = datetime.fromtimestamp(entry["dt"], tz=timezone.utc).astimezone()
            key = dt.strftime("%Y-%m-%d")
            by_day.setdefault(key, []).append(entry)

        days: list[WeatherDay] = []
        # Today's summary
        today_entries = by_day.get(today.strftime("%Y-%m-%d"), [])
        if today_entries:
            hi = max(float(e["main"]["temp_max"]) for e in today_entries)
            lo = min(float(e["main"]["temp_min"]) for e in today_entries)
            icon = today_entries[len(today_entries) // 2]["weather"][0].get("icon", "")
            days.append(WeatherDay(label="Today", high_c=hi, low_c=lo, icon_code=icon))

        # Next 2 days
        for key in sorted(by_day.keys()):
            d = datetime.strptime(key, "%Y-%m-%d").date()
            if d <= today:
                continue
            entries = by_day[key]
            hi = max(float(e["main"]["temp_max"]) for e in entries)
            lo = min(float(e["main"]["temp_min"]) for e in entries)
            icon = entries[len(entries) // 2]["weather"][0].get("icon", "")
            days.append(WeatherDay(label=d.strftime("%a"), high_c=hi, low_c=lo, icon_code=icon))
            if len(days) >= 3:
                break

        return WeatherStats(
            available=True,
            location=city,
            current_temp_c=current_temp_c,
            current_feels_like_c=current_feels_like_c,
            current_icon=current_icon,
            forecast=days,
        )

    def _fail(self, msg: str) -> WeatherStats:
        log.warning("Weather: %s", msg)
        self._last_failure = time.time()
        self._last_error = msg
        # Keep the cache if we have one — show last known good.
        if self._cached:
            return self._cached
        return WeatherStats(available=False, error=msg)
