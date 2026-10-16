# TrofeoVision — Knowledge Base

**Path:** `D:\Documents\PersonalProjects\Sites\TrofeoVision\`
**Stack:** Python 3.14 · Pillow · pyusb · psutil · LibreHardwareMonitor (optional)
**Device:** Thermalright Trofeo Vision 9.16" (462×1920 portrait LCD, VID 0x0416 / PID 0x5408)
**Status:** Active — offline renderer + LHM sensors working, USB push not yet tested on hardware
**Repo:** https://github.com/JuliusNarvasa/TrofeoVision (public)

---

## OVERVIEW

Replaces the official Thermalright TRCC software for the Trofeo Vision 9.16".
A Python renderer pulls live sensor data, draws a dashboard at native
resolution (462×1920), and pushes JPEG frames to the device over USB using
the reverse-engineered LY Bulk protocol.

The motivation: TRCC's theme system (`C:\TRCCCAP\Data\USBLCD\Theme4621920\`)
is image-only and only supports limited text-overlay live data. We wanted
pixel-perfect, fully-live dashboard widgets (CPU/GPU/RAM/ROM/Net/Time/Weather).

---

## DEVICE — Thermalright Trofeo Vision 9.16"

| Spec | Value |
|---|---|
| USB VID:PID | `0x0416:0x5408` (Winbond Electronics) |
| Resolution | 462 × 1920 (portrait) |
| USB class | Vendor Specific (`0xFF`) |
| EP OUT | `0x09` (bulk) |
| EP IN | `0x81` (bulk) |
| Protocol | LY Bulk (chunked JPEG) |
| JPEG quality | 85, baseline, 4:4:4 subsampling |
| Frame rate | ~6.3 FPS (USB 2.0 hardware cap), **default target 2 FPS** |

**Driver binding:** If pyusb can't see the device after TRCC install, use Zadig
to replace the driver with WinUSB for VID `0416` PID `5408`.

---

## LAYOUT (offline renderer — current state)

Top of the dashboard is a **greeting tile** (full-width 414×280) that
combines a personalized greeting with the live weather. Every other
section shifts down accordingly.

```
y=  0
y= 30 [Greeting tile (414×280):
y=    |   Good <time>, <name>             ← greeting, time-of-day based
y=    |   • <location>                    ← OWM city
y=    |   26°           Mostly Cloudy     ← big temp + condition
y=    |   ↑30° ↓24° • Feels like —       ← today's hi/lo (em-dash = no data)
y=    |   (background = <state>_<bucket>.png from assets/weather/) ]
y=310
y=326 [CPU <model name>]                            ← e.g. "AMD RYZEN 5 5600X"
y=364 [Hero card (full width, 414×340):
y=    |  [TAG]                  TEMP 62°
y=    |   47% CPU USAGE         POWER 78W
y=    |  ┌── history chart strip (USE/TEMP/PWR, no header) ──┐
y=    |  │                                                  │
y=    |  └──────────────────────────────────────────────────┘ ]
y=720 [Misc card (ONE big card, 4 cells with vertical dividers):
y=    |  CPU FAN  │ CPU CLOCK │ AIO PUMP  │ VOLTAGE         ]
y=920 [GPU <model name>]                            ← e.g. "AMD RADEON RX 7800 XT"
y=958 [Hero card (full width) — same structure as CPU  ]
y=1314 [Misc card: VRAM │ GPU CLOCK │ GPU FAN │ VRAM CLOCK]
y=1514 [RAM 34%      ] [ROM 56%      ]                  ← 2 wider mini cards (FPS tile removed)
y=1690 [NETWORK SPEED          ] [TIME              ]
y=1850 [TROFEO VISION 9.16"  ·  CUSTOM DASHBOARD  PYTHON  ·  PIL  ·  WINUSB]
```

**Section split:** `section_h=540`, `hero_h=340` (favored ~63%),
`gap=16`, `misc_h=184`. Chart strip sits in the bottom of the hero card
at `y+180`, height = `hero_h - 180 - 20` = `140 px`, width `374 px`.
`bottom_h` (RAM/ROM + Net/Time rows) trimmed from 180 → **160** to
make room for the greeting tile at the top; everything still fits inside
the 70-px footer band ending at `y=1920`.

**History:** ring buffer holds 120 samples at 1 Hz (2 minutes). The
chart legend (USE/TEMP/PWR dots) is the only chart header — no
seconds-counter title.

**Sensor cadences:** the LHM hardware tree is split into two refresh
rates so we don't pay the LPC/EC tree-walk cost at every tick:
- **Fast (1 Hz):** CPU + GPU only — temp/power/clock/VRAM need fresh
  data for the dashboard.
- **Slow (every 3 s):** motherboard + anything else — CPU fan + AIO
  pump RPMs are the only sensors here, and they barely change
  second-to-second. The split is in `SensorReader._tick` with the
  interval hard-coded as `_LHM_SLOW_INTERVAL_S` near the top of
  `sensors.py`.

---

## THEMING (light + dark)

Two palettes ship out of the box: a light monochrome (the original
reference look) and a dark low-light palette. The `C` color constants in
`renderer.py` are now swappable — `set_theme(theme)` overwrites them at
the start of every frame, so every drawing helper continues to read
from `C.XXX` without threading a theme parameter through.

**Palettes (`Theme.light()` / `Theme.dark()` in `renderer.py`):**

| Slot | Light | Dark |
|---|---|---|
| `BG` / page | `(240,240,244)` | `(18,18,22)` |
| `CARD` | `(255,255,255)` | `(32,32,38)` |
| `CARD_EDGE` | `(228,228,232)` | `(60,60,70)` |
| `TITLE` / `VALUE` | near-black | near-white |
| `LABEL` / `VALUE_DIM` | medium gray | lighter gray |
| `BAR_BG` / `BAR_FILL` | light / dark | dark / light (inverted) |
| `TAG_BG` / `TAG_FG` (ON DEMAND pill) | dark / white | bright / dark (inverted) |
| `USAGE_LINE` / `TEMP_LINE` / `POWER_LINE` | dark gray / orange / blue | light gray / brighter orange / brighter blue |
| `GREET_FG` / `GREET_DIM` | white / dim white | unchanged — always on photo background |

**Time-based auto switching** ([theme] in `config.toml`):

```toml
[theme]
mode = "auto"           # "auto" | "light" | "dark"
dark_start_hour = 18    # inclusive
dark_end_hour = 6       # exclusive (wraps midnight: dark 18-6 = 18:00–05:59)
```

`mode = "auto"` picks dark when the current local hour falls inside the
`dark_start_hour` .. `dark_end_hour` window, light otherwise. The window
may wrap midnight (defaults do — dark 18:00–06:00). A non-wrapping window
(e.g. `dark_start_hour=20, dark_end_hour=23`) is also supported. Setting
`dark_start_hour == dark_end_hour` disables the window (always light).

`Renderer.render()` resolves the active theme via `resolve_theme(cfg, dt)`
before drawing, so every frame is consistently themed. CLI override for
visual testing: `python scripts/render_offline.py --theme {auto,light,dark}`.

The greeting-tile photo background (`assets/weather/<state>_<bucket>.png`)
is theme-agnostic — the dark gradient overlay that ensures text
readability stays the same in both themes.

---

## GREETING TILE

Top-of-dashboard banner that **replaces** the old bottom weather card.
Combines the time-of-day greeting with the live weather, drawn over a
weather-state background image.

**Greeting mapping** (`time_bucket` in `weather.py`, all local time):
| Hour | Bucket | Greeting |
|---|---|---|
| 05–11 | `morning` | "Good morning, <name>" |
| 12 | `noon` | "Good noon, <name>" |
| 13–17 | `afternoon` | "Good afternoon, <name>" |
| 18–21 | `evening` | "Good evening, <name>" |
| 22–04 | `night` | "Good evening, <name>" (intentional fallback — apps rarely say "Good night") |

**Background images:** `<repo>/assets/weather/<state>_<bucket>.png`,
40 files = 8 weather states × 5 time buckets. All assets are **462×280 px**
PNG with the weather motif full-bleed; the renderer pastes them at native
size onto the 414×280 tile (3-px crop on each side) and overlays a
quadratic top→bottom dark gradient (`max_alpha=110/255`) for text
readability. State routing lives in `icon_to_state()` in `weather.py`.

| OWM prefix | State | OWM prefix | State |
|---|---|---|---|
| `01` | clear | `09` | drizzle |
| `02` | partly_cloudy | `10` | rain |
| `03`, `04` | cloudy | `11` | thunder |
| `13` | snow | `50` | fog |

**Configurable fields** (in `trofeo_vision/config.toml`):
```toml
[greeting]
name = "Bambaw"           # shown after "Good <time of day>, "

[weather]
enabled = true
api_key = "..."           # https://home.openweathermap.org/api_keys
# assets_dir = ""         # blank → <repo>/assets/weather/ (default)
```

**Disabled-weather fallback:** if `weather.enabled=false` or the fetch
fails, the tile still renders — greeting is real, location shows
"weather disabled", temp shows "?°", condition + hi/lo show "—". The
clear_morning background is used so the disabled state looks
intentional, not broken.

---

## TOP TILE — weather OR air quality

The greeting tile at the top of the dashboard can show one of two
payloads, switched live from the tray menu (radio items under
"Force Refresh" in the right-click menu). Persisted to `[top_tile].mode`
in `config.toml` so the choice survives restart.

| `mode` value | Tile shows |
|---|---|
| `weather` | Original layout — greeting + temp + condition + today's hi/lo + feels-like |
| `air_quality` | Greeting + raw US AQI number (0–500) + category + PM2.5 μg/m³ + PM10 μg/m³ |

Layout is identical across modes (same x/y/font/overlay) so flipping is
a payload swap, not a layout rebuild. Background comes from
`assets/air_quality/<aqi_state>_<bucket>.png` with category-color
gradient fallback.

**Air quality data source:** [Open-Meteo Air Quality API](https://open-meteo.com/en/docs/air-quality-api)
— backed by the Copernicus Atmosphere Monitoring Service (CAMS) model,
global coverage (every coordinate on Earth), **no API key required**,
free for non-commercial use. Picked after testing both OWM Air Pollution
(sparse Mindanao coverage → reported AQI 1 on hazy days) and WAQI
(good coverage but no Davao station registered).

API call is two steps (geocode + air quality):
```
1. https://geocoding-api.open-meteo.com/v1/search
   ?name=Davao&countryCode=PH&count=1&language=en&format=json
2. https://air-quality-api.open-meteo.com/v1/air-quality
   ?latitude=7.0731&longitude=125.6128
   &current=us_aqi,pm10,pm2_5,ozone,nitrogen_dioxide
   &timezone=auto
```

The geocode result is cached in memory (city + country rarely change),
so subsequent air-quality calls skip step 1. Result cache default = 30
min (pollutants move slower than temperature). `countryCode=PH` is what
keeps "Davao" resolving to Davao City Philippines instead of Davao
Costa Rica — the ISO code from `[weather].country` is reused.

**Configurable fields** (`config.toml`):
```toml
[air_quality]
enabled = true
city = "Davao"   # blank → inherits from [weather].city
country = "PH"   # blank → inherits from [weather].country (ISO-3166-1 alpha2)
refresh_seconds = 1800
# assets_dir = ""   # blank → <repo>/assets/air_quality/

[top_tile]
# "weather" or "air_quality" — flipped from the tray menu
mode = "weather"
```

**Tray menu** (`right-click → after Force Refresh`):
```
─────────
● Weather
○ Air Quality
─────────
```
The filled radio bullet shows the active payload. Picking either
item flips the mode, persists to `config.toml`, and the next frame
picks up the new payload — no restart needed.

**US AQI breakpoints** (used for the bucket label + gradient):

| AQI range | Label | State | Gradient color |
|---|---|---|---|
| 0–50 | Good | `good` | teal-green |
| 51–100 | Moderate | `fair` | yellow-green |
| 101–150 | Unhealthy (Sensitive) | `moderate` | warm yellow |
| 151–200 | Unhealthy | `poor` | orange |
| 201–300 | Very Unhealthy | `very_poor` | deep red |
| 301+ | Hazardous | `very_poor` | deep red |

**Disabled-air-quality fallback:** if `air_quality.enabled=false` or the
fetch fails, the tile still renders — greeting is real, location shows
"set [air_quality] enabled = true" (or the fetch error), big AQI shows "—",
pollutants show "—". The `good_morning` background (or generated
gradient) is used so the disabled state looks intentional.

**Custom backgrounds:** drop 462×280 PNGs into
`assets/air_quality/<aqi_state>_<time_bucket>.png` — 5 states × 5
buckets = 25 files max. Missing files fall back to `good_morning.png`,
then to a generated category-color gradient. See
`assets/air_quality/README.md`.

---

## PROTOCOL — LY Bulk chunked JPEG

Reference docs:
- `Lexonight1/thermalright-trcc-linux/doc/PROTOCOL_USBLCDNEW.md` (canonical)
- `Abigor-DIY/Open-Trofeo-LCD/trofeo_lcd.py` (working Linux driver)

### Handshake
```
Send 2048 bytes to EP 0x09:
  [0]=0x02, [1]=0xFF, [8]=0x01, rest zero

Read 512 bytes from EP 0x81. Validate:
  response[0]==0x03, response[1]==0xFF, response[8]==0x01
```

### Frame transfer
JPEG data → split into 512-byte USB chunks (496-byte payload each).
Each chunk has a 16-byte header (struct packed):

| Offset | Bytes | Value |
|---|---|---|
| 0 | 1 | `0x01` (marker) |
| 1 | 1 | `0xFF` |
| 2-5 | 4 | total JPEG byte count (LE u32) |
| 6-7 | 2 | this payload size (LE u16) |
| 8 | 1 | `0x01` (flag) |
| 9-10 | 2 | total chunks (LE u16, padded to multiple of 4) |
| 11-12 | 2 | chunk index (LE u16) |
| 13-15 | 3 | `0x00` padding |

Bursts: 8 chunks per USB write (4096 bytes per write).

After all chunks, read 512 bytes ACK from EP 0x81.

---

## SENSORS

| Field | Source | Status |
|---|---|---|
| CPU usage | `psutil.cpu_percent` | ✅ live |
| RAM % | `psutil.virtual_memory` | ✅ live |
| ROM % | `psutil.disk_usage('/')` | ✅ live |
| Net up/down | `psutil.net_io_counters()` delta | ✅ live |
| Time/date | `datetime.now()` | ✅ live (12-hour `H:MM AM/PM`) |
| GPU usage / temp / power / clock / fan / VRAM / voltage | LibreHardwareMonitor | ✅ live |
| CPU voltage (SVI2 TFN) | LibreHardwareMonitor | ✅ live (no admin needed) |
| CPU temp / power / clock | LibreHardwareMonitor | ✅ as admin (non-admin stays at 0) |
| CPU fan / AIO pump RPM | LibreHardwareMonitor (PawnIO) | ✅ as admin on B550M Pro4 |
| Weather | OpenWeatherMap | ✅ live — drives the greeting tile (location, temp, condition, today's hi/lo, feels-like). Set `[weather] enabled = true` + `api_key` in config.toml. |

**LHM DLL:** `trofeo_vision/LibreHardwareMonitorLib.dll` is FanControl's
build (`v0.9.6+2647e1bd`), not upstream LHM — the upstream Windows build
doesn't ship a working EC/LPC driver path. FanControl's build embeds
PawnIO scripts (LpcIO.bin, SmbusNCT6793.bin, …) and P/Invokes
`PawnIOLib.dll`. PawnIO must be installed and the kernel driver running
(`sc query PawnIO` → STATE 4 RUNNING; v2.1.0.0 verified on this box).

**LHM loader:** Pythonnet 3.x with `PYTHONNET_RUNTIME=coreclr` only
exposes `clr.AddReference(name)` (resolves against AppDomain base + PATH)
— it does NOT have `AddReferenceToFile(path)`. We hook
`AppDomain.AssemblyResolve` plus `os.add_dll_directory(pkg_dir)` so the
staged DLLs (`LibreHardwareMonitorLib`, `System.Memory`,
`System.Management`, `HidSharp`, `System.IO.Ports`,
`Mono.Posix.NETStandard`, `libMonoPosixHelper`, `MonoPosixHelper`)
resolve by name. We also `os.add_dll_directory("C:\Program Files\PawnIO")`
so LHM's P/Invoke finds `PawnIOLib.dll`. See the loader block at the
top of `trofeo_vision/sensors.py`.

**Admin requirements** (verified on Ryzen 5 5600X + ASRock B550M Pro4 + RX 7800 XT):
- Non-admin: GPU sensors (temp/power/clock/fan/VRAM/voltage), CPU voltage (SVI2 TFN),
  RAM/ROM/Net/Time/Weather — all live.
- Admin (`start.bat` → Yes on UAC): unlocks CPU package temp + power + clock via
  PawnIO AMD SMU scripts, AND unlocks motherboard Super I/O (NCT6796D-R) which
  exposes the CPU fan header RPM and the AIO pump RPM via LPC port reads.

**Hardware names** (displayed in section headers — "AMD RYZEN 5 5600X" instead
of "CPU STATUS MONITORING", "AMD RADEON RX 7800 XT" instead of "GPU STATUS
MONITORING"). Resolved once at `SensorReader.__init__`, cached for the lifetime
of the reader:
- **CPU name source priority:** LHM `hw.Name` → registry
  `HKLM\HARDWARE\DESCRIPTION\System\CentralProcessor\0\ProcessorNameString`
  (no admin required) → `"CPU"`. Trailing generic suffixes like
  `" 6-Core Processor"` are stripped via `_normalize_hw_name()` in
  `sensors.py`.
- **GPU name source priority:** LHM `hw.Name` → PowerShell
  `Get-CimInstance Win32_VideoController` (no admin required; filters out
  Parsec/SudoMaker virtual display adapters) → `"GPU"`.

---

## TROFEO'S OWN THEME FORMAT (for reference)

`C:\TRCCCAP\Data\USBLCD\Theme4621920\` holds the user's existing custom themes:

| File | Purpose |
|---|---|
| `Theme.png` | Cover/preview image (462×1920) |
| `00.png`, `01.png` | Frame sequence for animation |
| `config1.dc` | Binary text-overlay template (615 bytes) |
| `Theme.zt` | Compressed frame bundle (~14 MB) |
| `Theme.dcC7C1653A` | Parent theme config (48 bytes) |

`config1.dc` contains:
- Font references (Microsoft YaHei, CCWildWords)
- Per-field positions (x/y in pixels)
- Color bytes
- Live-data bindings: time, date, day-of-week confirmed; CPU/GPU sensors likely

The custom themes are: Both, Bubu, Megumin, Theme1-5, WithTime.

---

## USB PUSH — STATUS

- ✅ Driver code complete (`trofeo_vision/driver.py`)
- ⏸️ Not tested on hardware — would need to kill TRCC (PID 3840) first
- ⏸️ Recommended test path:
  1. Close TRCC entirely (incl. tray icon)
  2. Render one frame (dry-run)
  3. Push one frame via `python -m trofeo_vision.main --once` (need to add)
  4. If display lights up: start continuous push at `--rate 4`

---

## RUN COMMANDS

```powershell
# Offline render — safe, no USB
python scripts/render_offline.py --no-lhm --save-jpeg --out ./output

# Live push (requires TRCC closed)
python -m trofeo_vision.main --rate 4

# Tray mode (recommended) — kills TRCC, launches in background, no console.
# Right-click the TrofeoVision icon in the system tray for Pause/Resume/Quit.
.\start.bat

# Continuous dry-run (loops at 1fps, saves frames)
python scripts/render_offline.py --loop --rate 1 --no-lhm
```

---

## TRAY MODE (background, no console)

`start.bat` launches the dashboard as a background process with a
system-tray icon — no console window appears.

**Launcher chain:** `start.bat` → `start "" pythonw.exe` →
`scripts/run_tray.py` → `trofeo_vision.tray.TrayApp`.

**`TrayApp` (`trofeo_vision/tray.py`):**
- Main thread: pystray's Windows message loop (blocks in `icon.run()`).
- Render thread (daemon): sensors + renderer + USB push, same code path
  as `run_live()` in `main.py` minus the console signal handlers.
- Pause/Resume uses a `threading.Event` — render thread blocks cheaply
  between frames without busy-looping.
- State machine: `starting → running ⇄ paused → stopped`; transitions
  to `reconnecting` when USB push fails (typical after the PC sleeps
  and the device detaches), then auto-recovers to `running` once the
  device is re-claimed. True unrecoverable errors land in `error`.
- Sleep/wake resilience: USB errors don't kill the render thread.
  `_open_with_reconnect()` retries `Driver.open()` every 2 s until the
  device reappears, and the inner push loop catches `DriverError` to
  close + reopen on the fly. The display blanks during the gap (still
  per the "must push continuously" rule) but recovers automatically —
  no manual relaunch needed after sleep, hibernate, or TRCC racing us
  for the device at boot.

**Tray menu:**
- `Status: <state>` — read-only label, reflects current state.
- `Pause` / `Resume` — toggleable, only one is visible at a time.
- `Reconnecting — waiting for device…` — visible only while in
  `reconnecting`, so the user sees why frames have stopped.
- `Open log folder` — opens `<repo>/logs/` in Explorer.
- `Quit` — stops the render thread, closes USB, exits.

**Logging:** `<repo>/logs/trofeo_vision.log` (rotating, 1 MB × 3
backups). No console output because `pythonw.exe` has no stdout.

**Icon:** Generated at startup with PIL — a 64×64 dark rounded square
with the USE/TEMP/PWR chart-series dots + a small activity wave.
Colors match `Theme.dark()` from the renderer so the icon visually
links to the dashboard it drives.

**Headless / debug mode:** `python scripts/run_tray.py --verbose` runs
the same code path under `python.exe` (with a console) for debugging.

## AUTOSTART (scheduled task, admin)

`start.bat` is registered as a scheduled task that runs elevated at
logon — replaces the TRCC autostart entries that were removed
(`HKCU\...\Run\TRCCNext` registry value + `\TRCCAppStartup` scheduled
task).

| Field | Value |
|---|---|
| Task | `\TrofeoVision` (Task Scheduler, root folder) |
| Trigger | At logon, 30 s delay (lets desktop + USB stack settle) |
| Action | `D:\Documents\PersonalProjects\Sites\TrofeoVision\start.bat` |
| Principal | `Bambaw`, `LogonType=InteractiveToken` (sees the USB device and tray) |
| Run level | `HighestAvailable` (admin) — required so LHM's PawnIO LPC reads + AMD SMU scripts can pull CPU package temp/power/clock + CPU fan + AIO pump RPM |
| Power | "Stop on battery, No start on batteries" (default) — desktop only |

**Why admin:** `start.bat` itself doesn't need admin, but the LHM
sensors behind it do — without elevation, CPU temp/power/clock and the
fan/pump RPMs all stay at 0. The bat's `taskkill /F /IM TRCC.exe /T`
also benefits from admin so it can kill TRCC's services/children if
any are lingering.

**Manage:**
```powershell
# Status / config
schtasks /Query /TN '\TrofeoVision' /FO LIST /V
schtasks /Query /TN '\TrofeoVision' /XML ONE   # shows RunLevel, LogonType, Delay

# Run now (without rebooting)
schtasks /Run /TN '\TrofeoVision'

# Disable / re-enable (no admin needed — task registration is owned by SYSTEM-owned scheduler service)
Disable-ScheduledTask -TaskName 'TrofeoVision'
Enable-ScheduledTask  -TaskName 'TrofeoVision'

# Remove entirely (requires elevation — schtasks.exe requires admin to delete)
schtasks /Delete /TN '\TrofeoVision' /F

# Re-register (run elevated)
schtasks /Create /TN 'TrofeoVision' `
  /TR '"D:\Documents\PersonalProjects\Sites\TrofeoVision\start.bat"' `
  /SC ONLOGON /RL HIGHEST /DELAY 0000:30 /F
```

**Why not startup folder or HKCU Run:** both run non-elevated, so LHM
sensors stay at 0. Startup-folder `.lnk` can't request elevation
either — only a scheduled task with `RunLevel=HighestAvailable` (or a
UAC-prompting shortcut marked "run as admin") gets a clean elevated
start. The 30 s logon delay dodges the UAC prompt race at first login.

## KNOWN GOTCHAS

1. **TRCC holds USB** — must be fully closed before our driver can claim the interface
2. **CPU usage first sample is 0** — `psutil.cpu_percent(interval=None)` needs ~1s of warm-up
3. **LHM needs admin + PawnIO driver** for motherboard sensors (CPU temp on AMD boards)
4. **JPEG decoder is baseline only** — no progressive, must use `quality=85, subsampling=0`
5. **Display blanks when frames stop** — must push continuously, not just one frame
6. **psutil.cpu_percent per-CPU values** — we report overall %, not per-core
7. **Weather/AQ outage resilience (Oct 2026)** — `WeatherClient`/`AirQualityClient` use 5 s timeout + 60 s failure backoff (negative cache) + never-raise `get()` (placeholder tile on failure); `main.py`/`tray.py`/`render_offline.py` wrap refetch so API bugs can't stall/kill the frame loop — only `DriverError` triggers USB reconnect
