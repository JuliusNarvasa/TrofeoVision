"""Entry point — run the live dashboard on the Trofeo Vision.

Usage:
    python -m trofeo_vision.main              # push to device
    python -m trofeo_vision.main --dry-run    # render only, save PNG/JPEG
    python -m trofeo_vision.main --no-lhm     # psutil only (skip LibreHardwareMonitor)
    python -m trofeo_vision.main --rate 2     # 2 fps target

First time: this script will fail to claim USB if TRCC is running.
Kill TRCC first, or use --dry-run.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from pathlib import Path

from .config import Config
from .driver import Driver, DriverError
from .history import HistoryBuffer
from .renderer import Renderer
from .sensors import SensorReader
from .sensors_types import SensorSnapshot, WeatherStats, AirQualityStats
from .weather import WeatherClient, WeatherImages
from .air_quality import AirQualityClient, AirQualityImages


log = logging.getLogger("trofeo_vision")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="trofeo-vision")
    p.add_argument("--dry-run", action="store_true",
                   help="Render frames to output/, do not touch USB.")
    p.add_argument("--no-lhm", action="store_true",
                   help="Skip LibreHardwareMonitor (psutil only).")
    p.add_argument("--rate", type=float, default=None,
                   help="Target FPS (overrides config).")
    p.add_argument("--out-dir", type=Path,
                   default=Path(__file__).resolve().parent.parent / "output")
    p.add_argument("--once", action="store_true",
                   help="Render+push ONE frame then exit (works in both modes).")
    p.add_argument("--save-jpeg", action="store_true",
                   help="Also save the JPEG payload in dry-run mode.")
    p.add_argument("--diagnose", action="store_true",
                   help="Print LHM/USB/driver state at startup, then run normally.")
    return p.parse_args()


def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def snapshot_with_weather(sensors: SensorReader, weather: WeatherClient,
                          air_quality: AirQualityClient) -> SensorSnapshot:
    snap = sensors.snapshot
    if not isinstance(snap.weather, WeatherStats) or not snap.weather.available:
        snap = SensorSnapshot(
            timestamp=snap.timestamp,
            cpu=snap.cpu, gpu=snap.gpu, mem=snap.mem,
            net=snap.net,
            weather=weather.get(),
            air_quality=snap.air_quality,
        )
    if not isinstance(snap.air_quality, AirQualityStats) or not snap.air_quality.available:
        snap = SensorSnapshot(
            timestamp=snap.timestamp,
            cpu=snap.cpu, gpu=snap.gpu, mem=snap.mem,
            net=snap.net,
            weather=snap.weather,
            air_quality=air_quality.get(),
        )
    return snap


def run_dry(args, cfg: Config) -> int:
    args.out_dir.mkdir(parents=True, exist_ok=True)
    sensors = SensorReader(interval=cfg.usb.sensor_refresh_seconds, enable_lhm=not args.no_lhm)
    weather = WeatherClient(cfg.weather)
    air_quality = AirQualityClient(cfg.air_quality)
    weather_images = _make_weather_images(cfg)
    air_quality_images = _make_air_quality_images(cfg)
    history = HistoryBuffer()
    sensors.start()
    try:
        if args.once:
            time.sleep(0.5)
            snap = snapshot_with_weather(sensors, weather, air_quality)
            history.push(snap)
            r = Renderer(cfg.display.width, cfg.display.height)
            img = r.render(snap, history=history,
                           greeting_name=cfg.greeting.name,
                           weather_images=weather_images,
                           theme_cfg=cfg.theme,
                           top_tile_mode=cfg.top_tile.mode,
                           air_quality_images=air_quality_images)
            png = args.out_dir / "frame.png"
            r.save_png(png)
            log.info("Saved %s (%dx%d)", png, img.width, img.height)
            if args.save_jpeg:
                jpeg_path = args.out_dir / "frame.jpg"
                jpeg_path.write_bytes(r.to_jpeg(cfg.display.jpeg_quality))
                log.info("Saved %s (%d bytes)", jpeg_path, jpeg_path.stat().st_size)
            return 0

        # Continuous dry-run: render and save periodically.
        rate = args.rate or cfg.usb.target_fps
        sleep_s = 1.0 / max(0.1, rate)
        log.info("Dry-run at %.1f fps, saving frames to %s", rate, args.out_dir)
        idx = 0
        while True:
            snap = snapshot_with_weather(sensors, weather, air_quality)
            history.push(snap)
            r = Renderer(cfg.display.width, cfg.display.height)
            r.render(snap, history=history,
                     greeting_name=cfg.greeting.name,
                     weather_images=weather_images,
                     theme_cfg=cfg.theme,
                     top_tile_mode=cfg.top_tile.mode,
                     air_quality_images=air_quality_images)
            r.save_png(args.out_dir / f"frame_{idx:05d}.png")
            idx += 1
            if idx % 20 == 0:
                log.info("Rendered %d frames", idx)
            time.sleep(sleep_s)
    except KeyboardInterrupt:
        pass
    finally:
        sensors.stop()
    return 0


def run_live(args, cfg: Config) -> int:
    sensors = SensorReader(interval=cfg.usb.sensor_refresh_seconds, enable_lhm=not args.no_lhm)
    weather = WeatherClient(cfg.weather)
    air_quality = AirQualityClient(cfg.air_quality)
    weather_images = _make_weather_images(cfg)
    air_quality_images = _make_air_quality_images(cfg)
    history = HistoryBuffer()
    renderer = Renderer(cfg.display.width, cfg.display.height)
    driver = Driver(jpeg_quality=cfg.display.jpeg_quality)

    stop = {"flag": False}

    def _sigint(*_):
        log.info("Stopping (SIGINT)...")
        stop["flag"] = True

    signal.signal(signal.SIGINT, _sigint)
    if hasattr(signal, "SIGBREAK"):  # Windows console Ctrl+Break
        signal.signal(signal.SIGBREAK, _sigint)

    sensors.start()
    try:
        driver.open()
    except DriverError as e:
        log.error("%s", e)
        sensors.stop()
        return 1

    log.info("USB device open. Pushing frames at %.1f fps",
            args.rate or cfg.usb.target_fps)
    try:
        if args.once:
            time.sleep(0.5)  # let one sensor tick happen
            snap = snapshot_with_weather(sensors, weather, air_quality)
            history.push(snap)
            renderer.render(snap, history=history,
                            greeting_name=cfg.greeting.name,
                            weather_images=weather_images,
                            theme_cfg=cfg.theme,
                            top_tile_mode=cfg.top_tile.mode,
                            air_quality_images=air_quality_images)
            # Use driver.encode_jpeg() — rotates for the LCD orientation.
            jpeg = driver.encode_jpeg(renderer.image)
            stats = driver.push_frame(jpeg)
            log.info("One frame pushed: %d chunks, %d bytes, %.1f ms",
                     stats.chunks, stats.bytes, stats.duration_ms)
            return 0

        rate = args.rate or cfg.usb.target_fps
        sleep_s = 1.0 / max(0.1, rate)
        reconnect_backoff_s = 2.0
        log.info("Pushing frames at %.1f fps", rate)
        while not stop["flag"]:
            try:
                snap = snapshot_with_weather(sensors, weather, air_quality)
                history.push(snap)
                renderer.render(snap, history=history,
                                greeting_name=cfg.greeting.name,
                                weather_images=weather_images,
                                theme_cfg=cfg.theme,
                                top_tile_mode=cfg.top_tile.mode,
                                air_quality_images=air_quality_images)
                # Use driver.encode_jpeg() — rotates for the LCD orientation.
                jpeg = driver.encode_jpeg(renderer.image)
                stats = driver.push_frame(jpeg)
                log.debug("Frame sent: %d chunks, %d bytes, %.1f ms",
                          stats.chunks, stats.bytes, stats.duration_ms)
            except DriverError as e:
                # USB went bad mid-loop — typical after the PC sleeps and
                # the device detaches. Close + retry open() until the
                # device comes back, instead of dying.
                log.warning("USB push failed (%s) — reconnecting", e)
                try:
                    driver.close()
                except Exception:
                    log.exception("Error closing driver during reconnect")
                while not stop["flag"]:
                    time.sleep(reconnect_backoff_s)
                    if stop["flag"]:
                        break
                    try:
                        driver.open()
                        log.info("USB reconnected — resuming push")
                        break
                    except DriverError as e2:
                        log.info("USB still unavailable (%s) — retrying", e2)
                continue
            time.sleep(sleep_s)
    except KeyboardInterrupt:
        pass
    finally:
        driver.close()
        sensors.stop()
    return 0


def _make_weather_images(cfg: Config) -> WeatherImages:
    """Resolve the greeting-tile asset dir from config (with default fallback)."""
    if cfg.weather.assets_dir:
        return WeatherImages(Path(cfg.weather.assets_dir))
    return WeatherImages(WeatherImages.default_assets_dir())


def _make_air_quality_images(cfg: Config) -> AirQualityImages:
    """Resolve the air-quality tile asset dir from config (with default fallback)."""
    if cfg.air_quality.assets_dir:
        return AirQualityImages(Path(cfg.air_quality.assets_dir))
    return AirQualityImages(AirQualityImages.default_assets_dir())


def _diagnose(sensors: SensorReader, driver_kind: str = "Driver") -> None:
    """Print LHM/USB state at startup so failures are visible immediately."""
    log.info("=== DIAGNOSE ===")
    log.info("  cwd         : %s", os.getcwd())
    log.info("  LHM module  : %s", "available" if sensors.lhm_available
             else f"NOT available — {sensors.lhm_import_error}")
    log.info("  Driver      : %s", driver_kind)
    import psutil
    log.info("  psutil CPU%% : %.1f%%", psutil.cpu_percent(interval=None))
    try:
        import usb.core
        dev = usb.core.find(idVendor=0x0416, idProduct=0x5408)
        log.info("  USB device  : %s",
                 "FOUND (VID 0x0416, PID 0x5408)" if dev else "NOT FOUND")
    except Exception as e:  # noqa: BLE001
        log.info("  USB device  : error — %s", e)
    # Tiny delay to let LHM populate, then a sample read.
    time.sleep(1.5)
    snap = sensors.snapshot
    log.info("  CPU sample  : usage=%.1f%% temp=%.1f clock=%s",
             snap.cpu.usage_percent, snap.cpu.temp_c, snap.cpu.clock_str)
    log.info("  GPU sample  : usage=%.1f%% temp=%.1f power=%.1f clock=%s fan=%s vram=%s",
             snap.gpu.usage_percent, snap.gpu.temp_c, snap.gpu.power_w,
             snap.gpu.clock_str, snap.gpu.fan_str,
             f"{snap.gpu.vram_used_mb:.0f}/{snap.gpu.vram_total_mb:.0f}MB")
    log.info("=== END DIAGNOSE ===")


def main() -> int:
    args = parse_args()
    setup_logging(verbose=False)
    cfg = Config.load()
    if args.rate:
        cfg = Config(
            usb=cfg.usb,
            weather=cfg.weather,
            air_quality=cfg.air_quality,
            top_tile=cfg.top_tile,
            greeting=cfg.greeting,
            display=cfg.display,
            theme=cfg.theme,
        )  # cfg is frozen; rate is applied at runtime

    if args.dry_run:
        return run_dry(args, cfg)

    # In live mode, optionally print a startup diagnostic so LHM/USB failures
    # are visible (without it, missing GPU sensors fail silently).
    if args.diagnose:
        sensors_pre = SensorReader(interval=cfg.usb.sensor_refresh_seconds,
                                   enable_lhm=not args.no_lhm)
        sensors_pre.start()
        _diagnose(sensors_pre)
        sensors_pre.stop()
        log.info("Continuing to live mode in 2s...")
        time.sleep(0.5)

    return run_live(args, cfg)


if __name__ == "__main__":
    sys.exit(main())
