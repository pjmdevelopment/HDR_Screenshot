"""
Fullscreen region-selection overlay.

Displays a tone-mapped preview of the captured HDR frame on the correct
monitor so the user can pick a crop region.  Two modes:

  • "free"  — drag a rectangle from corner to corner (classic behaviour).
  • "fixed" — a fixed W×H box (stamp) follows the cursor; one click drops it.

Returns coordinates in the monitor's local space (origin = monitor top-left
corner), which matches the numpy array returned by capture.grab().

The overlay can be hosted two ways:
  • create_overlay(root, …) — builds a tk.Toplevel on an existing (shared) root
    and reports the result via a callback.  Preferred — no new root spin-up.
  • select_region(…)        — standalone fallback that owns a throw-away tk.Tk
    and blocks until done.  Used only when no shared root is available.
"""
import tkinter as tk
from PIL import Image, ImageTk

from capture import MonitorInfo


def _install(
    container: tk.Misc,
    preview: Image.Image,
    monitor: MonitorInfo,
    mode: str,
    size: "tuple[int, int] | None",
    finish,
) -> None:
    """
    Build the selection canvas onto *container* (a Tk root or Toplevel) and
    wire the event handlers.  Calls finish(result) exactly once, where result
    is (x1, y1, x2, y2) in monitor-local coords or None if cancelled.
    """
    container.overrideredirect(True)
    container.attributes("-topmost", True)
    # Position exactly over the target monitor (handles negative offsets too)
    container.geometry(
        f"{monitor.width}x{monitor.height}+{monitor.left}+{monitor.top}"
    )

    # Scale preview to the monitor only when needed — the common case is that
    # the preview already matches the monitor resolution, so skip the (costly,
    # on 4K) LANCZOS resize entirely.
    if preview.size != (monitor.width, monitor.height):
        preview = preview.resize((monitor.width, monitor.height), Image.LANCZOS)
    photo = ImageTk.PhotoImage(preview)

    canvas = tk.Canvas(container, cursor="crosshair", highlightthickness=0, bd=0)
    canvas.pack(fill="both", expand=True)
    canvas.create_image(0, 0, anchor="nw", image=photo)
    canvas.image = photo                       # keep a ref so it is not GC'd

    # Light dim overlay so the content stays readable during selection
    canvas.create_rectangle(
        0, 0, monitor.width, monitor.height,
        fill="black", stipple="gray50", outline="",
    )

    done = [False]                             # guard: finish() runs once

    def _finish(result) -> None:
        if done[0]:
            return
        done[0] = True
        finish(result)

    container.bind("<Escape>", lambda e: _finish(None))
    try:
        container.focus_force()
    except Exception:
        pass

    if mode == "fixed" and size:
        _wire_fixed(canvas, monitor, size, _finish)
    else:
        _wire_free(canvas, monitor, _finish)


# ── Free-drag mode ────────────────────────────────────────────────────────────

def _wire_free(canvas: tk.Canvas, monitor: MonitorInfo, finish) -> None:
    canvas.create_text(
        monitor.width // 2, 30,
        text="Drag to select region  •  Esc to cancel",
        fill="white", font=("Segoe UI", 14),
    )

    state = {"x": 0, "y": 0, "rect": None, "label": None}

    def _clear() -> None:
        for key in ("rect", "label"):
            if state[key]:
                canvas.delete(state[key])
                state[key] = None

    def _on_press(e: tk.Event) -> None:
        state["x"], state["y"] = e.x, e.y
        _clear()

    def _on_drag(e: tk.Event) -> None:
        _clear()
        x1, y1 = min(state["x"], e.x), min(state["y"], e.y)
        x2, y2 = max(state["x"], e.x), max(state["y"], e.y)
        state["rect"] = canvas.create_rectangle(
            x1, y1, x2, y2, outline="#ff3333", width=2, dash=(6, 3),
        )
        state["label"] = canvas.create_text(
            x1 + 4, y1 - 12 if y1 > 20 else y2 + 12,
            text=f"{x2 - x1} × {y2 - y1}",
            fill="#ff3333", font=("Segoe UI", 11), anchor="w",
        )

    def _on_release(e: tk.Event) -> None:
        x1, y1 = min(state["x"], e.x), min(state["y"], e.y)
        x2, y2 = max(state["x"], e.x), max(state["y"], e.y)
        finish((x1, y1, x2, y2) if (x2 - x1 >= 4 and y2 - y1 >= 4) else None)

    canvas.bind("<ButtonPress-1>",   _on_press)
    canvas.bind("<B1-Motion>",       _on_drag)
    canvas.bind("<ButtonRelease-1>", _on_release)


# ── Fixed-size (stamp) mode ───────────────────────────────────────────────────

def _wire_fixed(
    canvas: tk.Canvas, monitor: MonitorInfo,
    size: "tuple[int, int]", finish,
) -> None:
    w = max(4, min(int(size[0]), monitor.width))
    h = max(4, min(int(size[1]), monitor.height))

    canvas.create_text(
        monitor.width // 2, 30,
        text=f"Move the {w} × {h} box  •  click to capture  •  Esc to cancel",
        fill="white", font=("Segoe UI", 14),
    )

    state = {"rect": None, "label": None}

    def _box_for(cx: int, cy: int) -> "tuple[int, int, int, int]":
        # Centre the box on the cursor, clamped fully inside the monitor.
        x1 = min(max(cx - w // 2, 0), monitor.width - w)
        y1 = min(max(cy - h // 2, 0), monitor.height - h)
        return x1, y1, x1 + w, y1 + h

    def _redraw(cx: int, cy: int) -> None:
        for key in ("rect", "label"):
            if state[key]:
                canvas.delete(state[key])
        x1, y1, x2, y2 = _box_for(cx, cy)
        state["rect"] = canvas.create_rectangle(
            x1, y1, x2, y2, outline="#33ff66", width=2,
        )
        state["label"] = canvas.create_text(
            x1 + 4, y1 - 12 if y1 > 20 else y2 + 12,
            text=f"{w} × {h}",
            fill="#33ff66", font=("Segoe UI", 11), anchor="w",
        )

    def _on_motion(e: tk.Event) -> None:
        _redraw(e.x, e.y)

    def _on_click(e: tk.Event) -> None:
        finish(_box_for(e.x, e.y))

    canvas.bind("<Motion>",        _on_motion)
    canvas.bind("<ButtonPress-1>", _on_click)
    _redraw(monitor.width // 2, monitor.height // 2)   # initial box at centre


# ── Public entry points ───────────────────────────────────────────────────────

def create_overlay(
    root: tk.Misc,
    preview: Image.Image,
    monitor: MonitorInfo,
    mode: str = "free",
    size: "tuple[int, int] | None" = None,
    on_done=lambda result: None,
) -> tk.Toplevel:
    """
    Build the overlay as a Toplevel of *root* and report the result via
    on_done(result).  The Toplevel destroys itself once a result is produced.
    Must be called on the thread that owns *root* (the UI thread).
    """
    top = tk.Toplevel(root)

    def _finish(result) -> None:
        try:
            top.destroy()
        finally:
            on_done(result)

    _install(top, preview, monitor, mode, size, _finish)
    return top


def select_region(
    preview: Image.Image,
    monitor: MonitorInfo,
    mode: str = "free",
    size: "tuple[int, int] | None" = None,
) -> "tuple[int, int, int, int] | None":
    """
    Standalone fallback: own a throw-away tk.Tk, block until the user finishes,
    and return the region (or None).  Prefer create_overlay() with a shared root.
    """
    result: list = [None]
    root = tk.Tk()

    def _finish(res) -> None:
        result[0] = res
        root.destroy()

    _install(root, preview, monitor, mode, size, _finish)
    root.mainloop()
    return result[0]
