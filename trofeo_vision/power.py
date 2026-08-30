"""Windows suspend/resume detection via ``WM_POWERBROADCAST``.

Uses raw ctypes + ``RegisterPowerSettingNotification`` so we don't pull in
pywin32 just for this. A daemon thread owns an invisible top-level window
and pumps Win32 messages; ``PBT_APMSUSPEND`` / ``PBT_APMRESUMEAUTOMATIC``
flip ``sleep_evt`` / ``resume_evt``.

The render loop checks those events between frames so it can close the
USB device cleanly *before* the USB stack dies, instead of waiting for
the next push to fail with a ``USBTimeoutError`` (which used to cost a
couple of broken frames + an exception per sleep/wake cycle).

NOTE on the window style: ``PBT_APMSUSPEND`` and friends are *broadcast*
messages, which per MSDN are ONLY delivered to top-level windows.
``HWND_MESSAGE`` (message-only) windows explicitly do NOT receive
broadcasts — first attempt at this code used ``HWND_MESSAGE`` and never
saw any power events. We use a real top-level window with
``WS_EX_TOOLWINDOW`` (off the taskbar + alt-tab) and a 0×0 size so it
stays invisible but still receives broadcasts.

Tested on Windows 10/11 — fires reliably on real sleep and on the
lid-close equivalent. See the SO thread "Python detect sleep mode in
Windows10" for the same idea in pywin32 form.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import threading
from typing import Optional


log = logging.getLogger("trofeo_vision.power")


# ── Win32 constants ────────────────────────────────────────────────────────
WM_POWERBROADCAST = 0x0218
WM_QUIT = 0x0012
PBT_APMSUSPEND = 0x0004           # system is about to suspend
PBT_APMRESUMESUSPEND = 0x0007     # resumed after user-initiated wake
PBT_APMRESUMEAUTOMATIC = 0x0012   # resumed automatically (e.g. wake timer)
PBT_POWERSETTINGCHANGE = 0x8013   # GUID-registered power event
# Power broadcast lParam is a pointer to POWERBROADCAST_SETTING with this
# layout when the GUID is GUID_CONSOLE_DISPLAY_STATE (0 = off, 1 = on, 2 = dimmed).
DEVICE_NOTIFY_WINDOW_HANDLE = 0   # MSDN: 0 = window handle, 1 = service handle

# Window styles
WS_OVERLAPPED = 0x00000000
WS_POPUP = 0x80000000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TRANSPARENT = 0x00000020
WS_EX_LAYERED = 0x00080000

HWND_TOP = wt.HWND(0)

# GUID_CONSOLE_DISPLAY_STATE — canonical MSDN example GUID.
_GUID_DISPLAY = (ctypes.c_byte * 16)(
    0x56, 0x95, 0x69, 0x6F, 0x4A, 0x70, 0xA0, 0x47,
    0x8F, 0x24, 0xC2, 0x8D, 0x93, 0x6F, 0xDA, 0x47,
)

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


# ── Structs ────────────────────────────────────────────────────────────────
class _POINT(ctypes.Structure):
    _fields_ = [("x", wt.LONG), ("y", wt.LONG)]


class _MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wt.HWND),
        ("message", wt.UINT),
        ("wParam", wt.WPARAM),
        ("lParam", wt.LPARAM),
        ("time", wt.DWORD),
        ("pt", _POINT),
    ]


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _POWERBROADCAST_SETTING(ctypes.Structure):
    _fields_ = [
        ("PowerSetting", _GUID),
        ("DataLength", ctypes.c_ulong),
        ("Data", ctypes.c_ubyte * 1),  # variable; we only inspect a DWORD
    ]


# LRESULT and c_int are not exposed via ctypes.wintypes on Py 3.14 — define
# our own aliases. LRESULT is pointer-sized on x64 (long), int on x86.
LRESULT = ctypes.c_long
_c_int = ctypes.c_int

WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class _WNDCLASSEX(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.UINT),
        ("style", wt.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", _c_int),
        ("cbWndExtra", _c_int),
        ("hInstance", wt.HINSTANCE),
        ("hIcon", wt.HANDLE),
        ("hCursor", wt.HANDLE),
        ("hbrBackground", wt.HANDLE),
        ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR),
        ("hIconSm", wt.HANDLE),
    ]


# ── Function prototypes ────────────────────────────────────────────────────
user32.CreateWindowExW.restype = wt.HWND
user32.CreateWindowExW.argtypes = [
    wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
    _c_int, _c_int, _c_int, _c_int,
    wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID,
]
user32.RegisterClassExW.restype = wt.ATOM
user32.RegisterClassExW.argtypes = [ctypes.c_void_p]
user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.GetMessageW.restype = wt.BOOL
user32.GetMessageW.argtypes = [ctypes.c_void_p, wt.HWND, wt.UINT, wt.UINT]
user32.DispatchMessageW.restype = LRESULT
user32.DispatchMessageW.argtypes = [ctypes.c_void_p]
user32.TranslateMessage.restype = wt.BOOL
user32.TranslateMessage.argtypes = [ctypes.c_void_p]
user32.RegisterPowerSettingNotification.restype = wt.HANDLE
user32.RegisterPowerSettingNotification.argtypes = [
    wt.HANDLE, ctypes.c_void_p, wt.DWORD,
]
user32.UnregisterPowerSettingNotification.restype = wt.BOOL
user32.UnregisterPowerSettingNotification.argtypes = [wt.HANDLE]
user32.DestroyWindow.restype = wt.BOOL
user32.DestroyWindow.argtypes = [wt.HWND]
user32.PostMessageW.restype = wt.BOOL
user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]


# ── Public API ─────────────────────────────────────────────────────────────
class PowerMonitor:
    """Edge-triggered Windows suspend/resume listener.

    Usage::

        mon = PowerMonitor()
        mon.start()                              # spawns pump thread
        ...
        if mon.consume_suspend(timeout=0):       # non-blocking
            ...                                  # close USB cleanly
        if mon.consume_resume(timeout=0):
            log.info("woke up")
        ...
        mon.stop()                               # posts WM_QUIT
    """

    def __init__(self) -> None:
        self._sleep_evt = threading.Event()
        self._resume_evt = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._wndproc_ref: Optional[WNDPROC] = None  # keep alive
        self._hwnd: Optional[int] = None
        self._notify_handle: Optional[int] = None
        self._ready = threading.Event()
        self._stopped = threading.Event()
        # Unique class name so re-runs (and tests) don't collide.
        self._class_name = f"TrofeoVisionPower_{id(self):x}"

    # ── Public ─────────────────────────────────────────────────────────
    def start(self) -> None:
        """Start the pump thread. Idempotent."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stopped.clear()
        self._ready.clear()
        self._thread = threading.Thread(
            target=self._pump, name="PowerMonitor", daemon=True,
        )
        self._thread.start()
        # Don't block forever — the pump may legitimately fail to register
        # (e.g. no GUI session, missing privileges).
        if not self._ready.wait(timeout=2.0):
            log.warning("PowerMonitor pump did not signal ready within 2s")

    def stop(self) -> None:
        """Stop the pump thread. Safe to call multiple times."""
        if self._hwnd is not None:
            try:
                user32.PostMessageW(wt.HWND(self._hwnd), WM_QUIT, 0, 0)
            except Exception:
                log.exception("PowerMonitor: PostMessageW failed")
        self._stopped.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            if self._thread.is_alive():
                log.warning("PowerMonitor thread did not exit within 2s")

    def consume_suspend(self, timeout: Optional[float] = None) -> bool:
        """Block until a suspend event arrives, then clear it.
        Edge-triggered — a second suspend without an intervening resume
        is collapsed into the first.
        """
        fired = self._sleep_evt.wait(timeout)
        if fired:
            self._sleep_evt.clear()
        return fired

    def consume_resume(self, timeout: Optional[float] = None) -> bool:
        """Block until a resume event arrives, then clear it."""
        fired = self._resume_evt.wait(timeout)
        if fired:
            self._resume_evt.clear()
        return fired

    @property
    def sleep_evt(self) -> threading.Event:
        """Raw Event handle for use in multi-event waits (e.g.
        ``stop_evt.is_set() or sleep_evt.is_set()``)."""
        return self._sleep_evt

    @property
    def resume_evt(self) -> threading.Event:
        return self._resume_evt

    # ── Win32 internals ────────────────────────────────────────────────
    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_POWERBROADCAST:
            if wparam == PBT_APMSUSPEND:
                log.info("[POWER] PBT_APMSUSPEND — system about to suspend")
                self._sleep_evt.set()
            elif wparam == PBT_APMRESUMEAUTOMATIC:
                log.info("[POWER] PBT_APMRESUMEAUTOMATIC — system resumed")
                self._resume_evt.set()
            elif wparam == PBT_APMRESUMESUSPEND:
                log.info("[POWER] PBT_APMRESUMESUSPEND — user-resumed wake")
                self._resume_evt.set()
            elif wparam == PBT_POWERSETTINGCHANGE:
                # GUID-registered event. We registered for
                # GUID_CONSOLE_DISPLAY_STATE so this fires when the monitor
                # turns back on after wake. Treat it as a wake signal too.
                if lparam:
                    pb = ctypes.cast(
                        lparam, ctypes.POINTER(_POWERBROADCAST_SETTING)
                    )[0]
                    state = int(pb.Data[0]) if pb.DataLength else -1
                    log.info(
                        "[POWER] PBT_POWERSETTINGCHANGE — display state=%d",
                        state,
                    )
                    if state in (1, 2):  # on or dimmed = back from sleep
                        self._resume_evt.set()
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _pump(self) -> None:
        """Daemon thread body: register an invisible top-level window
        and pump Win32 messages until ``WM_QUIT`` arrives.
        """
        try:
            hinst = wt.HINSTANCE(kernel32.GetModuleHandleW(None))

            wndproc = WNDPROC(self._wndproc)
            self._wndproc_ref = wndproc  # protect from GC

            wc = _WNDCLASSEX()
            wc.cbSize = ctypes.sizeof(_WNDCLASSEX)
            wc.lpfnWndProc = wndproc
            wc.hInstance = hinst
            wc.lpszClassName = self._class_name

            atom = user32.RegisterClassExW(ctypes.byref(wc))
            if not atom:
                err = ctypes.get_last_error()
                if err != 1410:  # ERROR_CLASS_ALREADY_EXISTS is harmless
                    log.error("RegisterClassExW failed (err=%d)", err)
                    self._ready.set()  # unblock start()
                    return

            class_p = ctypes.c_wchar_p(self._class_name)
            # Real top-level window so WM_POWERBROADCAST *broadcast* events
            # (PBT_APMSUSPEND etc.) get delivered. WS_EX_TOOLWINDOW keeps it
            # off the taskbar/alt-tab; WS_EX_TRANSPARENT prevents hit-testing.
            # Size 0×0 + WS_POPUP = invisible but top-level = receives
            # broadcasts.
            hwnd = user32.CreateWindowExW(
                WS_EX_TOOLWINDOW | WS_EX_TRANSPARENT,
                class_p, None,
                WS_POPUP,                   # no title bar, no caption
                0, 0, 0, 0,                 # x, y, w, h — all zero
                HWND_TOP, None, hinst, None,
            )
            if not hwnd:
                err = ctypes.get_last_error()
                log.error("CreateWindowExW failed (err=%d)", err)
                self._ready.set()
                return
            self._hwnd = int(hwnd)

            # Also register for GUID_CONSOLE_DISPLAY_STATE so we get an
            # extra wake signal when the monitor turns back on. Cheap and
            # belt-and-braces: PBT_APMRESUMEAUTOMATIC is usually enough,
            # but this catches the "monitor was off during wake" edge case.
            nh = user32.RegisterPowerSettingNotification(
                hwnd, ctypes.byref(_GUID_DISPLAY), DEVICE_NOTIFY_WINDOW_HANDLE,
            )
            if nh:
                self._notify_handle = int(nh)
            else:
                err = ctypes.get_last_error()
                log.warning(
                    "RegisterPowerSettingNotification failed (err=%d); "
                    "PBT_APMSUSPEND/PMT_APMRESUME broadcasts still work",
                    err,
                )

            log.info(
                "Power monitor registered (hwnd=%d, class=%s, notify=%s)",
                self._hwnd, self._class_name,
                "ok" if self._notify_handle else "unavailable",
            )
            self._ready.set()

            msg = _MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))

            try:
                if self._notify_handle:
                    user32.UnregisterPowerSettingNotification(
                        wt.HANDLE(self._notify_handle)
                    )
                    self._notify_handle = None
            except Exception:
                pass
            try:
                user32.DestroyWindow(hwnd)
            except Exception:
                pass
            self._hwnd = None
            log.info("Power monitor message loop exited")
        except Exception:
            log.exception("PowerMonitor pump crashed")
            self._ready.set()