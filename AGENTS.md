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
| Frame rate | ~6.3 FPS (USB 2.0 limited) |

**Driver binding:** If pyusb can't see the device after TRCC install, use Zadig
to replace the driver with WinUSB for VID `0416` PID `5408`.

---

## LAYOUT (offline renderer — current state)

```
y=  0
y= 24 [CPU STATUS MONITORING]                              ← header
y= 52  [Hero card: ON DEMAND | 0% CPU USAGE     | [2x2 grid:
y=      | TEMP 0°  POWER 0W      |  CPU FAN | CPU CLOCK
y=      |                       |  WTR PUMP| VOLTAGE  ]
y=520 [GPU STATUS MONITORING]
y=548  [Hero card: ON DEMAND | 0% GPU USAGE     | [2x2 grid:
y=      | TEMP 0°  POWER 0W      |  VRAM | GPU CLOCK
y=      |                       |  GPU FAN | VOLTAGE  ]
y=1020 [RAM 34%] [ROM 56%] [FPS 0]                          ← 3 mini cards
y=1180 [NETWORK SPEED          ] [TIME              ]
y=1340 [WeatherFlow (disabled)                     ]
y=1860 [TROFEO VISION 9.16"  ·  CUSTOM DASHBOARD  PYTHON  ·  PIL  ·  WINUSB]
```

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
| Weather | OpenWeatherMap | ✅ stub, needs API key |

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

# Continuous dry-run (loops at 1fps, saves frames)
python scripts/render_offline.py --loop --rate 1 --no-lhm
```

## KNOWN GOTCHAS

1. **TRCC holds USB** — must be fully closed before our driver can claim the interface
2. **CPU usage first sample is 0** — `psutil.cpu_percent(interval=None)` needs ~1s of warm-up
3. **LHM needs admin + PawnIO driver** for motherboard sensors (CPU temp on AMD boards)
4. **JPEG decoder is baseline only** — no progressive, must use `quality=85, subsampling=0`
5. **Display blanks when frames stop** — must push continuously, not just one frame
6. **psutil.cpu_percent per-CPU values** — we report overall %, not per-core
7. **No FPS source** — currently hardcoded to 0; wire up PresentMon or RTSS later
