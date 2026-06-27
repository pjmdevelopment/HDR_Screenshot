"""
Shared UI layer — one persistent Tk root, the floating toolbar, the region
overlay, and the in-app toast.

Why this exists
───────────────
The original tool spun up a fresh Tk root for every region select / settings
window and launched powershell.exe for every notification, which made the app
feel slow ("windows slow to appear", "notifications feel heavy").  Here a single
hidden root is created once on a dedicated daemon thread; everything visible is a
Toplevel of that root, scheduled thread-safely via ``run_on_ui``.

Threading model
───────────────
  • The root lives on the *UI thread* and its ``mainloop`` runs there.
  • Capture work runs on short-lived worker threads.  They never touch Tk
    directly — they call ``run_on_ui`` / ``select_region`` / ``toast`` which
    marshal onto the UI thread.

Public API
──────────
    start(config, handlers)         — create root + toolbar, run mainloop
    run_on_ui(fn)                   — schedule fn on the UI thread
    select_region(preview, mon, …)  — blocking region pick (free or fixed/stamp)
    toast(title, body, image_path)  — fast in-process notification
    show_toolbar() / hide_toolbar() / is_toolbar_visible()
    open_viewer(image, …)           — open the annotation editor on the UI thread
    is_running()                    — True once the root is alive
    stop()                          — tear everything down
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import threading
import tkinter as tk
from typing import Callable

from PIL import Image, ImageTk

import overlay as overlay_mod
from capture import MonitorInfo

# ── Exclude-from-capture (Windows 10 2004+ / build 19041+) ────────────────────
# Marking a window with WDA_EXCLUDEFROMCAPTURE makes it invisible to DXGI/WGC
# screen capture while staying visible to the user — so the toolbar/overlay/toast
# never land in a screenshot and we can skip hiding them (no settle delay).
_WDA_EXCLUDEFROMCAPTURE = 0x00000011
_GA_ROOT = 2

try:
    _user32 = ctypes.windll.user32
    _user32.SetWindowDisplayAffinity.argtypes = [wt.HWND, wt.DWORD]
    _user32.SetWindowDisplayAffinity.restype = wt.BOOL
    _user32.GetAncestor.argtypes = [wt.HWND, wt.UINT]
    _user32.GetAncestor.restype = wt.HWND
    _user32.SystemParametersInfoW.argtypes = [
        ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
    _user32.SystemParametersInfoW.restype = wt.BOOL
except Exception:
    _user32 = None

_SPI_GETWORKAREA = 0x0030


def _primary_work_area() -> "tuple[int, int, int, int] | None":
    """Primary monitor's work area (screen minus taskbar) as (l, t, r, b),
    or None if unavailable.  Used so the toolbar never lands under the taskbar
    when it is docked at the top of the screen."""
    if _user32 is None:
        return None
    try:
        rect = wt.RECT()
        if _user32.SystemParametersInfoW(_SPI_GETWORKAREA, 0,
                                         ctypes.byref(rect), 0):
            return rect.left, rect.top, rect.right, rect.bottom
    except Exception:
        pass
    return None


def _exclude_from_capture(window: tk.Misc) -> bool:
    """Hide *window* from screen capture.  Returns True on success (the API is
    only available on Windows 10 2004+; older builds fall back to hiding)."""
    if _user32 is None:
        return False
    try:
        window.update_idletasks()
        hwnd = _user32.GetAncestor(window.winfo_id(), _GA_ROOT)
        return bool(_user32.SetWindowDisplayAffinity(hwnd, _WDA_EXCLUDEFROMCAPTURE))
    except Exception:
        return False

# ── Palette (kept dark to match the customtkinter settings window) ────────────
_BG      = "#1f1f2b"
_BG_HOV  = "#2c2c3d"
_FG      = "#e6e6e6"
_ACCENT  = "#3b6ea5"
_ACCENT_H = "#4f86c6"
_FONT    = ("Segoe UI", 10)

# ── Module state ──────────────────────────────────────────────────────────────
_root: tk.Tk | None = None
_toolbar: tk.Toplevel | None = None
_ready = threading.Event()
_toolbar_visible = False
_toolbar_excluded = False        # True once WDA_EXCLUDEFROMCAPTURE is applied

_config: dict = {}
_handlers: dict[str, Callable] = {}

# toolbar Tk variables (created on the UI thread in _build_toolbar)
_fixed_var: tk.BooleanVar | None = None
_w_var: tk.StringVar | None = None
_h_var: tk.StringVar | None = None


# ── Lifecycle ─────────────────────────────────────────────────────────────────

def start(config: dict, handlers: dict[str, Callable]) -> None:
    """
    Spin up the UI thread, create the hidden root + toolbar, and start the
    mainloop.  Returns once the root is ready (the mainloop keeps running on the
    UI thread).  *handlers* keys: new_region, fullscreen, open_settings,
    apply_config(partial_dict).
    """
    global _config, _handlers
    _config = config
    _handlers = handlers
    threading.Thread(target=_ui_thread, name="ui", daemon=True).start()
    _ready.wait(timeout=10)


def _ui_thread() -> None:
    global _root
    root = tk.Tk()
    root.withdraw()                # the root itself stays invisible forever
    _root = root
    _build_toolbar()
    if _config.get("show_toolbar", True):
        show_toolbar_transient()       # honour saved state without re-persisting
    _ready.set()
    root.mainloop()


def is_running() -> bool:
    return _root is not None and _ready.is_set()


def stop() -> None:
    if _root is not None:
        try:
            run_on_ui(_root.quit)
        except Exception:
            pass


def run_on_ui(fn: Callable) -> None:
    """Schedule *fn* to run on the UI thread (no-op if the root is gone)."""
    if _root is not None:
        try:
            _root.after(0, fn)
        except Exception:
            pass


# ── Floating toolbar ──────────────────────────────────────────────────────────

def _build_toolbar() -> None:
    global _toolbar, _fixed_var, _w_var, _h_var, _toolbar_excluded
    assert _root is not None

    bar = tk.Toplevel(_root)
    bar.withdraw()
    bar.overrideredirect(True)
    bar.attributes("-topmost", True)
    bar.configure(bg=_BG)

    frame = tk.Frame(bar, bg=_BG, padx=6, pady=4)
    frame.pack()

    # Drag handle (grip) — lets the user move the frameless bar
    grip = tk.Label(frame, text="⠿", bg=_BG, fg="#6a6a80", font=("Segoe UI", 12),
                    cursor="fleur")
    grip.pack(side="left", padx=(0, 4))
    _make_draggable(bar, grip)
    _make_draggable(bar, frame)

    _tool_button(frame, "✛ New",  _on_new)
    _tool_button(frame, "⛶ Full", _on_full)

    # Fixed-size (stamp) controls
    _fixed_var = tk.BooleanVar(value=_config.get("capture_mode") == "fixed")
    _w_var = tk.StringVar(value=str(_config.get("fixed_width", 800)))
    _h_var = tk.StringVar(value=str(_config.get("fixed_height", 600)))

    chk = tk.Checkbutton(
        frame, text="Fixed", variable=_fixed_var, command=_on_fixed_changed,
        bg=_BG, fg=_FG, selectcolor=_BG, activebackground=_BG,
        activeforeground=_FG, font=_FONT, bd=0, highlightthickness=0,
    )
    chk.pack(side="left", padx=(8, 2))

    vcmd = (bar.register(_validate_int), "%P")
    w_entry = tk.Entry(frame, textvariable=_w_var, width=5, font=_FONT,
                       bg=_BG_HOV, fg=_FG, insertbackground=_FG,
                       relief="flat", justify="center",
                       validate="key", validatecommand=vcmd)
    w_entry.pack(side="left")
    tk.Label(frame, text="×", bg=_BG, fg=_FG, font=_FONT).pack(side="left", padx=1)
    h_entry = tk.Entry(frame, textvariable=_h_var, width=5, font=_FONT,
                       bg=_BG_HOV, fg=_FG, insertbackground=_FG,
                       relief="flat", justify="center",
                       validate="key", validatecommand=vcmd)
    h_entry.pack(side="left", padx=(0, 4))
    for e in (w_entry, h_entry):
        e.bind("<FocusOut>", lambda _ev: _on_fixed_changed())
        e.bind("<Return>",   lambda _ev: _on_fixed_changed())

    _tool_button(frame, "⚙", _on_settings)
    _tool_button(frame, "✕", hide_toolbar, accent=False)

    # Initial position: top-centre of the primary monitor's *work area*, so a
    # taskbar docked at the top of the screen never overlaps the bar.
    bar.update_idletasks()
    bw = bar.winfo_reqwidth()
    wa = _primary_work_area()
    if wa is not None:
        left, top, right, _bottom = wa
        x = max(left, left + (right - left - bw) // 2)
        y = top + 8
    else:
        x = max(0, (bar.winfo_screenwidth() - bw) // 2)
        y = 12
    bar.geometry(f"+{x}+{y}")

    # Exclude the bar from screen capture so we never have to hide it before a
    # shot (the main perf win for "New → selection mode").
    _toolbar_excluded = _exclude_from_capture(bar)
    _toolbar = bar


def toolbar_excluded_from_capture() -> bool:
    """True when the toolbar is hidden from capture and so need not be withdrawn
    before grabbing (Windows 10 2004+)."""
    return _toolbar_excluded


def _tool_button(parent: tk.Misc, text: str, command: Callable,
                 accent: bool = True) -> tk.Button:
    bg = _ACCENT if accent else _BG_HOV
    hov = _ACCENT_H if accent else _BG
    btn = tk.Button(
        parent, text=text, command=command, font=_FONT,
        bg=bg, fg=_FG, activebackground=hov, activeforeground=_FG,
        relief="flat", bd=0, padx=8, pady=3, cursor="hand2",
    )
    btn.pack(side="left", padx=2)
    btn.bind("<Enter>", lambda _e: btn.configure(bg=hov))
    btn.bind("<Leave>", lambda _e: btn.configure(bg=bg))
    return btn


def _make_draggable(window: tk.Toplevel, widget: tk.Misc) -> None:
    state = {"x": 0, "y": 0}

    def _press(e: tk.Event) -> None:
        state["x"], state["y"] = e.x, e.y

    def _drag(e: tk.Event) -> None:
        x = window.winfo_x() + (e.x - state["x"])
        y = window.winfo_y() + (e.y - state["y"])
        window.geometry(f"+{x}+{y}")

    widget.bind("<ButtonPress-1>", _press)
    widget.bind("<B1-Motion>",     _drag)


def _validate_int(proposed: str) -> bool:
    return proposed == "" or (proposed.isdigit() and len(proposed) <= 5)


def _current_fixed_size() -> "tuple[int, int]":
    def _v(var: tk.StringVar | None, default: int) -> int:
        try:
            return max(4, int(var.get())) if var else default
        except (TypeError, ValueError):
            return default
    return _v(_w_var, 800), _v(_h_var, 600)


def _on_fixed_changed() -> None:
    """Persist the toolbar's mode/size controls back into config."""
    w, h = _current_fixed_size()
    mode = "fixed" if (_fixed_var and _fixed_var.get()) else "free"
    _config["capture_mode"] = mode
    _config["fixed_width"] = w
    _config["fixed_height"] = h
    _call("apply_config", {"capture_mode": mode,
                           "fixed_width": w, "fixed_height": h})


# ── Toolbar button handlers (run on the UI thread) ────────────────────────────

def _call(name: str, *args) -> None:
    fn = _handlers.get(name)
    if fn:
        try:
            fn(*args)
        except Exception as exc:        # never let a handler kill the UI thread
            print(f"[ui] handler {name!r} error: {exc}")


def _spawn(name: str) -> None:
    fn = _handlers.get(name)
    if fn:
        threading.Thread(target=fn, daemon=True).start()


def _on_new(*_a) -> None:
    _spawn("new_region")


def _on_full(*_a) -> None:
    _spawn("fullscreen")


def _on_settings(*_a) -> None:
    _call("open_settings")


# ── Show / hide ───────────────────────────────────────────────────────────────

def _deiconify_toolbar() -> None:
    if _toolbar is not None:
        _toolbar.deiconify()
        _toolbar.lift()
        _toolbar.attributes("-topmost", True)


def _withdraw_toolbar() -> None:
    if _toolbar is not None:
        _toolbar.withdraw()


# NOTE: ``_toolbar_visible`` is updated *synchronously* here (in the caller's
# thread) rather than inside the scheduled UI callback.  The tray menu reads it
# the instant the menu is opened, so the flag must reflect the intended state
# immediately — otherwise the "Show toolbar" checkmark can lag the real state.

def show_toolbar() -> None:
    global _toolbar_visible
    _toolbar_visible = True
    _call("apply_config", {"show_toolbar": True})
    _call("refresh_menu")
    run_on_ui(_deiconify_toolbar)


def hide_toolbar() -> None:
    global _toolbar_visible
    _toolbar_visible = False
    _call("apply_config", {"show_toolbar": False})
    _call("refresh_menu")
    run_on_ui(_withdraw_toolbar)


def hide_toolbar_transient() -> None:
    """Hide the bar for the duration of a capture without persisting the state."""
    global _toolbar_visible
    _toolbar_visible = False
    run_on_ui(_withdraw_toolbar)


def show_toolbar_transient() -> None:
    """Re-show the bar after a capture without persisting the state."""
    global _toolbar_visible
    _toolbar_visible = True
    run_on_ui(_deiconify_toolbar)


def is_toolbar_visible() -> bool:
    return _toolbar_visible


# ── Region overlay (blocking, marshalled onto the UI thread) ──────────────────

def select_region(
    preview: Image.Image,
    monitor: MonitorInfo,
    mode: str = "free",
    size: "tuple[int, int] | None" = None,
) -> "tuple[int, int, int, int] | None":
    """
    Show the region overlay on the shared root and block the *calling* thread
    until the user finishes.  Returns monitor-local (x1,y1,x2,y2) or None.
    Falls back to a standalone overlay if the shared root is not running.
    """
    if not is_running():
        return overlay_mod.select_region(preview, monitor, mode=mode, size=size)

    done = threading.Event()
    result: list = [None]

    def _build() -> None:
        def _on_done(res) -> None:
            result[0] = res
            done.set()
        top = overlay_mod.create_overlay(
            _root, preview, monitor, mode=mode, size=size, on_done=_on_done,
        )
        _exclude_from_capture(top)     # keep the overlay out of any capture

    run_on_ui(_build)
    done.wait()
    return result[0]


# ── Screenshot editor ─────────────────────────────────────────────────────────

def exclude_from_capture(window: tk.Misc) -> bool:
    """Public wrapper: hide *window* from screen capture (Windows 10 2004+)."""
    return _exclude_from_capture(window)


def open_viewer(image, save_folder: str, suggested_name: str = "screenshot",
                jpg_quality: int = 92) -> None:
    """Open the annotation editor for *image* (a PIL Image).  Safe to call from
    any thread — the window is built on the UI thread.  No-op if the root is not
    running yet."""
    if not is_running():
        return
    import viewer

    def _build() -> None:
        viewer.create_viewer(
            _root, image, save_folder=save_folder,
            suggested_name=suggested_name, jpg_quality=jpg_quality,
            exclude_fn=_exclude_from_capture,
        )
    run_on_ui(_build)


# ── In-app toast ──────────────────────────────────────────────────────────────

_TOAST_MS = 3400


def _load_thumb(image_path: str | None) -> "Image.Image | None":
    """Decode a small thumbnail off the UI thread; None if unavailable."""
    if not image_path or not os.path.isfile(image_path):
        return None
    try:
        img = Image.open(image_path)
        img.thumbnail((96, 96), Image.LANCZOS)
        return img.convert("RGB")
    except Exception:
        return None


def toast(title: str, body: str, image_path: str | None = None) -> None:
    """Show a fast in-process notification.  Safe to call from any thread."""
    if not is_running():
        return
    thumb = _load_thumb(image_path)             # decode off the UI thread
    run_on_ui(lambda: _show_toast(title, body, image_path, thumb))


def _show_toast(title: str, body: str, image_path: str | None,
                thumb: "Image.Image | None") -> None:
    assert _root is not None
    win = tk.Toplevel(_root)
    win.overrideredirect(True)
    win.attributes("-topmost", True)
    _exclude_from_capture(win)          # toast must not appear in a capture
    try:
        win.attributes("-alpha", 0.97)
    except Exception:
        pass
    win.configure(bg=_ACCENT)

    outer = tk.Frame(win, bg=_BG, padx=12, pady=10)
    outer.pack(padx=1, pady=1)                    # 1px accent border

    if thumb is not None:
        photo = ImageTk.PhotoImage(thumb)
        lbl = tk.Label(outer, image=photo, bg=_BG)
        lbl.image = photo                          # keep ref
        lbl.pack(side="left", padx=(0, 10))

    text = tk.Frame(outer, bg=_BG)
    text.pack(side="left", fill="y")
    tk.Label(text, text=title, bg=_BG, fg=_FG,
             font=("Segoe UI Semibold", 10), anchor="w",
             justify="left").pack(anchor="w")
    tk.Label(text, text=body, bg=_BG, fg="#b9b9c9",
             font=("Segoe UI", 9), anchor="w", justify="left",
             wraplength=320).pack(anchor="w")

    # Position bottom-right of the screen the cursor is on
    win.update_idletasks()
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    w, h = win.winfo_reqwidth(), win.winfo_reqheight()
    win.geometry(f"+{sw - w - 24}+{sh - h - 56}")

    def _open(_e=None) -> None:
        target = image_path if (image_path and os.path.isfile(image_path)) else None
        try:
            if target:
                os.startfile(target)               # type: ignore[attr-defined]
        except Exception:
            pass
        _close()

    def _close() -> None:
        try:
            win.destroy()
        except Exception:
            pass

    # Make the whole toast (including thumbnail + text labels) clickable.
    def _bind_all(widget: tk.Misc) -> None:
        widget.bind("<Button-1>", _open)
        for child in widget.winfo_children():
            _bind_all(child)
    _bind_all(win)

    win.after(_TOAST_MS, _close)
