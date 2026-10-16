"""Windows system tray launcher for the Trofeo Vision dashboard.

Designed to be launched via ``pythonw.exe`` from ``start.bat`` so no
console window pops up. The render loop runs in a background thread
and the user controls it from a right-click tray menu.

Tray menu:
    Status: <state>          read-only label, reflects current state
    Pause                   pauses frame pushes (visible only while running)
    Resume                  resumes frame pushes (visible only while paused)
    Open log folder         opens ``<repo>/logs/`` in Explorer
    ─────
    Quit                    stops the render thread + closes USB + exits

Logs go to ``<repo>/logs/trofeo_vision.log`` (rotated, 1 MB × 3 backups)
since pythonw has no console to print to.
"""

from __future__ import annotations

import logging
import logging.handlers
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw
import pystray
from pystray import Menu, MenuItem

from .config import Config, TopTileConfig
from .driver import Driver, DriverError
from .history import HistoryBuffer
from .power import PowerMonitor
from .renderer import Renderer
from .sensors import SensorReader
from .sensors_types import SensorSnapshot
from .weather import WeatherClient, WeatherImages
from .air_quality import AirQualityClient, AirQualityImages


REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = REPO_ROOT / "logs"
LOG_FILE = LOG_DIR / "trofeo_vision.log"


log = logging.getLogger("trofeo_vision")


# ─────────────────────────────────────────────────────────────────────────────
# Logging — file only (no console: pythonw has no stdout).
# ─────────────────────────────────────────────────────────────────────────────
def setup_logging(verbose: bool = False) -> None:
    """Configure logging to ``logs/trofeo_vision.log`` with rotation.

    Called once at app startup. Idempotent — replaces existing handlers
    so a relaunch from the same process doesn't double-log.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    # Drop any existing handlers (e.g. defaults installed by logging.basicConfig
    # in other modules) so we don't double-log.
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    ))
    root.addHandler(handler)
    # Make pystray quieter at INFO — its DEBUG chatter is noisy.
    logging.getLogger("pystray").setLevel(logging.WARNING)


# ─────────────────────────────────────────────────────────────────────────────
# Tray icon — drawn at startup with PIL (no static asset to maintain).
# ─────────────────────────────────────────────────────────────────────────────
def build_icon(state: str = "running") -> Image.Image:
    """Generate a 64×64 RGBA tray icon: dark rounded square + USE/TEMP/PWR
    dots + a small activity wave. State is reserved for future use
    (e.g. dimming when paused); the icon is currently identical.
    """
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # Body: dark rounded square (matches the dashboard's dark-theme card).
    d.rounded_rectangle((4, 4, size - 5, size - 5),
                        radius=10, fill=(18, 18, 22, 255),
                        outline=(60, 60, 70, 255), width=2)
    # Three series dots, left-to-right: USE / TEMP / PWR colors from
    # Theme.dark() in renderer.py — keeps the icon visually linked to
    # the dashboard it drives.
    d.ellipse((13, 18, 25, 30), fill=(220, 220, 230, 255))   # USE
    d.ellipse((27, 18, 39, 30), fill=(240, 160, 80, 255))    # TEMP
    d.ellipse((41, 18, 53, 30), fill=(100, 170, 230, 255))   # PWR
    # Activity wave at the bottom.
    d.line([(12, 46), (20, 40), (28, 48), (38, 42), (52, 46)],
           fill=(220, 220, 230, 255), width=2)
    return img


# ─────────────────────────────────────────────────────────────────────────────
# TrayApp — owns the icon and the render thread.
# ─────────────────────────────────────────────────────────────────────────────
class TrayApp:
    """Tray icon + background render thread.

    Lifecycle::

        app = TrayApp(cfg, rate=2.0)
        app.run()    # blocks until Quit is clicked

    Threading model:
        * Main thread: pystray's Windows message loop (blocks in icon.run()).
        * Render thread (daemon): drives sensors + renderer + USB push.
        * Pause/Resume uses a threading.Event so the render thread can
          block cheaply without busy-looping.
        * State changes go through ``_lock`` so the menu callbacks
          (running on the main thread) can't race the render thread.
    """

    STATE_STOPPED = "stopped"
    STATE_STARTING = "starting"
    STATE_RUNNING = "running"
    STATE_PAUSED = "paused"
    STATE_RECONNECTING = "reconnecting"   # USB device dropped (e.g. PC sleep/wake) — retrying
    STATE_ERROR = "error"

    # ── USB ACCESS DENIED watchdog ─────────────────────────────────────────
    # The recurring "USB stuck after sleep/wake" shape is libusb holding a
    # stale claim that Windows won't release. Without escalation this spins
    # forever producing thousands of identical log lines. After
    # BACKOFF_THRESHOLD consecutive ACCESS DENIEDs (~60 s @ 2 s), back off
    # to LONG_BACKOFF_S. After GIVEUP_THRESHOLD total cycles (~6 min),
    # give up so the tray surfaces ERROR and the user can Force Refresh.
    ACCESS_DENIED_BACKOFF_THRESHOLD = 30
    ACCESS_DENIED_LONG_BACKOFF_S = 10.0
    ACCESS_DENIED_GIVEUP_THRESHOLD = 60

    def __init__(self, cfg: Config, rate: Optional[float] = None,
                 verbose: bool = False):
        self.cfg = cfg
        self.rate = float(rate) if rate else cfg.usb.target_fps
        self.verbose = verbose

        self._lock = threading.Lock()
        self._state = self.STATE_STARTING
        self._error_msg = ""
        # Active top-tile mode — initialized from config, flipped by the
        # tray radio. The render thread reads it off self.cfg.top_tile.mode
        # per frame; this field is the lock-protected source of truth for
        # the menu bullet state.
        self._top_tile_mode: str = cfg.top_tile.mode
        # Pause event: set() = running, clear() = paused. Render loop
        # waits on it before each frame.
        self._pause_evt = threading.Event()
        self._pause_evt.set()
        # Stop event: signals the render thread to exit.
        self._stop_evt = threading.Event()
        # Reconnect-log throttle: 0.0 means "no throttling yet, log the
        # next failure immediately". Updated in _open_with_reconnect.
        self._reconnect_last_log_at: float = 0.0
        # Consecutive ACCESS DENIED count for the watchdog. Reset on
        # success or Force Refresh. Only errno-13 failures count — other
        # errors (device not found, etc.) don't trigger escalation.
        self._access_denied_count: int = 0
        # Force-refresh request: set from the tray menu callback, consumed
        # + cleared by the render thread / reconnect loop. Setting it
        # also kicks a fresh render thread if the previous one gave up.
        self._force_refresh_evt = threading.Event()

        self._render_thread: Optional[threading.Thread] = None
        self._sensors: Optional[SensorReader] = None
        self._driver: Optional[Driver] = None

        # Sleep/wake detector — fires before the USB stack dies so we can
        # close the device cleanly, and again on wake so we reconnect fast.
        self._power = PowerMonitor()
        self._power.start()

        # Icon + menu are built once; pystray re-evaluates the menu's
        # visible/enabled lambdas on every update_menu() call.
        self._icon_image = build_icon()
        self._menu = self._build_menu()
        self._icon: Optional[pystray.Icon] = None

    # ── Menu ─────────────────────────────────────────────────────────────
    def _build_menu(self) -> Menu:
        # Status label is dynamic so the user can see *when* the app
        # drops into RECONNECTING (typically after a PC sleep/wake) or
        # into ERROR. The label lambda re-evaluates on every
        # icon.update_menu() call (pystray calls it on the UI thread).
        # Other items are static; their visibility lambdas re-evaluate
        # on every icon.update_menu() call.
        return Menu(
            MenuItem(
                text=lambda _: f"Status: {self._format_state(self._safe_state())}",
                action=None,
                enabled=False,
            ),
            MenuItem("Pause",  self._on_pause,
                     visible=lambda _: self._safe_state() == self.STATE_RUNNING),
            MenuItem("Resume", self._on_resume,
                     visible=lambda _: self._safe_state() == self.STATE_PAUSED),
            MenuItem("Force Refresh", self._on_force_refresh,
                     # Always visible except during initial startup so the
                     # user can manually kick a fresh USB recovery at any
                     # time — most useful when state is RECONNECTING or
                     # ERROR/STUCK, but harmless from RUNNING (just causes
                     # a brief blip as the driver closes + reopens).
                     visible=lambda _: self._safe_state() != self.STATE_STARTING),
            Menu.SEPARATOR,
            # Top-tile selector. Radio items: pystray draws the active
            # choice with a bullet (●) so the user sees which payload
            # the dashboard is currently showing. Picking either item
            # flips [top_tile].mode in config.toml and the next frame
            # picks up the new payload — no restart needed.
            MenuItem("Weather", lambda i, it: self._on_choose_top_tile("weather"),
                     radio=True,
                     checked=lambda _: self._safe_top_tile_mode() == "weather"),
            MenuItem("Air Quality", lambda i, it: self._on_choose_top_tile("air_quality"),
                     radio=True,
                     checked=lambda _: self._safe_top_tile_mode() == "air_quality"),
            Menu.SEPARATOR,
            MenuItem("Open log folder", self._on_open_log),
            Menu.SEPARATOR,
            MenuItem("Quit", self._on_quit),
        )

    def _safe_state(self) -> str:
        with self._lock:
            return self._state

    def _safe_state_error(self) -> str:
        with self._lock:
            return self._error_msg[:80]

    def _safe_top_tile_mode(self) -> str:
        """Return the active top-tile mode under the lock.

        pystray's ``checked`` lambda runs on the UI thread, but the
        render thread reads ``self.cfg.top_tile.mode`` directly (no lock
        needed — frozen dataclass + GIL atomic attr read). Keeping the
        UI-side helper lock-protected means the radio bullet reflects
        whichever value the latest menu click set, without flickering.
        """
        with self._lock:
            return self._top_tile_mode

    def _format_state(self, state: str) -> str:
        """Map an internal state to its menu-label string. Called from
        the label lambda on the UI thread; takes ``state`` already
        extracted under the lock so this helper stays lock-free and
        lock-safe (no nested lock acquisition).
        """
        if state == self.STATE_RECONNECTING:
            return "reconnecting — waiting for device"
        if state == self.STATE_ERROR:
            return f"error: {self._safe_state_error()}"
        return state  # starting / running / paused / stopped

    def _set_state(self, new_state: str, error_msg: str = "") -> None:
        with self._lock:
            self._state = new_state
            self._error_msg = error_msg
        self._refresh_menu()

    def _refresh_menu(self) -> None:
        if self._icon is not None:
            # Re-evaluates the visible/enabled lambdas + the Status label.
            self._icon.update_menu()

    # ── Menu callbacks (run on the main / UI thread) ──────────────────────
    def _on_pause(self, _icon, _item) -> None:
        if self._safe_state() != self.STATE_RUNNING:
            return
        self._pause_evt.clear()
        self._set_state(self.STATE_PAUSED)
        log.info("Paused")

    def _on_resume(self, _icon, _item) -> None:
        if self._safe_state() != self.STATE_PAUSED:
            return
        self._pause_evt.set()
        self._set_state(self.STATE_RUNNING)
        log.info("Resumed")

    def _on_force_refresh(self, _icon, _item) -> None:
        """User-initiated USB recovery from the tray menu.

        Closes the current driver (releases any stale libusb handle from
        a pre-suspend state), resets the ACCESS DENIED watchdog counter,
        and triggers a fresh ``open()`` cycle. If the render thread had
        already exited (e.g. the watchdog gave up), this also restarts
        it so they don't need to Quit + relaunch.

        Safe to click from any non-STARTING state:
            - RUNNING       → brief blip, driver closes + reopens, push resumes
            - RECONNECTING  → bypasses the current backoff, retries immediately
            - PAUSED        → resets the counter so a subsequent Resume
                              starts from a clean state
            - ERROR/STUCK   → restarts the render thread with a fresh Driver
        """
        log.info("Force refresh requested from tray menu")
        self._force_refresh_evt.set()
        with self._lock:
            thread_dead = (self._render_thread is None
                           or not self._render_thread.is_alive())
            state = self._state
        if thread_dead and state in (self.STATE_ERROR, self.STATE_STOPPED):
            log.info("Force refresh: render thread is stopped, restarting")
            self._start_render_thread()
        self._refresh_menu()

    def _on_choose_top_tile(self, mode: str) -> None:
        """Tray radio callback: switch the top tile between weather and
        air quality, persist the choice to config.toml.

        The render thread picks up the new mode on the next frame — no
        USB churn, no driver close/reopen, no restart. Persistence
        happens via ``Config.save()`` so the choice survives a relaunch.
        """
        if mode not in ("weather", "air_quality"):
            log.warning("Ignoring unknown top-tile mode: %s", mode)
            return
        with self._lock:
            if self._top_tile_mode == mode:
                return
            self._top_tile_mode = mode
            # Config is frozen — build a new instance with the updated
            # mode. The render thread reads self.cfg.top_tile.mode each
            # frame, so this swap is the only state the render loop cares
            # about; the rest of cfg is unchanged.
            self.cfg = Config(
                usb=self.cfg.usb,
                weather=self.cfg.weather,
                air_quality=self.cfg.air_quality,
                top_tile=TopTileConfig(mode=mode),
                greeting=self.cfg.greeting,
                display=self.cfg.display,
                theme=self.cfg.theme,
            )
        log.info("Top tile mode → %s", mode)
        try:
            self.cfg.save()
        except Exception:  # noqa: BLE001
            log.exception("Failed to persist top_tile mode to config.toml "
                          "(the choice still applies for this session)")
        self._refresh_menu()

    def _on_open_log(self, _icon, _item) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        # explorer.exe opens the folder highlighted.
        subprocess.Popen(["explorer", str(LOG_DIR)])

    def _on_quit(self, _icon, _item) -> None:
        log.info("Quit requested — stopping render thread")
        # Unblock the render thread if it's currently paused.
        self._stop_evt.set()
        self._pause_evt.set()
        # Stop is fired inside _render_loop's finally block via _stop_evt.
        if self._icon is not None:
            self._icon.stop()

    # ── Render thread ────────────────────────────────────────────────────
    def _start_render_thread(self) -> None:
        self._stop_evt.clear()
        self._render_thread = threading.Thread(
            target=self._render_loop, name="RenderLoop", daemon=True,
        )
        self._render_thread.start()

    def _stop_render_thread(self) -> None:
        if self._render_thread and self._render_thread.is_alive():
            self._render_thread.join(timeout=3.0)

    def _render_loop(self) -> None:
        """Background thread: drives sensors + renderer + USB push."""
        try:
            self._set_state(self.STATE_STARTING)

            sensors = SensorReader(
                interval=self.cfg.usb.sensor_refresh_seconds, enable_lhm=True,
            )
            weather = WeatherClient(self.cfg.weather)
            air_quality = AirQualityClient(self.cfg.air_quality)
            assets_dir = (Path(self.cfg.weather.assets_dir)
                          if self.cfg.weather.assets_dir
                          else WeatherImages.default_assets_dir())
            weather_images = WeatherImages(assets_dir)
            aq_assets_dir = (Path(self.cfg.air_quality.assets_dir)
                             if self.cfg.air_quality.assets_dir
                             else AirQualityImages.default_assets_dir())
            air_quality_images = AirQualityImages(aq_assets_dir)
            history = HistoryBuffer()
            renderer = Renderer(self.cfg.display.width, self.cfg.display.height)
            driver = Driver(jpeg_quality=self.cfg.display.jpeg_quality)

            self._sensors = sensors
            self._driver = driver

            sensors.start()

            # Initial USB open. If the device is missing on startup (e.g.
            # PC just woke from sleep, or TRCC is still lingering), keep
            # retrying instead of bailing out — the tray app's whole job
            # is to stay alive in the background.
            if not self._open_with_reconnect(driver, self.RECONNECT_BACKOFF_S):
                return  # stopped while waiting for the device

            self._set_state(self.STATE_RUNNING)
            log.info("Live push started at %.1f fps", self.rate)

            sleep_s = 1.0 / max(0.1, self.rate)
            # snapshot_with_weather lives in main.py — duplicate the small
            # bit we need rather than import main (which pulls in argparse
            # + signal handlers designed for console mode).
            while not self._stop_evt.is_set():
                # Block cheaply while paused; unblocked by _on_resume or
                # _on_quit (which both set the event).
                self._pause_evt.wait()
                if self._stop_evt.is_set():
                    break

                # ── Force refresh ───────────────────────────────────────
                # User-initiated from the tray menu. Close the driver to
                # release any stale handle from a pre-suspend state, reset
                # the ACCESS DENIED watchdog, then fall into the normal
                # reconnect path. Doing this proactively (instead of
                # waiting for the next push to fail) keeps the blip short.
                if self._force_refresh_evt.is_set():
                    self._force_refresh_evt.clear()
                    log.info("Force refresh — closing USB device and reconnecting")
                    self._access_denied_count = 0
                    try:
                        driver.close()
                    except Exception:
                        log.exception("Force refresh: error closing driver")
                    self._set_state(self.STATE_RECONNECTING,
                                    "force refresh from tray menu")
                    if not self._open_with_reconnect(
                            driver, self.RECONNECT_BACKOFF_S):
                        return
                    try:
                        self._set_state(self.STATE_RUNNING)
                    except Exception:  # noqa: BLE001
                        log.exception(
                            "Force refresh: post-reconnect set_state failed",
                        )
                    continue  # skip the per-frame sleep + skip a frame

                # ── Power events ────────────────────────────────────────
                # Check suspend/resume between frames. Suspend fires BEFORE
                # the USB stack goes down, so we can release the device
                # cleanly instead of waiting for the next push to error out.
                # Resume simply logs — the existing reconnect retry loop
                # will pick the device back up on its next attempt.
                if self._power.consume_suspend(timeout=0):
                    log.warning("[SLEEP] System about to suspend — "
                                "closing USB device cleanly")
                    try:
                        driver.close()
                    except Exception:
                        log.exception("Error closing driver before suspend")
                    self._set_state(self.STATE_RECONNECTING,
                                    "system suspended")
                    # Loop into the existing reconnect retry path; it
                    # will block on open() errors until the device comes
                    # back on wake. Stop event still honored.
                    if not self._open_with_reconnect(
                            driver, self.RECONNECT_BACKOFF_S):
                        return
                    try:
                        self._set_state(self.STATE_RUNNING)
                    except Exception:  # noqa: BLE001
                        log.exception("Post-reconnect set_state failed")
                    continue  # skip the per-frame sleep + skip a frame

                if self._power.consume_resume(timeout=0):
                    log.info("[WAKE] System resumed from sleep")
                    # No special action — the reconnect loop (if any)
                    # already has the device or will on its next 2s tick.

                try:
                    snap = sensors.snapshot
                    # Refresh weather + air quality only when the snapshot
                    # doesn't already have a live value — the sensor reader
                    # may pre-fill these when it ticks, but on a fresh
                    # process (or after the sensor reader skipped a tick)
                    # we want to ensure the tile has current data.
                    # Each fetch is wrapped so an API bug can never kill
                    # the frame loop (dead API → placeholder tile).
                    if (not hasattr(snap.weather, "available")
                            or not snap.weather.available):
                        try:
                            fresh_weather = weather.get()
                        except Exception as e:  # noqa: BLE001
                            log.warning("Weather get() raised (%s) — "
                                        "keeping previous value", e)
                            fresh_weather = snap.weather
                        snap = SensorSnapshot(
                            timestamp=snap.timestamp,
                            cpu=snap.cpu, gpu=snap.gpu, mem=snap.mem,
                            net=snap.net,
                            weather=fresh_weather,
                            air_quality=snap.air_quality,
                        )
                    if (not hasattr(snap.air_quality, "available")
                            or not snap.air_quality.available):
                        try:
                            fresh_aq = air_quality.get()
                        except Exception as e:  # noqa: BLE001
                            log.warning("Air quality get() raised (%s) — "
                                        "keeping previous value", e)
                            fresh_aq = snap.air_quality
                        snap = SensorSnapshot(
                            timestamp=snap.timestamp,
                            cpu=snap.cpu, gpu=snap.gpu, mem=snap.mem,
                            net=snap.net,
                            weather=snap.weather,
                            air_quality=fresh_aq,
                        )
                    history.push(snap)
                    # self.cfg.top_tile.mode is the source of truth — it
                    # updates atomically when the tray radio fires. No
                    # lock needed on the render thread side.
                    renderer.render(
                        snap,
                        history=history,
                        greeting_name=self.cfg.greeting.name,
                        weather_images=weather_images,
                        theme_cfg=self.cfg.theme,
                        top_tile_mode=self.cfg.top_tile.mode,
                        air_quality_images=air_quality_images,
                    )
                    jpeg = driver.encode_jpeg(renderer.image)
                    stats = driver.push_frame(jpeg)
                    log.debug("Frame: %d chunks, %d bytes, %.1f ms",
                              stats.chunks, stats.bytes, stats.duration_ms)
                except DriverError as e:
                    # USB went bad mid-loop — typical after the PC sleeps
                    # and the device detaches. Drop into the reconnect
                    # loop; the outer `while` continues once we're back.
                    log.warning("USB push failed (%s) — reconnecting", e)
                    try:
                        driver.close()
                    except Exception:
                        log.exception("Error closing driver during reconnect")
                    if not self._open_with_reconnect(driver, self.RECONNECT_BACKOFF_S):
                        return  # stopped during reconnect
                    # Post-reconnect state flip + menu refresh. Wrap in
                    # its own try so a stray COM / UI exception here
                    # doesn't escape the outer DriverError handler and
                    # get reclassified as "Render loop crashed" — the
                    # driver is already back, keep pushing frames.
                    try:
                        log.info("USB reconnected — resuming push")
                        self._set_state(self.STATE_RUNNING)
                    except Exception as inner_exc:  # noqa: BLE001
                        log.exception(
                            "Error post-reconnect (continuing push): %s",
                            inner_exc,
                        )
                    continue  # skip the per-frame sleep after a reconnect
                except Exception:  # noqa: BLE001 — non-USB failure (render,
                    # sensor, weather/AQI): log + keep pushing next frame.
                    # Only DriverError triggers the USB reconnect path above.
                    log.exception("Frame failed (non-USB) — continuing push")
                    # Fall through to the interruptible sleep below so a
                    # persistently failing frame can't hot-spin the loop.

                # Sleep but stay responsive to stop/pause.
                self._interruptible_sleep(sleep_s)

        except Exception as e:  # noqa: BLE001
            log.exception("Render loop crashed")
            self._set_state(self.STATE_ERROR, f"{type(e).__name__}: {e}")
        finally:
            self._cleanup()
            if self._safe_state() != self.STATE_ERROR:
                self._set_state(self.STATE_STOPPED)
            log.info("Render loop stopped")

    # ── Reconnect helpers (sleep/wake resilience) ─────────────────────────
    RECONNECT_BACKOFF_S = 2.0  # how long to wait between open() retries

    def _open_with_reconnect(self, driver: Driver, backoff_s: float) -> bool:
        """Try to open the driver; on failure, retry with backoff until
        success, stop, or the ACCESS DENIED watchdog gives up.

        Returns ``True`` if ``driver.open()`` succeeded, ``False`` if the
        user asked to quit while we were waiting OR the watchdog
        escalated past ``ACCESS_DENIED_GIVEUP_THRESHOLD``. While waiting
        the state is set to ``RECONNECTING`` so the tray menu surfaces
        it; on giveup the state flips to ``ERROR``.

        Logging is throttled to once every 30 s so an hour of retries
        doesn't produce 1800 identical log lines. State flips and the
        ``_wait_or_stop`` sleep still happen on every retry.

        Force-refresh: ``_force_refresh_evt`` is checked at the top of
        each loop iteration. Setting it (from the tray menu) resets the
        ACCESS DENIED watchdog, closes the driver to release any stale
        handle, and falls through to a fresh ``driver.open()`` attempt
        without waiting for the current backoff.
        """
        while not self._stop_evt.is_set():
            # Force-refresh: user wants a fresh attempt RIGHT NOW.
            if self._force_refresh_evt.is_set():
                self._force_refresh_evt.clear()
                log.info("Force refresh — resetting USB recovery state")
                self._access_denied_count = 0
                try:
                    driver.close()
                except Exception:
                    log.debug("Force refresh: driver.close() raised",
                              exc_info=True)

            try:
                driver.open()
            except DriverError as e:
                self._set_state(self.STATE_RECONNECTING, str(e))

                # Track ACCESS DENIED specifically (errno 13). All other
                # failures (device-not-found, etc.) keep the normal
                # backoff and don't count toward the watchdog — only the
                # persistent-stale-claim storm should escalate.
                is_access_denied = getattr(e, "errno", None) == 13
                if is_access_denied:
                    self._access_denied_count += 1
                else:
                    self._access_denied_count = 0

                # Escalate the backoff once the storm has lasted long
                # enough to be clearly a stuck-handle issue.
                current_backoff = (
                    self.ACCESS_DENIED_LONG_BACKOFF_S
                    if self._access_denied_count >= self.ACCESS_DENIED_BACKOFF_THRESHOLD
                    else backoff_s
                )

                # Throttled log — one per 30 s, plus the count for diagnostics.
                now = time.monotonic()
                if (self._reconnect_last_log_at == 0.0
                        or (now - self._reconnect_last_log_at) >= 30.0):
                    suffix = (f" (ACCESS DENIED x{self._access_denied_count}, "
                              f"backoff {current_backoff:.1f}s)"
                              if is_access_denied else "")
                    log.info("USB not available (%s)%s — retrying in %.1fs",
                             e, suffix, current_backoff)
                    self._reconnect_last_log_at = now

                # One-shot warning when we first cross the escalation
                # threshold so it's obvious in the log what changed.
                if self._access_denied_count == self.ACCESS_DENIED_BACKOFF_THRESHOLD:
                    log.warning(
                        "USB ACCESS DENIED %dx — escalating backoff to "
                        "%.1fs (was %.1fs). Likely stale libusb handle "
                        "from post-suspend; if this persists, Force "
                        "Refresh from the tray menu or Quit + relaunch.",
                        self._access_denied_count, current_backoff, backoff_s,
                    )

                # Watchdog giveup: stop spinning so the user can Force
                # Refresh or Quit instead of waiting forever.
                if self._access_denied_count >= self.ACCESS_DENIED_GIVEUP_THRESHOLD:
                    elapsed_min = int(round(
                        self.ACCESS_DENIED_BACKOFF_THRESHOLD * backoff_s / 60
                        + (self.ACCESS_DENIED_GIVEUP_THRESHOLD
                           - self.ACCESS_DENIED_BACKOFF_THRESHOLD)
                        * self.ACCESS_DENIED_LONG_BACKOFF_S / 60
                    ))
                    log.error(
                        "USB unreachable after %d ACCESS DENIED attempts "
                        "(~%d min). Giving up — stale handle suspected. "
                        "Use 'Force Refresh' in the tray menu, or Quit "
                        "and relaunch via start.bat / the TrofeoVision "
                        "scheduled task.",
                        self._access_denied_count, elapsed_min,
                    )
                    self._set_state(
                        self.STATE_ERROR,
                        f"USB unreachable after {self._access_denied_count} "
                        "ACCESS DENIED attempts. Force Refresh or Quit + "
                        "relaunch.",
                    )
                    return False

                if self._wait_or_stop(current_backoff):
                    return False
                continue
            # Success — reset both throttles so the next failure series
            # logs its first attempt immediately and the watchdog starts
            # counting from zero.
            self._reconnect_last_log_at = 0.0
            self._access_denied_count = 0
            return True
        return False

    def _wait_or_stop(self, seconds: float) -> bool:
        """Sleep ``seconds`` but return early on stop. Returns ``True`` if
        the stop event fired during the wait.

        Unlike ``_interruptible_sleep``, this only watches the *stop*
        event — reconnect backoff should keep trying even while the user
        has the app paused, so the next resume picks up immediately.
        """
        step = 0.1
        elapsed = 0.0
        while elapsed < seconds:
            if self._stop_evt.is_set():
                return True
            time.sleep(step)
            elapsed += step
        return self._stop_evt.is_set()

    def _interruptible_sleep(self, seconds: float) -> None:
        """Sleep ``seconds`` but wake immediately if stop or pause flips.

        Split into small chunks so Quit and Pause feel instant. Polling
        at 50 ms is cheap (vs. the 250 ms frame interval) and gives the
        main thread a sub-frame window to react.
        """
        step = 0.05
        end_at = seconds
        elapsed = 0.0
        while elapsed < end_at:
            if self._stop_evt.is_set() or not self._pause_evt.is_set():
                return
            time.sleep(step)
            elapsed += step

    def _cleanup(self) -> None:
        """Release USB + stop sensors. Safe to call multiple times."""
        try:
            if self._driver is not None:
                self._driver.close()
        except Exception:  # noqa: BLE001
            log.exception("Error closing driver")
        try:
            if self._sensors is not None:
                self._sensors.stop()
        except Exception:  # noqa: BLE001
            log.exception("Error stopping sensors")
        try:
            self._power.stop()
        except Exception:  # noqa: BLE001
            log.exception("Error stopping power monitor")

    # ── Public entry ─────────────────────────────────────────────────────
    def run(self) -> None:
        """Start the render thread and pump Windows messages. Blocks
        until Quit is clicked (or the icon is stopped programmatically).
        """
        setup_logging(verbose=self.verbose)
        log.info("Trofeo Vision tray starting (rate=%.1f fps)", self.rate)
        self._start_render_thread()
        icon = pystray.Icon(
            "TrofeoVision",
            self._icon_image,
            "Trofeo Vision Dashboard",
            menu=self._menu,
        )
        self._icon = icon
        try:
            icon.run()
        finally:
            self._stop_evt.set()
            self._pause_evt.set()
            self._stop_render_thread()
            self._cleanup()
