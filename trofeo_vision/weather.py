"""Weather data fetcher (OpenWeatherMap).

Cached in memory for `refresh_seconds`. Disabled by default — needs an API key.
Activate by setting [weather] enabled = true and api_key = "<your key>" in config.toml.

Free tier: https://openweathermap.org/api/one-call-3 (1000 calls/day free).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Optional

import requests

from .config import WeatherConfig
from .sensors_types import WeatherDay, WeatherStats


log = logging.getLogger(__name__)


class WeatherClient:
    BASE_URL = "https://api.openweathermap.org/data/2.5/forecast"

    def __init__(self, cfg: WeatherConfig):
        self.cfg = cfg
        self._cached: Optional[WeatherStats] = None
        self._last_fetch = 0.0

    def get(self) -> WeatherStats:
        if not self.cfg.enabled or not self.cfg.api_key:
            return WeatherStats(available=False, error="weather disabled")
        if self._cached and (time.time() - self._last_fetch) < self.cfg.refresh_seconds:
            return self._cached
        return self._fetch()

    def _fetch(self) -> WeatherStats:
        params = {
            "q": f"{self.cfg.city},{self.cfg.country}",
            "appid": self.cfg.api_key,
            "units": self.cfg.units,
        }
        try:
            r = requests.get(self.BASE_URL, params=params, timeout=10)
        except requests.RequestException as e:
            log.warning("Weather fetch failed: %s", e)
            return self._fail(f"network error: {e}")

        if r.status_code != 200:
            return self._fail(f"HTTP {r.status_code}: {r.text[:120]}")

        try:
            data = r.json()
            self._cached = self._parse(data)
        except Exception as e:  # noqa: BLE001
            return self._fail(f"parse error: {e}")

        self._last_fetch = time.time()
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
            current_icon=current_icon,
            forecast=days,
        )

    def _fail(self, msg: str) -> WeatherStats:
        log.warning("Weather: %s", msg)
        # Keep the cache if we have one — show last known good.
        if self._cached:
            return self._cached
        return WeatherStats(available=False, error=msg)
