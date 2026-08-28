"""Live sensor reader.

Phase 1: psutil only (always works, no admin). Good enough for RAM/ROM/Net.
Phase 2: LibreHardwareMonitor via pythonnet (.NET Core runtime + netstandard2.0
          DLL). Works for GPU without admin; CPU temp/power/clock need admin
          (or PawnIO driver) for AMD CPUs.

If a source isn't available, the relevant field stays at its dataclass default
(usually 0) and the renderer shows '-' or a sensible placeholder.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import threading
import time
from typing import Optional

# IMPORTANT: PYTHONNET_RUNTIME must be set before `import clr`.
os.environ.setdefault("PYTHONNET_RUNTIME", "coreclr")

import psutil

from .sensors_types import (
    CpuStats,
    GpuStats,
    MemDiskStats,
    NetStats,
    SensorSnapshot,
    WeatherDay,
    WeatherStats,
)


log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Hardware name detection
#
# Used for the section headers ("CPU STATUS MONITORING" → actual model name).
# LHM is the preferred source (single source of truth, consistent format).
# Fallbacks run when LHM isn't loaded (--no-lhm) or hasn't seen the device yet.
# ─────────────────────────────────────────────────────────────────────────────
# Generic trailing suffix that Windows + LHM tack onto many CPU names, e.g.
# "AMD Ryzen 5 5600X 6-Core Processor" → strip " 6-Core Processor".
# Matches "<N>-Core Processor" or "Processor" on Intel chips.
_GENERIC_CPU_SUFFIX_RE = re.compile(r"\s+\d+-Core Processor$| Processor$")


def _normalize_hw_name(raw: str) -> str:
    """Trim generic marketing suffixes from a raw hardware name string."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    return _GENERIC_CPU_SUFFIX_RE.sub("", raw)


def _detect_cpu_name() -> str:
    """Best-effort CPU name without LHM. Windows-only; returns "" elsewhere."""
    if sys.platform != "win32":
        return ""
    # Registry is readable without elevation and always present on Windows.
    try:
        import winreg  # type: ignore  # Windows-only
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
        ) as key:
            name, _ = winreg.QueryValueEx(key, "ProcessorNameString")
        return _normalize_hw_name(str(name))
    except Exception as e:  # noqa: BLE001
        log.debug("CPU name from registry failed: %s", e)
        return ""


def _detect_gpu_name() -> str:
    """Best-effort GPU name without LHM via WMI (PowerShell CIM). Returns "" on failure.

    Avoids the deprecated ``wmic.exe`` shell — ``Get-CimInstance Win32_VideoController``
    via PowerShell is the modern equivalent and works without admin.
    """
    if sys.platform != "win32":
        return ""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_VideoController -Filter \"AdapterCompatibility<>'Parsec' AND AdapterCompatibility<>'SudoMaker'\" | Select-Object -First 1 -ExpandProperty Name)"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode != 0:
            return ""
        name = (out.stdout or "").strip()
        # CIM returns one name per instance; we already filtered to physical
        # adapters (skipping Parsec/SudoMaker virtual display drivers).
        return _normalize_hw_name(name)
    except Exception as e:  # noqa: BLE001
        log.debug("GPU name from WMI failed: %s", e)
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# LibreHardwareMonitor via pythonnet — best-effort, no-op if not importable.
# Stage the netstandard2.0 LHM DLL plus System.Memory / System.Management next
# to this package file. We load them by ABSOLUTE PATH (AddReferenceToFile)
# because name-based AddReference only searches .NET probing paths (GAC, app
# base), NOT cwd or sys.path — which is why the chdir trick failed in some
# invocation paths (e.g. `python -m trofeo_vision.main`).
# ─────────────────────────────────────────────────────────────────────────────
_LHM_AVAILABLE = False
_LHM_IMPORT_ERROR: Optional[str] = None
try:
    import clr  # type: ignore  # from pythonnet
    # IMPORTANT: clr (pythonnet 3.x with coreclr runtime) only exposes
    # `AddReference(name)` — NOT `AddReferenceToFile(path)`. .NET Core resolves
    # names against (a) the AppDomain base directory (= python.exe's folder)
    # and (b) the PATH; it does NOT search our package directory or sys.path.
    # We bridge this by:
    #   1. `os.add_dll_directory(pkg_dir)` so Windows can find native deps if
    #      the runtime probes there, and
    #   2. hooking `AppDomain.AssemblyResolve` to load any assembly name we
    #      have a staged DLL for, by absolute path. After that, name-based
    #      `clr.AddReference(name)` resolves to the staged copy.
    _pkg_dir = os.path.dirname(os.path.abspath(__file__))
    # `os.add_dll_directory` exists on Windows 3.8+ for native (P/Invoke) DLLs.
    # It's a no-op on platforms without support, so guard it.
    try:
        os.add_dll_directory(_pkg_dir)
    except (AttributeError, OSError):
        pass
    # The LHM DLL we stage (LibreHardwareMonitorLib.dll from FanControl's build)
    # bundles PawnIO scripts as embedded resources, but it P/Invokes
    # `PawnIOLib.dll` from `C:\Program Files\PawnIO\`. Without that dir in
    # the DLL search path the LPC/EC IO port reads fail silently. Add the
    # install dir if PawnIO is present — `LHM PawnIO.IsInstalled` returns
    # True at runtime if it's reachable.
    _pawnio_install = os.environ.get("PAWNIO_INSTALL") or r"C:\Program Files\PawnIO"
    if os.path.isdir(_pawnio_install):
        try:
            os.add_dll_directory(_pawnio_install)
        except (AttributeError, OSError):
            pass

    from System import AppDomain, ResolveEventHandler  # type: ignore
    from System.Reflection import Assembly  # type: ignore

    def _resolve_assembly(_sender, args):  # noqa: ANN001
        # `args.Name` looks like "System.Memory, Version=4.0.5.0, ..." —
        # only the leading assembly name matters for our DLL lookup.
        try:
            full_name = args.Name or ""
            asm_name = full_name.split(",", 1)[0].strip()
        except Exception:
            return None
        if not asm_name:
            return None
        dll_path = os.path.join(_pkg_dir, asm_name + ".dll")
        if os.path.isfile(dll_path):
            try:
                return Assembly.LoadFile(dll_path)
            except Exception:  # noqa: BLE001
                return None
        return None

    # `add_AssemblyResolve` requires an explicit ResolveEventHandler delegate.
    AppDomain.CurrentDomain.add_AssemblyResolve(
        ResolveEventHandler(_resolve_assembly)
    )

    # Load transitive deps first (System.Memory is a hard requirement of LHM).
    clr.AddReference("System.Memory")
    clr.AddReference("System.Management")
    clr.AddReference("LibreHardwareMonitorLib")
    from LibreHardwareMonitor import Hardware  # type: ignore
    _LHM_AVAILABLE = True
except Exception as e:  # noqa: BLE001
    _LHM_IMPORT_ERROR = repr(e)


class SensorReader:
    """Periodically samples all sensors and exposes the latest snapshot.

    Usage:
        reader = SensorReader()
        reader.start()
        ...
        snap = reader.snapshot  # current values
        reader.stop()
    """

    def __init__(self, interval: float = 1.0, enable_lhm: bool = True):
        self.interval = interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._snap = SensorSnapshot()
        self._last_net = psutil.net_io_counters()
        self._last_net_time = time.time()

        # Hardware names — resolved once at init (cheap; both fallbacks read
        # from Windows registry / WMI which we don't want to hit every tick).
        # Empty string means "unknown" and the renderer falls back to "CPU"/"GPU".
        self._cpu_name = ""
        self._gpu_name = ""

        # LHM state
        self._lhm_computer = None
        self._lhm_cpu_hw = None
        self._lhm_gpu_hw = None
        if enable_lhm and _LHM_AVAILABLE:
            try:
                self._init_lhm()
            except Exception as e:  # noqa: BLE001
                log.warning("LHM init failed, falling back to psutil only: %s", e)
                self._lhm_computer = None

        # Resolve names. Prefer LHM (already populated if init succeeded);
        # otherwise fall back to OS-level queries that work without admin.
        if self._lhm_cpu_hw is not None:
            self._cpu_name = _normalize_hw_name(self._lhm_cpu_hw.Name or "")
        if self._lhm_gpu_hw is not None:
            self._gpu_name = _normalize_hw_name(self._lhm_gpu_hw.Name or "")
        if not self._cpu_name:
            self._cpu_name = _detect_cpu_name()
        if not self._gpu_name:
            self._gpu_name = _detect_gpu_name()
        log.info("Detected hardware: CPU=%r, GPU=%r",
                 self._cpu_name or "(unknown)",
                 self._gpu_name or "(unknown)")

    @property
    def lhm_available(self) -> bool:
        return self._lhm_computer is not None

    @property
    def lhm_import_error(self) -> Optional[str]:
        return _LHM_IMPORT_ERROR

    # ── Lifecycle ────────────────────────────────────────────────────
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="sensor-reader",
                                        daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        if self._lhm_computer is not None:
            try:
                self._lhm_computer.Close()
            except Exception:  # noqa: BLE001
                pass

    @property
    def snapshot(self) -> SensorSnapshot:
        return self._snap

    # ── Internals ───────────────────────────────────────────────────
    def _init_lhm(self):
        from LibreHardwareMonitor import Hardware  # type: ignore
        computer = Hardware.Computer()
        computer.IsCpuEnabled = True
        computer.IsGpuEnabled = True
        computer.IsMemoryEnabled = False  # avoid RAMSPDToolkit-NDD dep
        computer.IsMotherboardEnabled = True
        computer.IsControllerEnabled = False
        computer.IsNetworkEnabled = False
        computer.IsStorageEnabled = False
        computer.Open()
        for hw in computer.Hardware:
            t = type(hw).__name__.lower()
            n = (hw.Name or "").lower()
            if ("cpu" in t or "ryzen" in n or "core" in n) and self._lhm_cpu_hw is None:
                self._lhm_cpu_hw = hw
            elif ("gpu" in t or "radeon" in n or "geforce" in n) and self._lhm_gpu_hw is None:
                self._lhm_gpu_hw = hw
        self._lhm_computer = computer
        log.info("LHM hardware: %s / %s",
                 self._lhm_cpu_hw.Name if self._lhm_cpu_hw else "(no CPU)",
                 self._lhm_gpu_hw.Name if self._lhm_gpu_hw else "(no GPU)")

    def _run(self):
        # First Update() — populates the sensor tree.
        if self._lhm_computer is not None:
            for hw in self._lhm_computer.Hardware:
                self._update_tree(hw)
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:  # noqa: BLE001
                log.exception("Sensor tick failed: %s", e)
            self._stop.wait(self.interval)

    def _update_tree(self, hw):
        try:
            hw.Update()
        except Exception:  # noqa: BLE001
            pass
        for sub in getattr(hw, "SubHardware", []):
            self._update_tree(sub)

    def _tick(self):
        # Update LHM hardware tree.
        if self._lhm_computer is not None:
            for hw in self._lhm_computer.Hardware:
                self._update_tree(hw)

        mem = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        snap = SensorSnapshot(
            cpu=self._read_cpu(),
            gpu=self._read_gpu(),
            mem=MemDiskStats(
                ram_percent=mem.percent,
                ram_used_gb=mem.used / (1024 ** 3),
                ram_total_gb=mem.total / (1024 ** 3),
                disk_percent=disk.percent,
                disk_used_gb=disk.used / (1024 ** 3),
                disk_total_gb=disk.total / (1024 ** 3),
            ),
            net=self._read_net(),
            timestamp=time.time(),
        )
        self._snap = snap

    def _read_cpu(self) -> CpuStats:
        cpu = CpuStats(usage_percent=psutil.cpu_percent(interval=None),
                       name=self._cpu_name)
        if self._lhm_cpu_hw is None:
            return cpu

        # Pull CPU temp, power, clock, voltage from LHM sensors.
        # AMD Ryzen: "Core (Tctl/Tdie)" Temperature, "Package" Power,
        #            "Cores (Average)" Clock, "Core (SVI2 TFN)" Voltage
        for s in self._lhm_cpu_hw.Sensors:
            v = s.Value
            if v is None:
                continue
            name = (s.Name or "")
            stype = str(s.SensorType).lower()
            val = float(v)
            # NB: stype is lowercased — match against lowercase keywords.
            if "temperature" in stype and ("Tctl" in name or "Tdie" in name
                                           or "Package" in name or "CPU" in name):
                if val > 0:
                    cpu = CpuStats(**{**cpu.__dict__, "temp_c": val})
            elif "power" in stype and ("Package" in name or "CPU" in name):
                if val > 0:
                    cpu = CpuStats(**{**cpu.__dict__, "power_w": val})
            elif "clock" in stype and "Average" in name:
                if val > 0:
                    cpu = CpuStats(**{**cpu.__dict__, "clock_mhz": val})
            elif "voltage" in stype and "SVI2 TFN" in name:
                if val > 0:
                    cpu = CpuStats(**{**cpu.__dict__, "voltage_v": val})

        # Fan + pump from motherboard sub-hardware.
        # LHM's NCT drivers (via PawnIO LPC/EC) expose fans as generic
        # "Fan #1"…"Fan #6" with no semantic labels; ASRock boards put the
        # pump on a header labelled SYS_FAN2 or similar. Heuristics:
        #   - First fan with "pump"/"water"/"aio" in name  → AIO pump
        #   - Otherwise the first motherboard fan → CPU fan
        #   - Second motherboard fan → AIO pump (fallback)
        # Scan ALL LHM hardware so we don't depend on a specific class name
        # ("Motherboard" vs future variants).
        if self._lhm_computer is not None:
            mb_fans: list[tuple[str, float]] = []
            pump_value: Optional[float] = None
            for hw in self._lhm_computer.Hardware:
                for sub in self._lhm_subtree(hw):
                    for s in sub.Sensors:
                        if str(s.SensorType).lower() != "fan":
                            continue
                        v = s.Value
                        if v is None:
                            continue
                        try:
                            fv = float(v)
                        except (TypeError, ValueError):
                            continue
                        if fv <= 0:
                            continue
                        name = (s.Name or "")
                        lname = name.lower()
                        # Prefer name-based pump detection first.
                        if pump_value is None and any(
                            k in lname for k in ("pump", "water", "aio")
                        ):
                            pump_value = fv
                            continue
                        # Otherwise collect motherboard-class fans.
                        tname = type(sub).__name__.lower()
                        hname = type(hw).__name__.lower()
                        if "motherboard" in tname or "motherboard" in hname \
                                or lname.startswith("fan #") or lname.startswith("fan"):
                            mb_fans.append((name, fv))
            if cpu.fan_rpm == 0 and mb_fans:
                cpu = CpuStats(**{**cpu.__dict__, "fan_rpm": mb_fans[0][1]})
            if cpu.pump_rpm == 0:
                if pump_value is not None:
                    cpu = CpuStats(**{**cpu.__dict__, "pump_rpm": pump_value})
                elif len(mb_fans) >= 2:
                    cpu = CpuStats(**{**cpu.__dict__, "pump_rpm": mb_fans[1][1]})
        return cpu

    def _read_gpu(self) -> GpuStats:
        gpu = GpuStats(name=self._gpu_name)
        if self._lhm_gpu_hw is None:
            return gpu
        # AMD RX 7800 XT sensor map (LHM):
        #   "GPU Core" + Temperature -> temp_c
        #   "GPU Package" + Power    -> power_w
        #   "GPU Core" + Clock       -> clock_mhz
        #   "GPU Memory" + Clock     -> vram_clock_mhz
        #   "GPU Core" + Voltage     -> voltage_v
        #   "GPU Core" + Load        -> usage_percent
        #   "GPU Fan"  + Fan         -> fan_rpm (NOT Control)
        #   "GPU Memory Used"   (SmallData) -> vram_used_mb
        #   "GPU Memory Total"  (SmallData) -> vram_total_mb
        for s in self._lhm_gpu_hw.Sensors:
            n = s.Name or ""
            v = s.Value
            if v is None:
                continue
            val = float(v)
            stype = str(s.SensorType)
            if stype == "Temperature" and n == "GPU Core":
                gpu = GpuStats(**{**gpu.__dict__, "temp_c": val})
            elif stype == "Power" and n == "GPU Package":
                gpu = GpuStats(**{**gpu.__dict__, "power_w": val})
            elif stype == "Clock" and n == "GPU Core":
                gpu = GpuStats(**{**gpu.__dict__, "clock_mhz": val})
            elif stype == "Clock" and n == "GPU Memory":
                gpu = GpuStats(**{**gpu.__dict__, "vram_clock_mhz": val})
            elif stype == "Voltage" and n == "GPU Core":
                gpu = GpuStats(**{**gpu.__dict__, "voltage_v": val})
            elif stype == "Load" and n == "GPU Core":
                gpu = GpuStats(**{**gpu.__dict__, "usage_percent": val})
            elif stype == "Fan" and n == "GPU Fan":
                gpu = GpuStats(**{**gpu.__dict__, "fan_rpm": val})
            elif stype == "SmallData" and n == "GPU Memory Used":
                gpu = GpuStats(**{**gpu.__dict__, "vram_used_mb": val})
            elif stype == "SmallData" and n == "GPU Memory Total":
                gpu = GpuStats(**{**gpu.__dict__, "vram_total_mb": val})
        return gpu

    def _lhm_subtree(self, hw):
        yield hw
        for child in getattr(hw, "SubHardware", []):
            yield from self._lhm_subtree(child)

    def _read_net(self) -> NetStats:
        now = psutil.net_io_counters()
        t = time.time()
        dt = max(0.001, t - self._last_net_time)
        up = (now.bytes_sent - self._last_net.bytes_sent) / dt
        down = (now.bytes_recv - self._last_net.bytes_recv) / dt
        self._last_net = now
        self._last_net_time = t
        return NetStats(up_bps=max(0.0, up), down_bps=max(0.0, down))
