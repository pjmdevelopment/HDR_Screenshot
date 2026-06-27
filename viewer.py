"""
Screenshot editor / preview window.

Shown after a capture when "After capture → Open editor" is selected.  Lets the
user annotate the tone-mapped SDR image (rectangle, ellipse, arrow, line, pen,
highlighter, text, blur/redact, crop) and then **Save** (PNG/JPG), **Copy** to
the clipboard, or **Pin** the result as a floating always-on-top window.

Design
──────
  • Built as a tk.Toplevel of the shared UI root (passed in), so it lives on the
    UI thread alongside the toolbar/overlay/toast.  Call ``create_viewer`` from
    the UI thread (ui.open_viewer marshals this for you).

  • Annotations are stored as plain dicts in *full-image* coordinates (the
    authoritative model).  Two renderers draw them with Pillow:
        – a fast one onto a pre-scaled display copy (shown on the canvas),
        – a full-resolution one used only for Save / Copy / Pin.
    During an active drag the in-progress shape is drawn as a lightweight canvas
    item; on release it is committed to the model and the display is re-rendered
    once.  This keeps interaction snappy on 4K while output stays full-res.

  • No move/select of committed shapes — use Undo.  (Kept intentionally simple.)
"""
from __future__ import annotations

import math
import os
import tkinter as tk
from tkinter import filedialog
from typing import Callable

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageTk

import clipboard_win

# ── Palette (matches ui.py) ───────────────────────────────────────────────────
_BG       = "#1f1f2b"
_BG_HOV   = "#2c2c3d"
_FG       = "#e6e6e6"
_ACCENT   = "#3b6ea5"
_ACCENT_H = "#4f86c6"
_FONT     = ("Segoe UI", 10)

# Drawing colour swatches
_SWATCHES = [
    ("#ff3b30", (255, 59, 48)),    # red
    ("#ffcc00", (255, 204, 0)),    # yellow
    ("#34c759", (52, 199, 89)),    # green
    ("#0a84ff", (10, 132, 255)),   # blue
    ("#ffffff", (255, 255, 255)),  # white
    ("#1a1a1a", (26, 26, 26)),     # black
]

# Stroke-width presets (in full-image pixels): Small / Medium / Large
_WIDTHS = {"S": 3, "M": 6, "L": 10}

# Tools and the toolbar glyphs that select them
_TOOLS = [
    ("rect",      "▭",  "Rectangle"),
    ("ellipse",   "◯",  "Ellipse"),
    ("arrow",     "↗",  "Arrow"),
    ("line",      "／",  "Line"),
    ("pen",       "✎",  "Pen"),
    ("highlight", "▤",  "Highlighter"),
    ("text",      "T",  "Text"),
    ("blur",      "░",  "Blur / redact"),
    ("crop",      "⛶",  "Crop"),
]

_MAX_UNDO = 30

# Keep references to floating pinned windows so they are not garbage-collected.
_pins: list[tk.Toplevel] = []


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for name in ("segoeui.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, max(8, size))
        except Exception:
            continue
    return ImageFont.load_default()


def _render(base_rgb: Image.Image, shapes: list[dict], scale: float) -> Image.Image:
    """Draw *shapes* (full-image coords) onto a copy of *base_rgb*, scaling each
    coordinate/width by *scale*.  Returns an RGB image the size of *base_rgb*."""
    img = base_rgb.convert("RGBA")

    for s in shapes:
        t   = s["type"]
        col = tuple(s["color"])
        w   = max(1, int(round(s["width"] * scale)))
        pts = [(x * scale, y * scale) for (x, y) in s["points"]]
        draw = ImageDraw.Draw(img)

        if t in ("rect", "ellipse"):
            (x1, y1), (x2, y2) = pts[0], pts[1]
            box = [min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)]
            fill = col + (255,) if s.get("fill") else None
            if t == "rect":
                draw.rectangle(box, outline=col + (255,), width=w, fill=fill)
            else:
                draw.ellipse(box, outline=col + (255,), width=w, fill=fill)

        elif t in ("line", "pen"):
            if len(pts) >= 2:
                draw.line(pts, fill=col + (255,), width=w, joint="curve")

        elif t == "arrow":
            (x1, y1), (x2, y2) = pts[0], pts[1]
            draw.line([(x1, y1), (x2, y2)], fill=col + (255,), width=w)
            ang  = math.atan2(y2 - y1, x2 - x1)
            head = max(10.0, w * 4.0)
            for da in (math.radians(150), math.radians(-150)):
                hx = x2 + head * math.cos(ang + da)
                hy = y2 + head * math.sin(ang + da)
                draw.line([(x2, y2), (hx, hy)], fill=col + (255,), width=w)

        elif t == "highlight":
            if len(pts) >= 2:
                layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
                ImageDraw.Draw(layer).line(
                    pts, fill=col + (110,),
                    width=max(w, int(round(16 * scale))), joint="curve",
                )
                img = Image.alpha_composite(img, layer)

        elif t == "text":
            draw.text(pts[0], s.get("text", ""), fill=col + (255,),
                      font=_font(int(round(s.get("fontsize", 24) * scale))))

        elif t == "blur":
            (x1, y1), (x2, y2) = pts[0], pts[1]
            box = (int(min(x1, x2)), int(min(y1, y2)),
                   int(max(x1, x2)), int(max(y1, y2)))
            if box[2] - box[0] > 1 and box[3] - box[1] > 1:
                region = img.crop(box).filter(
                    ImageFilter.GaussianBlur(radius=max(4, int(round(12 * scale)))))
                img.paste(region, box)

    return img.convert("RGB")


class _Viewer:
    def __init__(self, root: tk.Misc, image: Image.Image, *,
                 save_folder: str, suggested_name: str, jpg_quality: int,
                 exclude_fn: Callable[[tk.Misc], bool] | None) -> None:
        self._base_full   = image.convert("RGB")
        self._save_folder = save_folder
        self._suggested   = suggested_name
        self._jpg_quality = int(jpg_quality)
        self._exclude_fn  = exclude_fn

        self._shapes: list[dict] = []
        self._undo: list[tuple[Image.Image, list[dict]]] = []
        self._redo: list[tuple[Image.Image, list[dict]]] = []

        self._tool  = "rect"
        self._color = _SWATCHES[0][1]
        self._width = _WIDTHS["M"]
        self._fill  = False

        self._drag_start: tuple[int, int] | None = None
        self._pen_pts: list[tuple[int, int]] = []
        self._preview_ids: list[int] = []
        self._text_entry: tk.Entry | None = None

        # ── Display scale ──────────────────────────────────────────────────
        self._win = tk.Toplevel(root)
        self._win.title("HDR Screenshot — Editor")
        self._win.configure(bg=_BG)
        sw = self._win.winfo_screenwidth()
        sh = self._win.winfo_screenheight()
        self._max_w = int(sw * 0.92)
        self._max_h = int(sh * 0.84)
        self._recompute_scale()

        if exclude_fn:
            try:
                exclude_fn(self._win)
            except Exception:
                pass

        self._build_ui()
        self._render_display()
        self._center()
        self._win.protocol("WM_DELETE_WINDOW", self._win.destroy)
        self._win.bind("<Control-z>", lambda _e: self._do_undo())
        self._win.bind("<Control-y>", lambda _e: self._do_redo())
        self._win.bind("<Escape>",    lambda _e: self._win.destroy())

    # ── Geometry ───────────────────────────────────────────────────────────

    def _recompute_scale(self) -> None:
        w, h = self._base_full.size
        self._scale = min(1.0, self._max_w / w, self._max_h / h)
        self._disp_size = (max(1, int(w * self._scale)),
                           max(1, int(h * self._scale)))
        self._base_disp = self._base_full.resize(self._disp_size, Image.LANCZOS)

    def _center(self) -> None:
        self._win.update_idletasks()
        w = self._win.winfo_reqwidth()
        h = self._win.winfo_reqheight()
        sw = self._win.winfo_screenwidth()
        sh = self._win.winfo_screenheight()
        x = max(0, (sw - w) // 2)
        y = max(0, (sh - h) // 2)
        self._win.geometry(f"+{x}+{y}")

    # ── UI construction ─────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # Top toolbar: tools, colours, widths, fill, undo/redo
        top = tk.Frame(self._win, bg=_BG, padx=6, pady=5)
        top.pack(fill="x")

        self._tool_buttons: dict[str, tk.Button] = {}
        for tool, glyph, tip in _TOOLS:
            b = tk.Button(top, text=glyph, font=("Segoe UI", 11), width=2,
                          bg=_BG_HOV, fg=_FG, activebackground=_ACCENT_H,
                          activeforeground=_FG, relief="flat", bd=0,
                          cursor="hand2",
                          command=lambda t=tool: self._set_tool(t))
            b.pack(side="left", padx=1)
            self._tool_buttons[tool] = b

        tk.Frame(top, bg="#3a3a4d", width=1, height=22).pack(side="left", padx=8)

        for hexcol, rgb in _SWATCHES:
            sw = tk.Button(top, bg=hexcol, activebackground=hexcol, width=2,
                           relief="flat", bd=1, cursor="hand2",
                           command=lambda c=rgb: self._set_color(c))
            sw.pack(side="left", padx=1)

        tk.Frame(top, bg="#3a3a4d", width=1, height=22).pack(side="left", padx=8)

        self._width_buttons: dict[str, tk.Button] = {}
        for name, _val in _WIDTHS.items():
            b = tk.Button(top, text=name, font=_FONT, width=2,
                          bg=_BG_HOV, fg=_FG, activebackground=_ACCENT_H,
                          activeforeground=_FG, relief="flat", bd=0,
                          cursor="hand2",
                          command=lambda n=name: self._set_width(n))
            b.pack(side="left", padx=1)
            self._width_buttons[name] = b

        self._fill_var = tk.BooleanVar(value=False)
        tk.Checkbutton(top, text="Fill", variable=self._fill_var,
                       command=self._on_fill, bg=_BG, fg=_FG, selectcolor=_BG,
                       activebackground=_BG, activeforeground=_FG, font=_FONT,
                       bd=0, highlightthickness=0).pack(side="left", padx=(8, 2))

        tk.Frame(top, bg="#3a3a4d", width=1, height=22).pack(side="left", padx=8)
        self._btn(top, "↶ Undo", self._do_undo, accent=False)
        self._btn(top, "↷ Redo", self._do_redo, accent=False)

        # Canvas
        self._canvas = tk.Canvas(self._win, width=self._disp_size[0],
                                 height=self._disp_size[1], bg="#101018",
                                 highlightthickness=0, bd=0, cursor="crosshair")
        self._canvas.pack()
        self._img_id = self._canvas.create_image(0, 0, anchor="nw")
        self._canvas.bind("<ButtonPress-1>",   self._on_press)
        self._canvas.bind("<B1-Motion>",       self._on_drag)
        self._canvas.bind("<ButtonRelease-1>", self._on_release)

        # Bottom bar: status + actions
        bot = tk.Frame(self._win, bg=_BG, padx=6, pady=6)
        bot.pack(fill="x")

        self._status = tk.Label(bot, text="", bg=_BG, fg="#9a9aae", font=_FONT)
        self._status.pack(side="left", padx=4)

        self._btn(bot, "Close", self._win.destroy, accent=False).pack_configure(side="right")
        self._btn(bot, "💾 Save", self._do_save).pack_configure(side="right")
        self._btn(bot, "⧉ Copy", self._do_copy).pack_configure(side="right")
        self._btn(bot, "📌 Pin", self._do_pin, accent=False).pack_configure(side="right")

        self._aot_var = tk.BooleanVar(value=False)
        tk.Checkbutton(bot, text="Always on top", variable=self._aot_var,
                       command=self._on_aot, bg=_BG, fg=_FG, selectcolor=_BG,
                       activebackground=_BG, activeforeground=_FG, font=_FONT,
                       bd=0, highlightthickness=0).pack(side="right", padx=8)

        self._set_tool("rect")
        self._set_width("M")
        self._set_color(self._color)

    def _btn(self, parent: tk.Misc, text: str, cmd: Callable,
             accent: bool = True) -> tk.Button:
        bg = _ACCENT if accent else _BG_HOV
        hov = _ACCENT_H if accent else _BG
        b = tk.Button(parent, text=text, command=cmd, font=_FONT, bg=bg, fg=_FG,
                      activebackground=hov, activeforeground=_FG, relief="flat",
                      bd=0, padx=10, pady=4, cursor="hand2")
        b.pack(side="left", padx=3)
        b.bind("<Enter>", lambda _e: b.configure(bg=hov))
        b.bind("<Leave>", lambda _e: b.configure(bg=bg))
        return b

    # ── Tool / style selection ───────────────────────────────────────────────

    def _set_tool(self, tool: str) -> None:
        self._commit_text_entry()
        self._tool = tool
        for t, b in self._tool_buttons.items():
            b.configure(bg=_ACCENT if t == tool else _BG_HOV)
        self._canvas.configure(cursor="xterm" if tool == "text" else "crosshair")

    def _set_color(self, rgb: tuple) -> None:
        self._color = rgb

    def _set_width(self, name: str) -> None:
        self._width = _WIDTHS[name]
        for n, b in self._width_buttons.items():
            b.configure(bg=_ACCENT if n == name else _BG_HOV)

    def _on_fill(self) -> None:
        self._fill = bool(self._fill_var.get())

    def _on_aot(self) -> None:
        try:
            self._win.attributes("-topmost", bool(self._aot_var.get()))
        except Exception:
            pass

    # ── Coordinate helpers ───────────────────────────────────────────────────

    def _to_img(self, cx: float, cy: float) -> tuple[int, int]:
        w, h = self._base_full.size
        x = min(max(int(round(cx / self._scale)), 0), w)
        y = min(max(int(round(cy / self._scale)), 0), h)
        return x, y

    def _disp_w(self) -> int:
        return max(1, int(round(self._width * self._scale)))

    def _hexcolor(self) -> str:
        return "#%02x%02x%02x" % self._color

    def _clear_preview(self) -> None:
        for cid in self._preview_ids:
            self._canvas.delete(cid)
        self._preview_ids.clear()

    # ── Canvas interaction ───────────────────────────────────────────────────

    def _on_press(self, e: tk.Event) -> None:
        if self._tool == "text":
            self._place_text_entry(e.x, e.y)
            return
        self._drag_start = (e.x, e.y)
        self._pen_pts = [(e.x, e.y)]

    def _on_drag(self, e: tk.Event) -> None:
        if self._tool == "text" or self._drag_start is None:
            return
        self._clear_preview()
        x0, y0 = self._drag_start
        col = self._hexcolor()
        w = self._disp_w()

        if self._tool in ("rect", "ellipse"):
            fn = self._canvas.create_rectangle if self._tool == "rect" else self._canvas.create_oval
            fill = col if self._fill else ""
            self._preview_ids.append(
                fn(x0, y0, e.x, e.y, outline=col, width=w, fill=fill))
        elif self._tool in ("line", "arrow"):
            self._preview_ids.append(
                self._canvas.create_line(x0, y0, e.x, e.y, fill=col, width=w,
                                         arrow="last" if self._tool == "arrow" else None))
        elif self._tool in ("blur", "crop"):
            self._preview_ids.append(
                self._canvas.create_rectangle(x0, y0, e.x, e.y, outline="#cccccc",
                                              width=1, dash=(4, 2)))
        elif self._tool == "highlight":
            self._pen_pts.append((e.x, e.y))
            self._preview_ids.append(
                self._canvas.create_line(*sum(self._pen_pts, ()), fill=col,
                                         width=max(w, int(16 * self._scale))))
        elif self._tool == "pen":
            self._pen_pts.append((e.x, e.y))
            self._preview_ids.append(
                self._canvas.create_line(*sum(self._pen_pts, ()), fill=col,
                                         width=w, smooth=True))

    def _on_release(self, e: tk.Event) -> None:
        if self._tool == "text" or self._drag_start is None:
            return
        self._clear_preview()
        x0, y0 = self._drag_start
        self._drag_start = None
        p1 = self._to_img(x0, y0)
        p2 = self._to_img(e.x, e.y)

        if self._tool == "crop":
            self._apply_crop(p1, p2)
            self._pen_pts = []
            return

        shape: dict | None = None
        if self._tool in ("rect", "ellipse", "arrow", "line", "blur"):
            if abs(p2[0] - p1[0]) < 3 and abs(p2[1] - p1[1]) < 3:
                return                                  # ignore stray clicks
            shape = {"type": self._tool, "color": self._color,
                     "width": self._width, "fill": self._fill,
                     "points": [p1, p2]}
        elif self._tool in ("pen", "highlight"):
            pts = [self._to_img(cx, cy) for (cx, cy) in self._pen_pts]
            if len(pts) >= 2:
                shape = {"type": self._tool, "color": self._color,
                         "width": self._width, "points": pts}
        self._pen_pts = []

        if shape is not None:
            self._commit(shape)

    # ── Text entry ───────────────────────────────────────────────────────────

    def _place_text_entry(self, cx: int, cy: int) -> None:
        self._commit_text_entry()
        entry = tk.Entry(self._canvas, bg="#ffffff", fg=self._hexcolor(),
                         insertbackground=self._hexcolor(),
                         font=("Segoe UI", max(9, int(self._width * 2.2))))
        self._text_entry = entry
        self._text_pos = self._to_img(cx, cy)
        win_id = self._canvas.create_window(cx, cy, anchor="nw", window=entry)
        self._preview_ids.append(win_id)
        entry.focus_set()
        entry.bind("<Return>",  lambda _e: self._commit_text_entry())
        entry.bind("<Escape>",  lambda _e: self._cancel_text_entry())
        entry.bind("<FocusOut>", lambda _e: self._commit_text_entry())

    def _cancel_text_entry(self) -> None:
        if self._text_entry is not None:
            self._text_entry = None
            self._clear_preview()

    def _commit_text_entry(self) -> None:
        if self._text_entry is None:
            return
        text = self._text_entry.get().strip()
        self._text_entry = None
        self._clear_preview()
        if text:
            self._commit({"type": "text", "color": self._color,
                          "width": self._width, "fontsize": max(16, self._width * 4),
                          "text": text, "points": [self._text_pos]})

    # ── Crop ──────────────────────────────────────────────────────────────────

    def _apply_crop(self, p1: tuple[int, int], p2: tuple[int, int]) -> None:
        box = (min(p1[0], p2[0]), min(p1[1], p2[1]),
               max(p1[0], p2[0]), max(p1[1], p2[1]))
        if box[2] - box[0] < 8 or box[3] - box[1] < 8:
            return
        self._push_undo()
        self._base_full = self._base_full.crop(box)
        dx, dy = box[0], box[1]
        for s in self._shapes:
            s["points"] = [(px - dx, py - dy) for (px, py) in s["points"]]
        self._recompute_scale()
        self._render_display()

    # ── Model / undo ─────────────────────────────────────────────────────────
    # Each undo entry snapshots (base_full, shapes).  The base only changes on a
    # crop, so non-crop entries simply re-reference the current base (no copy).

    def _commit(self, shape: dict) -> None:
        self._push_undo()
        self._shapes.append(shape)
        self._render_display()

    def _snapshot(self) -> tuple[Image.Image, list[dict]]:
        return (self._base_full, [dict(s) for s in self._shapes])

    def _restore(self, snap: tuple[Image.Image, list[dict]]) -> None:
        base, shapes = snap
        rescale = base is not self._base_full
        self._base_full = base
        self._shapes = shapes
        if rescale:
            self._recompute_scale()
        self._render_display()

    def _push_undo(self) -> None:
        self._undo.append(self._snapshot())
        if len(self._undo) > _MAX_UNDO:
            self._undo.pop(0)
        self._redo.clear()

    def _do_undo(self) -> None:
        if not self._undo:
            return
        self._redo.append(self._snapshot())
        self._restore(self._undo.pop())

    def _do_redo(self) -> None:
        if not self._redo:
            return
        self._undo.append(self._snapshot())
        self._restore(self._redo.pop())

    # ── Rendering ─────────────────────────────────────────────────────────────

    def _render_display(self) -> None:
        disp = _render(self._base_disp, self._shapes, self._scale)
        self._photo = ImageTk.PhotoImage(disp)
        self._canvas.itemconfig(self._img_id, image=self._photo)
        self._canvas.configure(width=self._disp_size[0], height=self._disp_size[1])

    def _render_full(self) -> Image.Image:
        return _render(self._base_full, self._shapes, 1.0)

    def _flash(self, msg: str) -> None:
        self._status.configure(text=msg)
        self._win.after(2500, lambda: self._status.configure(text=""))

    # ── Actions ───────────────────────────────────────────────────────────────

    def _do_save(self) -> None:
        try:
            os.makedirs(self._save_folder, exist_ok=True)
        except Exception:
            pass
        path = filedialog.asksaveasfilename(
            parent=self._win, title="Save screenshot",
            initialdir=self._save_folder, initialfile=self._suggested,
            defaultextension=".png",
            filetypes=[("PNG image", "*.png"), ("JPEG image", "*.jpg")],
        )
        if not path:
            return
        try:
            out = self._render_full()
            ext = os.path.splitext(path)[1].lower()
            if ext in (".jpg", ".jpeg"):
                out.save(path, format="JPEG", quality=self._jpg_quality)
            else:
                out.save(path, format="PNG")
            self._flash(f"Saved → {os.path.basename(path)}")
        except Exception as exc:
            self._flash(f"Save failed: {exc}")

    def _do_copy(self) -> None:
        try:
            clipboard_win.copy_image(self._render_full())
            self._flash("Copied to clipboard ✓")
        except Exception as exc:
            self._flash(f"Copy failed: {exc}")

    def _do_pin(self) -> None:
        img = self._render_full()
        _pin_image(self._win.master, img, self._scale, self._exclude_fn)
        self._flash("Pinned")


def _pin_image(root: tk.Misc, img: Image.Image, scale: float,
               exclude_fn: Callable[[tk.Misc], bool] | None) -> None:
    """Drop *img* as a frameless, draggable, always-on-top window."""
    pin = tk.Toplevel(root)
    pin.overrideredirect(True)
    pin.attributes("-topmost", True)
    pin.configure(bg=_ACCENT)
    if exclude_fn:
        try:
            exclude_fn(pin)
        except Exception:
            pass

    disp = img if scale >= 1.0 else img.resize(
        (max(1, int(img.width * scale)), max(1, int(img.height * scale))),
        Image.LANCZOS)
    photo = ImageTk.PhotoImage(disp)
    lbl = tk.Label(pin, image=photo, bd=0)
    lbl.image = photo                       # keep ref
    lbl.pack(padx=2, pady=2)                 # 2px accent border

    state = {"x": 0, "y": 0}
    lbl.bind("<ButtonPress-1>", lambda e: state.update(x=e.x, y=e.y))
    lbl.bind("<B1-Motion>", lambda e: pin.geometry(
        f"+{pin.winfo_x() + e.x - state['x']}+{pin.winfo_y() + e.y - state['y']}"))
    lbl.bind("<Double-Button-1>", lambda _e: _close_pin(pin))
    lbl.bind("<Button-3>",        lambda _e: _close_pin(pin))

    sw, sh = pin.winfo_screenwidth(), pin.winfo_screenheight()
    pin.update_idletasks()
    pin.geometry(f"+{(sw - pin.winfo_reqwidth()) // 2}+{(sh - pin.winfo_reqheight()) // 2}")
    _pins.append(pin)


def _close_pin(pin: tk.Toplevel) -> None:
    try:
        _pins.remove(pin)
    except ValueError:
        pass
    try:
        pin.destroy()
    except Exception:
        pass


def create_viewer(root: tk.Misc, image: Image.Image, *,
                  save_folder: str, suggested_name: str = "screenshot",
                  jpg_quality: int = 92,
                  exclude_fn: Callable[[tk.Misc], bool] | None = None) -> None:
    """Open the editor for *image*.  Must be called on the UI thread."""
    _Viewer(root, image, save_folder=save_folder, suggested_name=suggested_name,
            jpg_quality=jpg_quality, exclude_fn=exclude_fn)
