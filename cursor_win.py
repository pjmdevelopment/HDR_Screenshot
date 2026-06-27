"""
Capture the current mouse cursor as a straight-alpha BGRA bitmap via Win32.

Why Win32 and not DXGI?
───────────────────────
DXGI Desktop Duplication never composites the cursor into the captured frame —
it exposes the pointer separately (position + a ``GetFramePointerShape`` blob).
That works only on the HDR duplication path, and the relevant comtypes method is
declared without paramflags (so it hits the same argument-count enforcement the
D3D11 calls work around).  Reading the cursor through GDI instead is one code
path that serves both the HDR (FP16) and SDR (dxcam) capture paths, and it
renders monochrome / colour / alpha cursors uniformly.

Public API
──────────
    get_cursor() -> (bgra, x, y) | None
        bgra : (H, W, 4) uint8, straight (non-premultiplied) alpha, channel
               order B, G, R, A.
        x, y : top-left position of the bitmap in virtual-desktop coordinates
               (hotspot already subtracted).
        Returns None when the cursor is hidden or unavailable.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt

import numpy as np

_user32 = ctypes.windll.user32
_gdi32  = ctypes.windll.gdi32

_CURSOR_SHOWING   = 0x00000001
_BI_RGB           = 0
_DIB_RGB_COLORS   = 0

HCURSOR = ctypes.c_void_p
HBITMAP = ctypes.c_void_p


class CURSORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize",      wt.DWORD),
        ("flags",       wt.DWORD),
        ("hCursor",     HCURSOR),
        ("ptScreenPos", wt.POINT),
    ]


class ICONINFO(ctypes.Structure):
    _fields_ = [
        ("fIcon",    wt.BOOL),
        ("xHotspot", wt.DWORD),
        ("yHotspot", wt.DWORD),
        ("hbmMask",  HBITMAP),
        ("hbmColor", HBITMAP),
    ]


class BITMAP(ctypes.Structure):
    _fields_ = [
        ("bmType",       wt.LONG),
        ("bmWidth",      wt.LONG),
        ("bmHeight",     wt.LONG),
        ("bmWidthBytes", wt.LONG),
        ("bmPlanes",     wt.WORD),
        ("bmBitsPixel",  wt.WORD),
        ("bmBits",       ctypes.c_void_p),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize",          wt.DWORD),
        ("biWidth",         wt.LONG),
        ("biHeight",        wt.LONG),
        ("biPlanes",        wt.WORD),
        ("biBitCount",      wt.WORD),
        ("biCompression",   wt.DWORD),
        ("biSizeImage",     wt.DWORD),
        ("biXPelsPerMeter", wt.LONG),
        ("biYPelsPerMeter", wt.LONG),
        ("biClrUsed",       wt.DWORD),
        ("biClrImportant",  wt.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [
        ("bmiHeader", BITMAPINFOHEADER),
        ("bmiColors", wt.DWORD * 3),
    ]


# ── Signatures (64-bit pointer safety) ────────────────────────────────────────
_user32.GetCursorInfo.argtypes = [ctypes.POINTER(CURSORINFO)]
_user32.GetCursorInfo.restype  = wt.BOOL
_user32.GetIconInfo.argtypes   = [HCURSOR, ctypes.POINTER(ICONINFO)]
_user32.GetIconInfo.restype    = wt.BOOL
_user32.GetDC.argtypes         = [wt.HWND]
_user32.GetDC.restype          = ctypes.c_void_p
_user32.ReleaseDC.argtypes     = [wt.HWND, ctypes.c_void_p]
_user32.ReleaseDC.restype      = ctypes.c_int

_gdi32.GetObjectW.argtypes  = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
_gdi32.GetObjectW.restype   = ctypes.c_int
_gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
_gdi32.DeleteObject.restype  = wt.BOOL
_gdi32.GetDIBits.argtypes = [
    ctypes.c_void_p, HBITMAP, wt.UINT, wt.UINT,
    ctypes.c_void_p, ctypes.POINTER(BITMAPINFO), wt.UINT,
]
_gdi32.GetDIBits.restype = ctypes.c_int


def _read_bitmap_32(hdc, hbm, w: int, h: int) -> "np.ndarray | None":
    """Read *hbm* as a top-down 32-bit BGRA array of shape (h, w, 4).

    A 1-bpp source (the AND/XOR mask) is expanded by GDI using the bitmap's
    colour table, so a set bit reads back as white (255) and a clear bit as
    black (0)."""
    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize        = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth       = w
    bmi.bmiHeader.biHeight      = -h          # negative → top-down rows
    bmi.bmiHeader.biPlanes      = 1
    bmi.bmiHeader.biBitCount    = 32
    bmi.bmiHeader.biCompression = _BI_RGB

    buf = (ctypes.c_ubyte * (w * h * 4))()
    got = _gdi32.GetDIBits(hdc, hbm, 0, h, buf, ctypes.byref(bmi), _DIB_RGB_COLORS)
    if got == 0:
        return None
    return np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4).copy()


def get_cursor() -> "tuple[np.ndarray, int, int] | None":
    ci = CURSORINFO()
    ci.cbSize = ctypes.sizeof(CURSORINFO)
    if not _user32.GetCursorInfo(ctypes.byref(ci)):
        return None
    if not (ci.flags & _CURSOR_SHOWING) or not ci.hCursor:
        return None

    ii = ICONINFO()
    if not _user32.GetIconInfo(ci.hCursor, ctypes.byref(ii)):
        return None

    hdc = _user32.GetDC(None)
    try:
        if ii.hbmColor:
            bm = BITMAP()
            _gdi32.GetObjectW(ii.hbmColor, ctypes.sizeof(BITMAP), ctypes.byref(bm))
            w, h = int(bm.bmWidth), int(bm.bmHeight)
            if w <= 0 or h <= 0:
                return None
            color = _read_bitmap_32(hdc, ii.hbmColor, w, h)
            if color is None:
                return None
            bgra = color.copy()

            if not color[:, :, 3].any():
                # Colour cursor without an alpha channel: reconstruct alpha from
                # the AND mask (set bit → transparent; clear bit → opaque).
                mask = _read_bitmap_32(hdc, ii.hbmMask, w, h)
                if mask is not None:
                    transparent = mask[:, :, 0] > 127
                    alpha = np.where(transparent, 0, 255).astype(np.uint8)
                    # AND-set pixels that still carry colour are XOR pixels —
                    # show them opaque rather than dropping them.
                    xor = transparent & (color[:, :, :3].any(axis=2))
                    alpha[xor] = 255
                    bgra[:, :, 3] = alpha
                else:
                    bgra[:, :, 3] = 255
        else:
            # Monochrome cursor: hbmMask is w × 2h — AND mask on top, XOR below.
            bm = BITMAP()
            _gdi32.GetObjectW(ii.hbmMask, ctypes.sizeof(BITMAP), ctypes.byref(bm))
            w, h2 = int(bm.bmWidth), int(bm.bmHeight)
            h = h2 // 2
            if w <= 0 or h <= 0:
                return None
            full = _read_bitmap_32(hdc, ii.hbmMask, w, h2)
            if full is None:
                return None
            andm = full[:h, :, 0] > 127
            xorm = full[h:, :, 0] > 127
            bgra = np.zeros((h, w, 4), dtype=np.uint8)
            white   = (~andm) & xorm          # opaque white
            black   = (~andm) & (~xorm)       # opaque black
            invert  = andm & xorm             # XOR-with-screen → render black
            bgra[white, 0:3] = 255
            bgra[white | black | invert, 3] = 255

        x = int(ci.ptScreenPos.x) - int(ii.xHotspot)
        y = int(ci.ptScreenPos.y) - int(ii.yHotspot)
        return bgra, x, y
    finally:
        if ii.hbmMask:
            _gdi32.DeleteObject(ii.hbmMask)
        if ii.hbmColor:
            _gdi32.DeleteObject(ii.hbmColor)
        if hdc:
            _user32.ReleaseDC(None, hdc)
