"""
HDR Screenshot Tool — main entry point.

Architecture
────────────
  • pystray runs the tray icon in the *main* thread (required on Windows).
    Its `setup` callback fires once the icon is ready; everything else starts
    from there.

  • pynput GlobalHotKeys runs in a background thread.  It is restarted
    whenever the user saves new hotkeys in Settings.

  • Each screenshot job runs in a short-lived daemon thread so the hotkey
    thread is never blocked.

  • The region-selection overlay (tkinter) runs in its own daemon thread so
    it can own a fresh Tk root without conflicting with pystray.
"""
import ctypes
import os
import sys
import threading
import time
from datetime import datetime

import pystray
from PIL import Image

import capture
import clipboard_win
import config as cfg
import hdr_detect
import notification
import settings_window
import tonemapping
import ui
from capture import MonitorInfo

# Seconds to wait after hiding the toolbar so the compositor produces a
# toolbar-free frame before we grab.
_TOOLBAR_SETTLE_S = 0.12

# ── Single-instance guard ─────────────────────────────────────────────────────

_MUTEX_NAME = "Global\\HDRScreenshotToolMutex"
_mutex_handle = None   # тримаємо посилання щоб GC не прибрав


def _ensure_single_instance() -> None:
    global _mutex_handle
    _mutex_handle = ctypes.windll.kernel32.CreateMutexW(None, False, _MUTEX_NAME)
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        ctypes.windll.user32.MessageBoxW(
            0,
            "HDR Screenshot Tool вже запущено.\nЗнайдіть іконку в системному треї.",
            "HDR Screenshot Tool",
            0x40 | 0x1000,  # MB_ICONINFORMATION | MB_SETFOREGROUND
        )
        sys.exit(0)


# ── State ─────────────────────────────────────────────────────────────────────
_config: dict = cfg.load()
_config_lock  = threading.Lock()

_hotkey_listener      = None
_hotkey_listener_lock = threading.Lock()

_capture_lock = threading.Lock()   # prevent overlapping captures

_icon: pystray.Icon | None = None  # set in main()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _notify(message: str, title: str = "HDR Screenshot",
            image_path: str | None = None) -> None:
    """Показує toast-сповіщення; при наявності image_path — з мініатюрою та кліком."""
    notification.show(title, message, image_path=image_path, fallback_icon=_icon)


def _hdr_label(monitor: MonitorInfo) -> str:
    """Return '[HDR]' or '[SDR]' based on the monitor's current mode."""
    try:
        return "[HDR]" if hdr_detect.is_hdr_on_monitor(monitor.idx) else "[SDR]"
    except Exception:
        return ""


# ── Screenshot workflow ───────────────────────────────────────────────────────

def _process_and_save(
    frame,
    monitor: MonitorInfo,
    save_folder: str,
    mode: str,
    tm_method: str,
    sdr_white_nits: float = 250.0,
) -> tuple[Image.Image, str]:
    """Tone-map / save files per *mode*.
    Returns (sdr_image, notify_path) where *notify_path* is the best path to
    show in the toast (SDR PNG preferred; HDR PNG as fallback)."""
    ts = _timestamp()
    os.makedirs(save_folder, exist_ok=True)

    sdr_img: Image.Image | None = None
    sdr_path: str | None = None
    hdr_path: str | None = None

    if mode in ("sdr", "both"):
        sdr_img = tonemapping.to_sdr(
            frame, method=tm_method, sdr_white_nits=sdr_white_nits
        )
        sdr_path = os.path.join(save_folder, f"hdr_sdr_{ts}.png")
        sdr_img.save(sdr_path, format="PNG")

    if mode in ("hdr", "both"):
        hdr_path = os.path.join(save_folder, f"hdr_raw_{ts}.png")
        tonemapping.save_hdr_png(frame, hdr_path)

    if sdr_img is None:
        sdr_img = tonemapping.to_sdr(
            frame, method=tm_method, sdr_white_nits=sdr_white_nits
        )

    notify_path = sdr_path or hdr_path or ""
    return sdr_img, notify_path


def _hide_toolbar_for_capture() -> bool:
    """Ensure the toolbar isn't in the shot.  On Windows 10 2004+ the bar is
    excluded from capture, so nothing needs to happen (fast path).  Otherwise
    withdraw it and let a toolbar-free frame settle.  Returns True if the bar
    was withdrawn and must be restored + a fresh grab forced."""
    if ui.toolbar_excluded_from_capture():
        return False
    was_visible = ui.is_toolbar_visible()
    if was_visible:
        ui.hide_toolbar_transient()
        time.sleep(_TOOLBAR_SETTLE_S)   # let the toolbar-free frame settle
    return was_visible


def _do_fullscreen() -> None:
    """Capture the monitor under the cursor, save, copy to clipboard."""
    if not _capture_lock.acquire(blocking=False):
        return
    was_visible = False
    try:
        with _config_lock:
            c = dict(_config)

        was_visible = _hide_toolbar_for_capture()
        mon = capture.cursor_monitor()
        frame = capture.grab(mon, fresh=was_visible)
        if frame is None:
            _notify("Capture failed — is dxcam installed?", "Error")
            return

        sdr_img, notify_path = _process_and_save(
            frame, mon, c["save_folder"], c["save_mode"], c["tonemapping"],
            sdr_white_nits=c.get("sdr_white_nits", 250),
        )

        clipboard_win.copy_image(sdr_img)
        label = _hdr_label(mon)
        _notify(
            f"{label} Monitor {mon.idx} saved → {c['save_folder']}",
            image_path=notify_path,
        )

    except Exception as exc:
        _notify(f"Error: {exc}", "Error")
    finally:
        if was_visible:
            ui.show_toolbar_transient()
        _capture_lock.release()


def _do_region() -> None:
    """Capture full frame, show region overlay on correct monitor, crop, save."""
    if not _capture_lock.acquire(blocking=False):
        return
    was_visible = False
    try:
        with _config_lock:
            c = dict(_config)

        was_visible = _hide_toolbar_for_capture()
        mon = capture.cursor_monitor()
        frame = capture.grab(mon, fresh=was_visible)
        if frame is None:
            _notify("Capture failed — is dxcam installed?", "Error")
            return

        preview = tonemapping.to_sdr(
            frame, method=c["tonemapping"],
            sdr_white_nits=c.get("sdr_white_nits", 250),
        )
        mode = c.get("capture_mode", "free")
        size = (int(c.get("fixed_width", 800)), int(c.get("fixed_height", 600)))
        region = ui.select_region(preview, mon, mode=mode, size=size)

        if region is None:
            return                          # user cancelled

        x1, y1, x2, y2 = region
        cropped = frame[y1:y2, x1:x2]

        sdr_img, notify_path = _process_and_save(
            cropped, mon, c["save_folder"], c["save_mode"], c["tonemapping"],
            sdr_white_nits=c.get("sdr_white_nits", 250),
        )

        clipboard_win.copy_image(sdr_img)
        label = _hdr_label(mon)
        _notify(
            f"{label} Region saved → {c['save_folder']}",
            image_path=notify_path,
        )

    except Exception as exc:
        _notify(f"Error: {exc}", "Error")
    finally:
        if was_visible:
            ui.show_toolbar_transient()
        _capture_lock.release()


# ── Hotkey management ─────────────────────────────────────────────────────────

def _start_hotkey_listener() -> None:
    global _hotkey_listener

    with _config_lock:
        enabled   = _config.get("hotkeys_enabled", True)
        hk_full   = _config["hotkey_fullscreen"]
        hk_region = _config["hotkey_region"]

    # Always tear down any existing listener first, then re-register only when
    # hotkeys are enabled (toolbar + tray work regardless).
    with _hotkey_listener_lock:
        if _hotkey_listener:
            _hotkey_listener.stop()
            _hotkey_listener = None
        if not enabled:
            return

        from pynput import keyboard

        hotkeys = {
            hk_full:   lambda: threading.Thread(target=_do_fullscreen, daemon=True).start(),
            hk_region: lambda: threading.Thread(target=_do_region,     daemon=True).start(),
        }
        try:
            listener = keyboard.GlobalHotKeys(hotkeys)
            listener.start()
            _hotkey_listener = listener
        except Exception as exc:
            _notify(f"Hotkey registration failed: {exc}", "Error")


def _restart_hotkeys_after_save(new_cfg: dict) -> None:
    global _config
    with _config_lock:
        _config = new_cfg
    capture.set_idle_release_secs(new_cfg.get("idle_release_secs", 120))
    _start_hotkey_listener()


def _apply_config(partial: dict) -> None:
    """Persist partial config changes coming from the toolbar (mode/size,
    toolbar visibility).  Source of truth is _config."""
    global _config
    with _config_lock:
        _config.update(partial)
        snapshot = dict(_config)
    try:
        cfg.save(snapshot)
    except Exception as exc:
        print(f"[main] config save failed: {exc}")


# ── Tray icon ─────────────────────────────────────────────────────────────────

def _load_tray_icon() -> Image.Image:
    base = sys._MEIPASS if getattr(sys, 'frozen', False) else os.path.dirname(os.path.abspath(__file__))
    icon_path = os.path.join(base, "app.ico")
    if os.path.exists(icon_path):
        return Image.open(icon_path)
    return Image.new("RGB", (64, 64), color=(30, 30, 60))


def _open_settings() -> None:
    with _config_lock:
        current = dict(_config)
    settings_window.open_settings(current, _restart_hotkeys_after_save)


def _on_settings(_icon, _item) -> None:
    _open_settings()


def _on_new_region(_icon, _item) -> None:
    threading.Thread(target=_do_region, daemon=True).start()


def _on_fullscreen(_icon, _item) -> None:
    threading.Thread(target=_do_fullscreen, daemon=True).start()


def _on_toggle_toolbar(_icon, _item) -> None:
    if ui.is_toolbar_visible():
        ui.hide_toolbar()
    else:
        ui.show_toolbar()


def _on_quit(icon, _item) -> None:
    with _hotkey_listener_lock:
        if _hotkey_listener:
            _hotkey_listener.stop()
    try:
        capture.release_all_cameras()
    except Exception:
        pass
    try:
        ui.stop()
    except Exception:
        pass
    icon.stop()


def _start_ui() -> None:
    """Bring up the shared UI root + floating toolbar with our handlers."""
    with _config_lock:
        snapshot = dict(_config)
    capture.set_idle_release_secs(snapshot.get("idle_release_secs", 120))
    ui.start(snapshot, {
        "new_region":   _do_region,
        "fullscreen":   _do_fullscreen,
        "open_settings": _open_settings,
        "apply_config": _apply_config,
    })


def _setup(icon: pystray.Icon) -> None:
    icon.visible = True
    _start_ui()
    _start_hotkey_listener()


def main() -> None:
    global _icon

    _ensure_single_instance()

    menu = pystray.Menu(
        pystray.MenuItem("New screenshot", _on_new_region),
        pystray.MenuItem("Full screen", _on_fullscreen),
        pystray.MenuItem("Show toolbar", _on_toggle_toolbar,
                         checked=lambda _item: ui.is_toolbar_visible()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Settings…", _on_settings),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", _on_quit),
    )

    _icon = pystray.Icon(
        name="HDRScreenshot",
        icon=_load_tray_icon(),
        title="HDR Screenshot",
        menu=menu,
    )

    _icon.run(setup=_setup)

    # _icon.run() returns once the tray loop ends (Quit → icon.stop()), so the
    # tray icon has already been removed cleanly by this point.  Force-terminate
    # now: a screenshot leaves a live Direct3D11 device + COM-initialised
    # threads (FP16Capture / dxcam Desktop Duplication) that can stall the normal
    # interpreter shutdown and leave the process lingering in Task Manager.
    os._exit(0)


if __name__ == "__main__":
    main()
