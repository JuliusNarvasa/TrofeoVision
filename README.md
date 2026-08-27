# Trofeo Vision — Live Dashboard

Custom Python renderer + USB driver for the **Thermalright Trofeo Vision 9.16"**
(462×1920 portrait LCD). Pushes a live hardware-monitoring dashboard to the
device via WinUSB, bypassing the official TRCC software entirely.

## Status (2026-08-27)

- ✅ Offline renderer produces a 462×1920 PNG/JPEG that matches the reference style
- ✅ psutil live data: CPU%, RAM, ROM, network up/down, time
- ✅ USB driver ported (LY Bulk protocol, VID 0x0416 / PID 0x5408)
- ✅ Weather client stub (OpenWeatherMap, disabled by default)
- ✅ Dry-run mode — render to disk, no USB access
- ✅ LibreHardwareMonitor — working via Pythonnet + `AssemblyResolve` + bundled DLLs + PawnIO (`assets/`)
- ⏸️ Hardware test — needs TRCC closed and user permission to take over USB

## Quick start

```powershell
# Install deps
pip install -r requirements.txt

# Render one frame to output/frame_latest.png (no USB, safe)
python scripts/render_offline.py --no-lhm --save-jpeg

# Push to the device (TRCC must be closed first)
python -m trofeo_vision.main --rate 4
```

## Architecture

```
trofeo_vision/
├── __init__.py
├── config.py        # Config dataclass + config.toml loader
├── renderer.py      # PIL ImageDraw — turns SensorSnapshot → 462x1920 RGB
├── sensors.py       # SensorReader thread (psutil + optional LHM)
├── sensors_types.py # Frozen dataclasses: CpuStats, GpuStats, WeatherStats...
├── driver.py        # LY Bulk USB driver (handshake + chunked JPEG push)
├── weather.py       # OpenWeatherMap client (10-min cache)
└── main.py          # Entry point — render + push loop

scripts/
└── render_offline.py  # Dry-run script: render to PNG/JPEG without USB
```

## Protocol reference

The LY Bulk protocol (used by the 9.16" Trofeo Vision) was reverse-engineered
from `Lexonight1/thermalright-trcc-linux` and `Abigor-DIY/Open-Trofeo-LCD`:

- **VID:PID**: `0x0416:0x5408`
- **Endpoints**: `0x09` OUT (bulk), `0x81` IN (bulk)
- **Frame format**: 462×1920 baseline JPEG, 4:4:4 subsampling
- **Chunk format**: 512-byte USB transfers (16-byte header + 496-byte payload)
- **Burst**: 8 chunks per write = 4096-byte USB transfers
- **Handshake**: send 2048-byte init packet, read 512-byte ACK

See `trofeo_vision/driver.py` for the full implementation with header layout.

## Sensors

| Field | Source | Notes |
|---|---|---|
| CPU usage | `psutil.cpu_percent` | First sample is 0; second tick is accurate |
| RAM | `psutil.virtual_memory` | % + GB used |
| ROM | `psutil.disk_usage('/')` | % + GB used |
| Network | `psutil.net_io_counters()` delta | Bytes/s auto-scaled (B/K/M/G) |
| Time | `datetime.now()` | Local time |
| CPU temp/power/clock/fan/voltage | LibreHardwareMonitor | Needs admin + PawnIO driver |
| GPU usage/temp/power/clock/fan/VRAM | LibreHardwareMonitor | AMD RX 7800 XT supported via ADL |
| Weather | OpenWeatherMap | Set `weather.api_key` in `config.toml` |

**LHM caveat:** The `LibreHardwareMonitorLib.dll` NuGet package has transitive
deps (System.Memory v4.0.5, etc.) that aren't bundled. Easiest fix is to use
the [rpa3ck/ThermalEngine](https://github.com/rpa3ck/ThermalEngine) approach:
run a standalone `SensorHelperApp.exe` that talks LHM and exposes sensors
via HTTP/JSON, then have this app call it as a sidecar.

## Driver binding (one-time)

If pyusb can't see the device after TRCC was installed:

1. Download [Zadig](https://zadig.akeo.ie/)
2. `Options → List All Devices`
3. Find `0416:5408` (Winbond Electronics USBDISPLAY)
4. Select **WinUSB** driver → `Replace Driver`

After this, `python -c "import usb.core; print(usb.core.find(idVendor=0x0416, idProduct=0x5408))"`
should print the device.

## Configuration

Create `trofeo_vision/config.toml`:

```toml
[usb]
vendor_id = 0x0416
product_id = 0x5408
target_fps = 4.0
sensor_refresh_seconds = 1.0

[weather]
enabled = false
api_key = ""        # https://openweathermap.org/api/one-call-3
city = "Manila"
country = "PH"
units = "metric"

[display]
width = 462
height = 1920
jpeg_quality = 85
```

## Why this exists

The user's custom character themes live at
`C:\TRCCCAP\Data\USBLCD\Theme4621920\` — TRCC's theme format is image-only.
There's no live data binding beyond limited time/date text overlays in the
`config1.dc` binary files. This project replaces TRCC entirely with a Python
renderer that has full design freedom.

## Files of note

- `output/frame_latest.png` — last rendered frame (PNG, full quality)
- `output/frame_latest.jpg` — same frame as JPEG (what would be pushed)
- `trofeo_vision/driver.py` — only file that touches USB; everything else
  is unit-testable on its own
- `scripts/render_offline.py` — the visual dry-run, no admin needed
