"""Render one frame to PNG/JPEG without touching USB. Quick visual check.

Usage:
    python scripts/render_offline.py                    # one frame
    python scripts/render_offline.py --loop             # 1 fps forever, save frames
    python scripts/render_offline.py --loop --rate 0.5  # 1 frame every 2s
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from trofeo_vision.config import Config  # noqa: E402
from trofeo_vision.history import HistoryBuffer  # noqa: E402
from trofeo_vision.renderer import Renderer  # noqa: E402
from trofeo_vision.sensors import SensorReader  # noqa: E402
from trofeo_vision.sensors_types import SensorSnapshot, WeatherStats  # noqa: E402
from trofeo_vision.weather import WeatherClient, WeatherImages  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--loop", action="store_true")
    p.add_argument("--rate", type=float, default=1.0)
    p.add_argument("--no-lhm", action="store_true")
    p.add_argument("--save-jpeg", action="store_true")
    p.add_argument("--out", type=Path, default=ROOT / "output")
    args = p.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    cfg = Config.load()
    sensors = SensorReader(interval=0.5, enable_lhm=not args.no_lhm)
    weather = WeatherClient(cfg.weather)
    assets_dir = (Path(cfg.weather.assets_dir) if cfg.weather.assets_dir
                  else WeatherImages.default_assets_dir())
    weather_images = WeatherImages(assets_dir)
    history = HistoryBuffer()
    sensors.start()
    time.sleep(0.3)  # let one tick happen

    renderer = Renderer(cfg.display.width, cfg.display.height)
    idx = 0
    try:
        while True:
            snap = sensors.snapshot
            if not snap.weather.available:
                snap = SensorSnapshot(
                    timestamp=snap.timestamp,
                    cpu=snap.cpu, gpu=snap.gpu, mem=snap.mem,
                    net=snap.net,
                    weather=weather.get(),
                )
            history.push(snap)
            renderer.render(snap, history=history,
                            greeting_name=cfg.greeting.name,
                            weather_images=weather_images)
            png_path = args.out / ("frame_latest.png" if not args.loop else f"frame_{idx:05d}.png")
            renderer.save_png(png_path)
            print(f"[{idx:05d}] saved {png_path}")
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
