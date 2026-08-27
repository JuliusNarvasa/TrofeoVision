"""Rolling history buffer for sensor samples.

The dashboard's hero cards (CPU + GPU) show a small mixed line chart
with the last N samples of usage / temp / power. This module keeps a
tiny ring buffer that the main loop appends to every tick.

The renderer just reads from the buffer — it does not own it, so the
buffer survives across renderer instances in dry-run / live modes.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from .sensors_types import SensorSnapshot


@dataclass
class HistorySample:
    """One tick of the three values we plot per device."""

    cpu_usage: float = 0.0
    cpu_temp: float = 0.0
    cpu_power: float = 0.0
    gpu_usage: float = 0.0
    gpu_temp: float = 0.0
    gpu_power: float = 0.0


class HistoryBuffer:
    """FIFO ring buffer of recent samples, capped at ``maxlen`` entries.

    Default config targets 1 sample/second for 2 minutes (120 samples) — a
    rate limit independent of the render/push rate so the chart shows a
    stable time window even when frames go out faster (4 FPS) or slower.
    """

    def __init__(self, maxlen: int = 120, min_interval_s: float = 1.0):
        self.samples: deque[HistorySample] = deque(maxlen=maxlen)
        self.maxlen = maxlen
        self.min_interval_s = min_interval_s
        self._last_push_t: float = 0.0

    def push(self, snap: SensorSnapshot) -> bool:
        """Append one tick from a snapshot if the rate limit has elapsed.

        Returns True if a sample was added, False if skipped (too soon).
        Uses ``snap.timestamp`` so the rate is tied to sample time, not
        wall clock — replays/dry-runs stay deterministic.
        """
        if self.min_interval_s > 0 and (snap.timestamp - self._last_push_t) < self.min_interval_s:
            return False
        self._last_push_t = snap.timestamp
        self.samples.append(
            HistorySample(
                cpu_usage=snap.cpu.usage_percent,
                cpu_temp=snap.cpu.temp_c,
                cpu_power=snap.cpu.power_w,
                gpu_usage=snap.gpu.usage_percent,
                gpu_temp=snap.gpu.temp_c,
                gpu_power=snap.gpu.power_w,
            )
        )
        return True

    @property
    def seconds_covered(self) -> float:
        """Seconds of history currently held (0 when empty)."""
        return len(self.samples) * self.min_interval_s

    # ── Accessors used by the renderer ───────────────────────────────
    def cpu_tuples(self) -> list[tuple[float, float, float]]:
        """(usage%, temp°C, powerW) per sample, oldest first."""
        return [(s.cpu_usage, s.cpu_temp, s.cpu_power) for s in self.samples]

    def gpu_tuples(self) -> list[tuple[float, float, float]]:
        return [(s.gpu_usage, s.gpu_temp, s.gpu_power) for s in self.samples]

    def cpu_current(self) -> tuple[float, float, float]:
        if not self.samples:
            return (0.0, 0.0, 0.0)
        s = self.samples[-1]
        return (s.cpu_usage, s.cpu_temp, s.cpu_power)

    def gpu_current(self) -> tuple[float, float, float]:
        if not self.samples:
            return (0.0, 0.0, 0.0)
        s = self.samples[-1]
        return (s.gpu_usage, s.gpu_temp, s.gpu_power)

    def __len__(self) -> int:
        return len(self.samples)
