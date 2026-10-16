"""Air quality data fetcher + tile-image loader (Open-Meteo Air Quality API).

Cached in memory for ``refresh_seconds`` (default 30 min — pollutants move
slower than weather). Disabled by default; activate by setting
``[air_quality] enabled = true`` in config.toml.

Open-Meteo's Air Quality API was chosen after testing both WAQI and the
OpenWeatherMap Air Pollution API:

* **OWM Air Pollution** — sparse coverage in Mindanao, defaults to a clean
  baseline where no station exists. We previously used this and saw AQI 1
  for Davao on a day with visible regional haze.
* **WAQI (aqicn.org)** — much better coverage in SEA *where stations
  exist*, but the Philippines registration is sparse and we couldn't find
  a Davao station. Token-based, free for ~1000 calls/day.
* **Open-Meteo Air Quality** (current) — global coverage backed by the
  Copernicus Atmosphere Monitoring Service (CAMS) model. Has data for
  every coordinate on Earth (including Davao at the exact coords we want).
  Free for non-commercial use, **no API key required**.

API call (two-step, like the previous WAQI/OWM clients)::

    1.  https://geocoding-api.open-meteo.com/v1/search
        ?name=Davao&countryCode=PH&count=1&language=en&format=json
        → lat / lon + display name
    2.  https://air-quality-api.open-meteo.com/v1/air-quality
        ?latitude=7.07&longitude=125.61
        &current=us_aqi,pm10,pm2_5,ozone,nitrogen_dioxide
        &timezone=auto
        → us_aqi (0–500) + current pollutant concentrations in μg/m³

The geocode step is cached in memory (city + country rarely change) so
subsequent air-quality calls skip step 1 entirely.

Also provides ``AirQualityImages``, the greeting-tile background loader
for ``assets/air_quality/<state>_<bucket>.png``. 5 AQI states × 5 time
buckets = 25 files. Missing files fall back to a generated category-color
gradient so the tile looks intentional on a fresh install.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests
from PIL import Image, ImageDraw

from .config import AirQualityConfig
from .sensors_types import AirQualityStats


log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# AQI states + bucket mapping (US AQI breakpoints)
# ─────────────────────────────────────────────────────────────────────────────
#
# 5 AQI states × 5 time-of-day buckets = 25 background files, named:
#     <state>_<bucket>.png  (e.g. "good_morning.png", "very_poor_night.png")
#
# AQI states in canonical order. WAQI returns a numeric US AQI (0–500);
# we bucket it into one of these five states for the background gradient
# + tile color theme.

AQI_STATES = (
    "good",       # US AQI   0–50
    "fair",       # US AQI  51–100  ("Moderate")
    "moderate",   # US AQI 101–150  ("Unhealthy for Sensitive Groups")
    "poor",       # US AQI 151–200  ("Unhealthy")
    "very_poor",  # US AQI 201+     ("Very Unhealthy" / "Hazardous")
)

# Bucket bounds match WeatherImages' time_bucket() so the two tiles have
# consistent time-of-day handling — air quality uses the same "morning" /
# "noon" / "afternoon" / "evening" / "night" buckets.
TIME_BUCKETS = ("morning", "noon", "afternoon", "evening", "night")

# US AQI breakpoints → (human label, our 5-state bucket).
# "Unhealthy (Sensitive)" is shorter than the official
# "Unhealthy for Sensitive Groups" — saves horizontal pixels on the
# tile label while preserving the meaning. The official long form is
# documented in AGENTS.md for anyone who wants the verbatim category.
_US_AQI_BREAKPOINTS: tuple[tuple[int, int, str, str], ...] = (
    (0,   50,  "Good",                       "good"),
    (51,  100, "Moderate",                   "fair"),
    (101, 150, "Unhealthy (Sensitive)",      "moderate"),
    (151, 200, "Unhealthy",                  "poor"),
    (201, 300, "Very Unhealthy",             "very_poor"),
    (301, 999, "Hazardous",                  "very_poor"),
)

# Background gradient anchor color per state — used both as a fallback
# when ``<state>_<bucket>.png`` is missing AND as the loading-screen
# color before the first fetch completes. Colors are tuned to read
# well behind the readability gradient the renderer applies.
_AQI_STATE_COLORS: dict[str, tuple[int, int, int]] = {
    "good":      (60, 165, 145),    # teal-green
    "fair":      (155, 195, 90),    # yellow-green
    "moderate":  (225, 180, 70),    # warm yellow
    "poor":      (215, 115, 60),    # orange
    "very_poor": (180, 60, 60),     # deep red
}

_FALLBACK_STATE = "good"
_FALLBACK_BUCKET = "morning"


def _bucket_for_aqi(aqi: int) -> tuple[str, str]:
    """Numeric US AQI → (human label, our 5-state bucket).

    Out-of-range (negative / NaN-as-string from WAQI's "-" sentinel)
    falls back to ``("—", "good")`` so the renderer never crashes on a
    transient bad value.
    """
    try:
        n = int(aqi)
    except (TypeError, ValueError):
        return "—", _FALLBACK_STATE
    for lo, hi, label, state in _US_AQI_BREAKPOINTS:
        if lo <= n <= hi:
            return label, state
    return "Hazardous" if n > 999 else "—", _FALLBACK_STATE


def aqi_to_state(aqi: int) -> str:
    """Numeric US AQI → one of AQI_STATES (just the bucket name).

    See ``aqi_category_label`` if you also want the human label.
    """
    return _bucket_for_aqi(aqi)[1]


def aqi_category_label(aqi: int) -> str:
    """Numeric US AQI → short human label for the renderer.

    Returns "—" for invalid inputs (negative, non-numeric) so the tile
    never renders a garbage string. WAQI sometimes returns the literal
    string "-" when no station has reported yet — we treat that as
    unavailable.
    """
    return _bucket_for_aqi(aqi)[0]


def time_bucket(dt: datetime) -> str:
    """Local-time → bucket name. Identical to weather.time_bucket() so
    weather + air quality can share a single time-of-day helper in the
    future. Kept duplicated here so the modules stay independently
    importable (weather.py stays usable even if air_quality.py isn't).
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
    """Same "Good evening" / "Good morning" mapping as the weather tile.

    Lives here too so the renderer doesn't need a weather import when
    showing the air quality mode. Night falls back to "Good evening".
    """
    return {
        "morning":   "Good morning",
        "noon":      "Good noon",
        "afternoon": "Good afternoon",
        "evening":   "Good evening",
        "night":     "Good evening",
    }.get(bucket, "Hello")


# ─────────────────────────────────────────────────────────────────────────────
# Background image loader
# ─────────────────────────────────────────────────────────────────────────────
class AirQualityImages:
    """Lazy loader + cache for greeting-tile backgrounds.

    Resolves (state, bucket) → a PIL.Image by reading
    ``<state>_<bucket>.png`` from ``assets_dir``. Missing files fall back
    to ``good_morning.png``, then to a generated category-color gradient
    so the renderer never crashes on a partial asset set.

    Generated fallbacks: a 462×280 vertical gradient from black to the
    state's anchor color, so even with zero PNGs the tile looks
    category-appropriate (good → teal, very_poor → red).
    """

    # Native greeting-tile size — must match weather.py so the renderer's
    # _paste_rounded works without a per-call resize.
    TILE_W, TILE_H = 462, 280

    def __init__(self, assets_dir: Path):
        self.assets_dir = Path(assets_dir)
        self._cache: dict[tuple[str, str], Image.Image] = {}
        self._generated: dict[tuple[str, str], Image.Image] = {}

    def get(self, state: str, bucket: str) -> Image.Image:
        """Return the background image for (state, bucket).

        State is normalized to a known set so an out-of-range AQI can't
        poison the cache. Cache key is the requested combo — once a real
        file is loaded we keep reusing it; a missing-file fallback is
        also cached so we don't keep spamming the warning log every frame.
        """
        state = state if state in AQI_STATES else _FALLBACK_STATE
        bucket = bucket if bucket in TIME_BUCKETS else _FALLBACK_BUCKET
        key = (state, bucket)
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        path = self.assets_dir / f"{state}_{bucket}.png"
        if path.exists():
            try:
                img = Image.open(path).convert("RGB")
            except Exception as e:  # noqa: BLE001
                log.warning("Air quality image unreadable: %s (%s) — using "
                            "generated fallback", path, e)
                img = self._generate(state)
        else:
            # First lookup for this combo; cache the generated fallback so
            # subsequent frames skip the warning log.
            if (state, bucket) != (_FALLBACK_STATE, _FALLBACK_BUCKET):
                log.warning("Air quality image missing: %s — using generated "
                            "fallback", path)
            # Try good_morning before going fully generated.
            if (state, bucket) != (_FALLBACK_STATE, _FALLBACK_BUCKET):
                fallback_path = self.assets_dir / f"{_FALLBACK_STATE}_{_FALLBACK_BUCKET}.png"
                if fallback_path.exists():
                    try:
                        img = Image.open(fallback_path).convert("RGB")
                        self._cache[key] = img
                        return img
                    except Exception:  # noqa: BLE001
                        pass
            img = self._generate(state)
        self._cache[key] = img
        return img

    def _generate(self, state: str) -> Image.Image:
        """Build a category-color vertical gradient as a placeholder.

        Cached per state — there are only 5 unique gradients regardless
        of which bucket triggered the fallback.
        """
        cache_key = ("__generated__", state)
        cached = self._generated.get(cache_key)
        if cached is not None:
            return cached
        anchor = _AQI_STATE_COLORS.get(state, _AQI_STATE_COLORS[_FALLBACK_STATE])
        w, h = self.TILE_W, self.TILE_H
        img = Image.new("RGB", (w, h), (24, 24, 28))
        d = ImageDraw.Draw(img)
        # Top→bottom gradient from near-black to the anchor color.
        for row in range(h):
            ratio = row / max(1, h - 1)
            r = int(24 + (anchor[0] - 24) * ratio)
            g = int(24 + (anchor[1] - 24) * ratio)
            b = int(24 + (anchor[2] - 24) * ratio)
            d.line([(0, row), (w, row)], fill=(r, g, b))
        self._generated[cache_key] = img
        return img

    @staticmethod
    def default_assets_dir() -> Path:
        """The canonical assets folder: <repo>/assets/air_quality/.

        Mirrors WeatherImages.default_assets_dir() — config.py lives at
        ``<repo>/trofeo_vision/``; parents[1] is the repo root.
        """
        return Path(__file__).resolve().parent.parent / "assets" / "air_quality"


# ─────────────────────────────────────────────────────────────────────────────
# API client
# ─────────────────────────────────────────────────────────────────────────────
class AirQualityClient:
    """Two-step Open-Meteo fetch: geocode → air quality.

    Geocode result is cached in memory (city + country rarely change),
    so subsequent air-quality calls skip step 1 entirely. Air-quality
    results are also cached for ``refresh_seconds`` so a 2 FPS render
    loop hits the API at most ~48 times/day (geocode) and once every
    30 min (air quality) by default.

    No API key required — Open-Meteo is free for non-commercial use.
    Country disambiguation is via the ISO-3166-1 alpha2 ``countryCode``
    parameter on the geocoding endpoint (e.g. "PH"), so "Davao" always
    resolves to Davao City Philippines instead of Davao Costa Rica.
    """

    GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
    AIR_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
    # Pollutants + US AQI for the headline. Open-Meteo separately returns
    # european_aqi which would also work, but US AQI is what most people
    # reading AirNow / AirVisual are used to.
    CURRENT_FIELDS = "us_aqi,pm10,pm2_5,ozone,nitrogen_dioxide"
    # Short timeout so a dead API stalls one frame for ~5 s max instead of
    # 10 s+. Per-frame refetch callers only refetch while `not available`,
    # and the failure backoff below keeps a down API from stalling frames.
    REQUEST_TIMEOUT_S = 5.0
    # Negative cache: after a failure, serve the placeholder (or last-good
    # cache) immediately without hitting the network for this long.
    FAILURE_BACKOFF_S = 60.0

    def __init__(self, cfg: AirQualityConfig):
        self.cfg = cfg
        self._cached: Optional[AirQualityStats] = None
        self._last_fetch = 0.0
        self._last_failure = 0.0
        self._last_error = ""
        # Geocode cache: (city, country) → (lat, lon, display_name).
        # Country filter is what keeps "Davao" pointing at the PH one.
        self._geo_cache: dict[tuple[str, str], tuple[float, float, str]] = {}

    def get(self) -> AirQualityStats:
        """Return cached air quality, refreshing when stale. Never raises —
        on any failure path returns last-good cache or an
        ``available=False`` placeholder so the frame loop keeps pushing.
        """
        try:
            if not self.cfg.enabled:
                return AirQualityStats(available=False, error="air quality disabled")
            now = time.time()
            if self._cached and (now - self._last_fetch) < self.cfg.refresh_seconds:
                return self._cached
            # Negative cache: don't hammer a dead API every frame.
            if (self._last_failure
                    and (now - self._last_failure) < self.FAILURE_BACKOFF_S):
                if self._cached:
                    return self._cached
                return AirQualityStats(available=False, error=self._last_error or "backing off")
            return self._fetch()
        except Exception as e:  # noqa: BLE001 — get() must never raise
            log.warning("Air quality get() unexpected error: %s", e)
            if self._cached:
                return self._cached
            return AirQualityStats(available=False, error=f"unexpected: {e}")

    def _fetch(self) -> AirQualityStats:
        try:
            lat, lon, location = self._geocode()
        except _AirQualityError as e:
            return self._fail(f"geocode: {e}")
        except Exception as e:  # noqa: BLE001 — _geocode must never escape raw
            return self._fail(f"geocode unexpected: {e}")

        params = {
            "latitude": f"{lat:.4f}",
            "longitude": f"{lon:.4f}",
            "current": self.CURRENT_FIELDS,
            "timezone": "auto",
        }
        try:
            r = requests.get(self.AIR_URL, params=params,
                             timeout=self.REQUEST_TIMEOUT_S)
        except requests.RequestException as e:
            log.warning("Air quality fetch failed: %s", e)
            return self._fail(f"network error: {e}")
        except Exception as e:  # noqa: BLE001 — never let _fetch raise
            log.warning("Air quality fetch unexpected error: %s", e)
            return self._fail(f"unexpected: {e}")

        if r.status_code != 200:
            try:
                body = r.text[:120]
            except Exception:  # noqa: BLE001
                body = "<unreadable body>"
            return self._fail(f"HTTP {r.status_code}: {body}")

        try:
            payload = r.json()
            self._cached = self._parse(payload, location)
        except Exception as e:  # noqa: BLE001
            return self._fail(f"parse error: {e}")

        self._last_fetch = time.time()
        self._last_failure = 0.0
        return self._cached

    def _geocode(self) -> tuple[float, float, str]:
        """Resolve ``cfg.city`` + ``cfg.country`` → (lat, lon, display name).

        Open-Meteo's geocoding API is free + no key. Cached by
        (city, country) so repeated air-quality calls skip this.
        """
        key = (self.cfg.city, self.cfg.country)
        cached = self._geo_cache.get(key)
        if cached is not None:
            return cached
        city = self.cfg.city or "Davao"
        params = {
            "name": city,
            "count": 1,
            "language": "en",
            "format": "json",
        }
        # Open-Meteo accepts ISO-3166-1 alpha2 country codes via
        # countryCode. Empty country means "any country" — fine for
        # unambiguous names like "Manila", but "Davao" needs PH to
        # avoid the Costa Rica match.
        if self.cfg.country:
            params["countryCode"] = self.cfg.country
        try:
            r = requests.get(self.GEO_URL, params=params,
                             timeout=self.REQUEST_TIMEOUT_S)
        except requests.RequestException as e:
            raise _AirQualityError(f"network error: {e}") from e
        if r.status_code != 200:
            try:
                body = r.text[:120]
            except Exception:  # noqa: BLE001
                body = "<unreadable body>"
            raise _AirQualityError(f"HTTP {r.status_code}: {body}")
        try:
            results = r.json().get("results") or []
        except Exception as e:  # noqa: BLE001 — malformed body, not a crash
            raise _AirQualityError(f"unparseable geocode body: {e}") from e
        if not results:
            raise _AirQualityError(
                f"no geocoding results for {city}"
                + (f", {self.cfg.country}" if self.cfg.country else "")
            )
        first = results[0]
        try:
            lat = float(first["latitude"])
            lon = float(first["longitude"])
        except (KeyError, TypeError, ValueError) as e:
            raise _AirQualityError(f"bad geocode coords: {e}") from e
        display = (
            f"{first.get('name', city)}, {first.get('country', self.cfg.country)}"
        )
        out = (lat, lon, display)
        self._geo_cache[key] = out
        return out

    def _parse(self, payload: dict, location: str) -> AirQualityStats:
        """Translate Open-Meteo's air-quality response to AirQualityStats.

        Open-Meteo returns ``current.us_aqi`` as either an int or ``None``
        (when their model has no current reading for that coord — rare but
        possible). Anything that isn't a usable int becomes an
        unavailable reading with the error message preserved.
        """
        current = payload.get("current") or {}
        aqi_raw = current.get("us_aqi")
        if aqi_raw is None:
            return AirQualityStats(
                available=False,
                location=location,
                error="no current AQI reading from CAMS model",
            )
        try:
            aqi = int(aqi_raw)
        except (TypeError, ValueError):
            return AirQualityStats(
                available=False,
                location=location,
                error=f"unparseable AQI value: {aqi_raw!r}",
            )
        _, state = _bucket_for_aqi(aqi)

        # Pollutant values are direct floats (no nested "v" wrapper as
        # WAQI had). Missing fields default to 0.0 so the renderer never
        # crashes on an incomplete reading.
        pm25 = float(current.get("pm2_5") or 0.0)
        pm10 = float(current.get("pm10") or 0.0)
        ozone = float(current.get("ozone") or 0.0)
        no2 = float(current.get("nitrogen_dioxide") or 0.0)

        return AirQualityStats(
            available=True,
            location=location,
            aqi=aqi,
            aqi_label=state,
            pm25=pm25,
            pm10=pm10,
            components={
                "pm2_5": pm25,
                "pm10": pm10,
                "ozone": ozone,
                "nitrogen_dioxide": no2,
                "us_aqi": aqi,
            },
        )

    def _fail(self, msg: str) -> AirQualityStats:
        log.warning("Air quality: %s", msg)
        self._last_failure = time.time()
        self._last_error = msg
        if self._cached:
            return self._cached
        return AirQualityStats(available=False, error=msg)


class _AirQualityError(Exception):
    """Internal error marker so geocode failures don't get re-classified
    as network errors by the outer ``_fetch`` handler.
    """