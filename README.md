# HDR Screenshot Tool for Windows

A lightweight system-tray tool for capturing **HDR screenshots** on Windows 10/11. It grabs the raw HDR frame, tone-maps it to a correct SDR PNG (or exports a real **10-bit HDR AVIF**), and copies it to the clipboard — instantly, from a hotkey or a floating toolbar. An optional post-capture editor lets you annotate, crop, and pin before saving.

![Windows 11](https://img.shields.io/badge/Windows-10%2F11-blue)
![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue)
![License MIT](https://img.shields.io/badge/license-MIT-green)

---

![Comparison: Snipping Tool vs HDR Screenshot Tool — Death Stranding 2](docs/comparison.png)

## Why

Standard Windows capture tools (Snipping Tool, Win+PrintScreen) grab frames in SDR — HDR highlights are clipped or blown out. This tool captures the raw frame in **FP16 scRGB** directly via the DXGI Desktop Duplication API, then applies tone mapping (the same method used by OBS) and saves a correctly tone-mapped SDR PNG.

Because it captures at the compositor level via DXGI — not through the GDI/BitBlt pipeline — it works correctly everywhere: games, browsers, and desktop apps alike.

## Features

- **Floating toolbar** — a draggable, always-on-top bar with one-click capture, fixed-size mode, and settings. Hidden from your own screenshots, so it never lands in the shot.
- **Full-screen capture** — configurable hotkey (default `Ctrl+Shift+H`), captures the monitor under the cursor.
- **Region capture** — configurable hotkey, with two modes:
  - **Free** — drag to select any crop area over a live preview overlay.
  - **Fixed (stamp)** — set a width × height and click to drop a fixed-size box.
- **Active-window capture** — configurable hotkey (default `Ctrl+Shift+W`), grabs just the foreground window.
- **True HDR export** — save a real 10-bit **AVIF** (PQ / Rec.2020) that HDR-aware viewers display with full highlights, not just a tone-mapped SDR PNG. Choose SDR, HDR, or both. Falls back to 16-bit PNG when the AVIF encoder is unavailable.
- **HDR → SDR tone mapping** with adjustable SDR white point (in nits).
- **Four tone-mapping algorithms** — Windows/OBS-style (recommended), Windows + highlight roll-off, ACES filmic [test], Reinhard [test].
- **Post-capture editor** — optionally open captures in a built-in annotator: rectangle, ellipse, arrow, line, pen, highlighter, text, blur/redact, and crop, then Save (PNG/JPG), Copy, or **Pin** as a floating always-on-top window.
- **Cursor capture** — optionally composite the mouse cursor into the shot (off by default).
- **Auto-copy to clipboard** after every capture.
- **Fast in-process notifications** — a toast with a screenshot thumbnail, rendered in-process (no PowerShell spawn). Click it to open the file.
- **System tray** — lives quietly in the notification area; toggle the toolbar, capture, or open settings from the menu.
- **Multi-monitor support** — captures the monitor under the cursor, with correct Win32↔DXGI index mapping.
- **SDR fallback** — if HDR is not active on the target monitor, falls back to standard SDR capture automatically.
- **Configurable hotkeys** — rebind in Settings by pressing a combo; global hotkeys can be toggled off entirely (toolbar and tray still work).
- **Start with Windows** — optional autostart, configurable in settings.
- **Single instance** — launching again just points you at the existing tray icon.

## Download

> **No Python required** — just download and run.

| File | Description |
|------|-------------|
| [HDR_Screenshot.exe](https://github.com/pjmdevelopment/HDR_Screenshot/releases/latest/download/HDR_Screenshot.exe) | Standalone executable (Windows 10/11) |

> **Note:** Windows SmartScreen may warn about an unsigned executable. See [Troubleshooting](#troubleshooting) below.

Alternatively, run from source — see [Installation](#installation).

## Usage

After launch, the app sits in the system tray and shows a floating toolbar at the top of your primary monitor.

- **Toolbar** — `✛ New` (region), `⛶ Full` (full screen), `Fixed` + size boxes (stamp mode), `⚙` (settings), `✕` (hide toolbar). Drag it anywhere by the `⠿` grip.
- **Hotkeys** — `Ctrl+Shift+H` for full screen, `Ctrl+Shift+W` for the active window, and your configured region hotkey for region capture.
- **Tray menu** — right-click the tray icon for New screenshot, Full screen, Show toolbar, Settings, and Quit.

Every capture is tone-mapped (and/or exported as HDR), saved to your save folder, and copied to the clipboard, with a toast confirming the result. With **After capture → Open editor first** enabled, the shot opens in the annotator instead of being saved silently.

## Settings

Right-click the tray icon → **Settings…** (or the `⚙` toolbar button).

| Setting | Description |
|---------|-------------|
| Save folder | Where screenshots are saved |
| Save mode | SDR image (PNG/JPG), HDR (AVIF), or both |
| Tone mapping | Tone-mapping algorithm (applies to the SDR output) |
| SDR brightness (nits) | SDR white reference level (160–480 nits). Higher = darker output. Recommended: 200–250 |
| After capture | Save & copy instantly, or open the editor first |
| Capture cursor | Composite the mouse cursor into captures |
| Hotkey — Full screen | Hotkey for full-screen capture |
| Hotkey — Region | Hotkey for region capture |
| Hotkey — Window | Hotkey for active-window capture |
| Enable global hotkeys | Turn global hotkeys on/off (toolbar and tray still work when off) |
| Start with Windows | Launch automatically on system startup |

Fixed-mode and toolbar visibility are controlled directly from the toolbar and remembered across launches.

Settings are stored in `%LOCALAPPDATA%\HDRScreenshotTool\config.json` — the app works correctly from any location, including `Program Files`, without administrator privileges.

## Troubleshooting

### Windows Smart App Control / SmartScreen

On Windows 11 with **Smart App Control** enabled, the exe may be blocked with a message like _"Smart App Control blocked a potentially unsafe app"_. This is expected behaviour for unsigned executables from unknown publishers.

**Option 1 — disable Smart App Control** (one-time, does not affect overall system security):
> Settings → Privacy & Security → Windows Security → App & Browser Control → Smart App Control → **Off**

**Option 2 — run from source** (no permissions required):
```bash
python main.py
```

**Option 3 — classic SmartScreen** (if a "More info" button appears):
> Click "More info" → "Run anyway"

### HDR capture returns a black or corrupted image

> Make sure HDR is actually enabled in Windows display settings for your monitor. The tool detects HDR state per-monitor — if HDR is off, it falls back to standard SDR capture automatically.

### Screenshot looks identical to Snipping Tool output

> Your SDR brightness (nits) setting may be too high. Try lowering it to 200–250 nits in Settings. Higher values compress the HDR range more aggressively, making the result look similar to SDR capture.

### Hotkey not working

> Another application may have registered the same hotkey globally. Change the hotkey in Settings → Hotkey — Full screen / Hotkey — Region, or use the toolbar instead.

## Requirements

- Windows 10 version 1703+ (theoretical) or Windows 11 (tested)
- HDR-capable monitor and GPU with HDR enabled in Windows display settings
- Python 3.11+ (only when running from source)

## Installation

```bash
git clone https://github.com/pjmdevelopment/HDR_Screenshot.git
cd HDR_Screenshot
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

After launch, the tray icon and floating toolbar appear. Use the toolbar, the hotkeys, or right-click the tray icon to open Settings.

## Build as exe

```bash
pyinstaller --onefile --windowed --icon app.ico --add-data "app.ico;." -n HDR_Screenshot main.py
```

The executable will be in `dist\HDR_Screenshot.exe` — attach this to a GitHub Release so the [Download](#download) link resolves.

## Project structure

```
main.py              # entry point, tray icon, hotkeys, single-instance guard, screenshot workflow
capture.py           # camera pool with idle release, Win32↔DXGI monitor index mapping,
                     #   foreground-window rect, cursor compositing
cursor_win.py        # capture the mouse cursor as a straight-alpha BGRA bitmap (Win32 GDI)
dxgi_capture/        # custom FP16 capture via DXGI Desktop Duplication
  capture.py         # FP16Capture class, raw vtable D3D11 calls
tonemapping.py       # tone-mapping operators, PNG saving
hdr_export.py        # true HDR export — 10-bit AVIF, PQ/Rec.2020 (pillow-heif)
viewer.py            # post-capture editor: annotate, crop, save/copy/pin
ui.py                # shared UI layer: one persistent Tk root, floating toolbar,
                     #   region overlay, fast in-process toast, editor launcher
settings_window.py   # settings UI (customtkinter, lazily imported)
overlay.py           # region-selection overlay rendering (free + fixed modes)
clipboard_win.py     # copy image to Windows clipboard
notification.py      # toast helper — delegates to the in-process toast in ui.py
hdr_detect.py        # per-monitor HDR state detection
autostart.py         # "Start with Windows" registry entry
config.py            # config.json load/save
```

## How it works

1. When a capture is triggered (hotkey, toolbar, or tray), the monitor under the cursor — or the active window — is captured.
2. If that monitor is in HDR mode, `FP16Capture` is used (DXGI `R16G16B16A16_FLOAT` format); otherwise it falls back to dxcam BGRA capture.
3. The resulting float BGRA array (scRGB linear; values >1.0 are HDR highlights) is cropped for region/window captures, and the mouse cursor is composited in if **Capture cursor** is enabled.
4. Depending on **Save mode**, the frame is tone-mapped to an SDR PNG/JPG via `tonemapping.to_sdr()`, exported as a 10-bit HDR AVIF via `hdr_export.save_hdr_avif()`, or both.
5. The image is saved, copied to the clipboard, and a toast appears with a thumbnail — or, with **Open editor first**, the capture opens in the annotator instead.

The toolbar, overlay, and toast are all marked `WDA_EXCLUDEFROMCAPTURE` (Windows 10 2004+), so they stay visible to you but never appear in a screenshot — no hide-and-settle flicker before a grab.

### Tone mapping — Windows/OBS mode

Divides each pixel by `sdr_white_nits / 80`, clips to [0, 1], then applies sRGB gamma encoding. This maps SDR UI content to exactly [0, 1] while clipping HDR highlights to white — identical to OBS screen-recording output.

### Tone mapping — Windows + highlight roll-off

The same SDR paper-white normalisation, but instead of hard-clipping everything above a knee (default 0.8) to flat white, highlights roll smoothly into the top of the range via a Reinhard-style shoulder. Structure inside bright HDR highlights (skies, the sun, specular glints) is preserved rather than blown out, at the cost of paper-white mapping slightly below pure white.

### True HDR export — AVIF (PQ / Rec.2020)

When **Save mode** is HDR or both, the captured scRGB frame is converted Rec.709 → Rec.2020 in linear light, scaled to absolute luminance (scRGB 1.0 = 80 nits), PQ-encoded (SMPTE ST 2084), and written as a 10-bit AVIF with the correct NCLX/CICP tags (primaries 9 / transfer 16 / matrix 9). HDR-aware viewers (Chrome/Edge, Windows Photos with the AV1/HEVC extensions) display it with real highlights. This needs the optional `pillow-heif` dependency; without it, the tool falls back to a 16-bit PNG.

### FP16 capture internals

`FP16Capture` uses `IDXGIOutput5.DuplicateOutput1` to acquire frames in `DXGI_FORMAT_R16G16B16A16_FLOAT`. It calls `CopyResource`, `Map`, and `Unmap` on the D3D11 device context via **raw vtable calls** (bypassing comtypes dispatch, which corrupts D3D11 state in Python 3.14+).

## Known limitations

- DRM-protected content cannot be captured — a DXGI limitation.
- True HDR export requires the optional `pillow-heif` package (and a libheif build with an AVIF encoder); without it, HDR saves fall back to a 16-bit PNG.
- HDR AVIF files only look correct in HDR-aware viewers — in SDR-only image viewers they appear washed out or over-bright.

## License

MIT
