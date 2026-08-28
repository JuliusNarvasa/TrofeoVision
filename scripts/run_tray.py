"""Tray entry point — launched by ``start.bat`` via ``pythonw.exe``.

Has no console (pythonw.exe), no CLI surface, no logging of its own:
just constructs the ``TrayApp`` and lets it block on the pystray
message loop. All process behavior lives in ``trofeo_vision.tray``.

Run directly (debugging)::
    python scripts/run_tray.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from trofeo_vision.config import Config  # noqa: E402
from trofeo_vision.tray import TrayApp  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description="Trofeo Vision tray launcher")
    p.add_argument("--rate", type=float, default=None,
                   help="Target FPS (overrides config).")
    p.add_argument("--verbose", action="store_true",
                   help="DEBUG-level logging.")
    args = p.parse_args()
    cfg = Config.load()
    TrayApp(cfg, rate=args.rate, verbose=args.verbose).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
