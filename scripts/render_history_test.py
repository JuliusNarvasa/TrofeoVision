"""Smoke test for the new history chart — uses synthetic samples.

We don't need real sensors to verify the chart renders correctly; this
script just feeds a sine-wave + spike pattern into the HistoryBuffer
and writes one PNG so we can eyeball the result.
"""

import math
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from trofeo_vision.config import Config  # noqa: E402
from trofeo_vision.history import HistoryBuffer  # noqa: E402
from trofeo_vision.renderer import Renderer  # noqa: E402
from trofeo_vision.sensors_types import (  # noqa: E402
    CpuStats, GpuStats, MemDiskStats, NetStats,
    SensorSnapshot, WeatherStats,
)


def make_snap(t: float, jitter: bool = True) -> SensorSnapshot:
    """Build a synthetic snapshot with realistic-ish waveforms."""
    # 60-sample cycle ≈ sine wave for each metric, with a slow load pattern
    cpu_usage = 30 + 20 * math.sin(t * 0.15) + random.uniform(-3, 3)
    cpu_temp = 55 + 8 * math.sin(t * 0.10) + random.uniform(-1, 1)
    cpu_power = 60 + 25 * math.sin(t * 0.12) + random.uniform(-3, 3)
    gpu_usage = 45 + 30 * (0.5 + 0.5 * math.sin(t * 0.20)) + random.uniform(-4, 4)
    gpu_temp = 60 + 12 * math.sin(t * 0.13) + random.uniform(-1, 1)
    gpu_power = 90 + 60 * (0.5 + 0.5 * math.sin(t * 0.18)) + random.uniform(-5, 5)
    # Add a one-time spike at t=30 to make the chart interesting
    if 28 <= t <= 33:
        cpu_power += 50
        gpu_power += 80
    return SensorSnapshot(
        timestamp=time.time() + t,
        cpu=CpuStats(
            usage_percent=max(0, min(100, cpu_usage)),
            temp_c=max(0, cpu_temp),
            power_w=max(0, cpu_power),
            fan_rpm=1200, clock_mhz=4200,
            pump_rpm=2400, voltage_v=1.20,
        ),
        gpu=GpuStats(
            usage_percent=max(0, min(100, gpu_usage)),
            temp_c=max(0, gpu_temp),
            power_w=max(0, gpu_power),
            fan_rpm=1400, clock_mhz=2100,
            vram_used_mb=2400, vram_total_mb=16000, voltage_v=0.85,
        ),
        mem=MemDiskStats(ram_percent=42, ram_used_gb=13.5, ram_total_gb=32,
                         disk_percent=58, disk_used_gb=580, disk_total_gb=1000),
        net=NetStats(up_bps=200_000, down_bps=1_500_000),
        weather=WeatherStats(available=False, error="weather disabled"),
    )


def main() -> int:
    cfg = Config.load()
    history = HistoryBuffer(maxlen=60)

    # Simulate 60 ticks (~15s at 4fps)
    for t in range(60):
        history.push(make_snap(float(t)))

    snap = make_snap(60.0)
    r = Renderer(cfg.display.width, cfg.display.height)
    r.render(snap, history=history)

    out = ROOT / "output" / "frame_history_test.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    r.save_png(out)
    print(f"saved {out}  buffer_len={len(history)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
