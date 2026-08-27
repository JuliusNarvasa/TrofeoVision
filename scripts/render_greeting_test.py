"""Render a single test frame with a mocked WeatherStats so we can eyeball
the greeting tile when OpenWeatherMap is actually returning data.

Doesn't touch sensors, USB, or any network. Run:
    python scripts/render_greeting_test.py [--with-weather|--no-weather]
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from trofeo_vision.history import HistoryBuffer  # noqa: E402
from trofeo_vision.renderer import Renderer  # noqa: E402
from trofeo_vision.sensors_types import (  # noqa: E402
    SensorSnapshot,
    WeatherDay,
    WeatherStats,
)
from trofeo_vision.weather import WeatherImages  # noqa: E402


def _make_snapshot(with_weather: bool, hour: int | None = None) -> SensorSnapshot:
    """Build a static snapshot — zeros for sensors, mocked weather if asked."""
    if hour is None:
        hour = datetime.now().hour
    ts = datetime.now().replace(hour=hour, minute=37, second=0, microsecond=0).timestamp()
    wx = WeatherStats(available=False)
    if with_weather:
        wx = WeatherStats(
            available=True,
            location="Catalunan Grande",
            current_temp_c=26.0,
            current_icon="02d",  # partly cloudy
            forecast=[
                WeatherDay(label="Today", high_c=30.0, low_c=24.0, icon_code="02d"),
                WeatherDay(label="Tue",   high_c=31.0, low_c=25.0, icon_code="10d"),
                WeatherDay(label="Wed",   high_c=29.0, low_c=24.0, icon_code="11d"),
            ],
        )
    return SensorSnapshot(timestamp=ts, weather=wx)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--with-weather", action="store_true",
                   help="Mock an enabled WeatherStats so the tile shows real data.")
    p.add_argument("--no-weather", action="store_true",
                   help="Render the disabled-weather fallback path.")
    p.add_argument("--hour", type=int, default=None,
                   help="Force the local-time hour (0-23) so you can preview "
                        "each time-of-day bucket without waiting. Default: now.")
    p.add_argument("--out", type=Path, default=ROOT / "output")
    args = p.parse_args()

    if not (args.with_weather or args.no_weather):
        args.with_weather = True  # default to the more interesting path

    args.out.mkdir(parents=True, exist_ok=True)
    snap = _make_snapshot(args.with_weather, hour=args.hour)
    history = HistoryBuffer()
    history.push(snap)

    images = WeatherImages(WeatherImages.default_assets_dir())
    renderer = Renderer()
    renderer.render(snap, history=history,
                    greeting_name="Bambaw",
                    weather_images=images)

    tag = "with_weather" if args.with_weather else "no_weather"
    bucket_hour = snap.timestamp  # the snapshot timestamp encodes the bucket
    dt = datetime.fromtimestamp(bucket_hour)
    fname = f"greeting_{tag}_h{dt.hour:02d}.png"
    out = args.out / fname
    renderer.save_png(out)
    jpg = args.out / (out.stem + ".jpg")
    jpg.write_bytes(renderer.to_jpeg(quality=85))
    print(f"saved {out}  ({dt.strftime('%H:%M %p')} — "
          f"bucket={'on the hour' if dt.minute == 0 else 'arbitrary'})")
    print(f"saved {jpg}")
    return 0


if __name__ == "__main__":
    sys.exit(main())