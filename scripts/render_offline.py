"""Render one frame to PNG/JPEG without touching USB. Quick visual check.

Usage:
    python scripts/render_offline.py                    # one frame (theme from config)
    python scripts/render_offline.py --loop             # 1 fps forever, save frames
    python scripts/render_offline.py --loop --rate 0.5  # 1 frame every 2s
    python scripts/render_offline.py --theme dark       # force dark theme (testing)
    python scripts/render_offline.py --theme light      # force light theme (testing)
    python scripts/render_offline.py --top-tile air_quality   # flip to AQI tile
    python scripts/render_offline.py --top-tile weather       # (default)
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from trofeo_vision.config import Config, ThemeConfig, TopTileConfig  # noqa: E402
from trofeo_vision.history import HistoryBuffer  # noqa: E402
from trofeo_vision.renderer import Renderer  # noqa: E402
from trofeo_vision.sensors import SensorReader  # noqa: E402
from trofeo_vision.sensors_types import (  # noqa: E402
    AirQualityStats,
    SensorSnapshot,
    WeatherStats,
)
from trofeo_vision.weather import WeatherClient, WeatherImages  # noqa: E402
from trofeo_vision.air_quality import (  # noqa: E402
    AirQualityClient,
    AirQualityImages,
)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--loop", action="store_true")
    p.add_argument("--rate", type=float, default=1.0)
    p.add_argument("--no-lhm", action="store_true")
    p.add_argument("--save-jpeg", action="store_true")
    p.add_argument("--out", type=Path, default=ROOT / "output")
    p.add_argument("--theme", choices=("auto", "light", "dark"),
                   help="Override theme mode from config (default: use config).")
    p.add_argument("--top-tile", choices=("weather", "air_quality"),
                   help="Override top-tile mode from config (default: use config).")
    args = p.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    cfg = Config.load()
    # Apply CLI overrides on top of the loaded config.
    theme = cfg.theme
    if args.theme:
        theme = ThemeConfig(mode=args.theme,
                            dark_start_hour=cfg.theme.dark_start_hour,
                            dark_end_hour=cfg.theme.dark_end_hour)
    top_tile_mode = cfg.top_tile.mode
    if args.top_tile:
        top_tile_mode = args.top_tile
    cfg = Config(
        usb=cfg.usb, weather=cfg.weather, air_quality=cfg.air_quality,
        greeting=cfg.greeting, display=cfg.display, theme=theme,
        top_tile=TopTileConfig(mode=top_tile_mode),
    )
    sensors = SensorReader(interval=0.5, enable_lhm=not args.no_lhm)
    weather = WeatherClient(cfg.weather)
    air_quality = AirQualityClient(cfg.air_quality)
    weather_assets = (Path(cfg.weather.assets_dir) if cfg.weather.assets_dir
                      else WeatherImages.default_assets_dir())
    aq_assets = (Path(cfg.air_quality.assets_dir) if cfg.air_quality.assets_dir
                 else AirQualityImages.default_assets_dir())
    weather_images = WeatherImages(weather_assets)
    air_quality_images = AirQualityImages(aq_assets)
    history = HistoryBuffer()
    sensors.start()
    time.sleep(0.3)  # let one tick happen

    renderer = Renderer(cfg.display.width, cfg.display.height)
    idx = 0
    try:
        while True:
            snap = sensors.snapshot
            # Fill weather + air quality on the snapshot if the sensor
            # reader didn't already have a live value for this frame.
            if not snap.weather.available:
                snap = SensorSnapshot(
                    timestamp=snap.timestamp,
                    cpu=snap.cpu, gpu=snap.gpu, mem=snap.mem,
                    net=snap.net,
                    weather=weather.get(),
                    air_quality=snap.air_quality,
                )
            if not snap.air_quality.available:
                snap = SensorSnapshot(
                    timestamp=snap.timestamp,
                    cpu=snap.cpu, gpu=snap.gpu, mem=snap.mem,
                    net=snap.net,
                    weather=snap.weather,
                    air_quality=air_quality.get(),
                )
            history.push(snap)
            renderer.render(snap, history=history,
                            greeting_name=cfg.greeting.name,
                            weather_images=weather_images,
                            theme_cfg=cfg.theme,
                            top_tile_mode=cfg.top_tile.mode,
                            air_quality_images=air_quality_images)
            png_path = args.out / ("frame_latest.png" if not args.loop else f"frame_{idx:05d}.png")
            renderer.save_png(png_path)
            print(f"[{idx:05d}] saved {png_path}  (top tile: {cfg.top_tile.mode})")
            if args.save_jpeg:
                jpg_path = args.out / (png_path.stem + ".jpg")
                jpg_path.write_bytes(renderer.to_jpeg(cfg.display.jpeg_quality))
                print(f"        saved {jpg_path}")
            idx += 1
            if not args.loop:
                break
            time.sleep(1.0 / max(0.05, args.rate))
    except KeyboardInterrupt:
        pass
    finally:
        sensors.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
