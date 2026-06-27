"""
True HDR file export — encode the captured FP16 scRGB frame as a 10-bit AVIF
with PQ (SMPTE ST 2084) transfer and Rec.2020 primaries.

The capture pipeline keeps the frame in float32 scRGB linear (Rec.709 primaries,
1.0 = 80 nits, highlights > 1.0).  Tone-mapping throws that dynamic range away to
make an SDR PNG; here we preserve it in a real HDR container that HDR-aware
viewers (Chrome/Edge, Windows Photos with the AV1/HEVC extensions, etc.) display
with proper highlights.

Pipeline
────────
  scRGB linear (Rec.709)              frame[:, :, 2::-1]
    → Rec.2020 linear                3×3 primary conversion
    → absolute luminance             × 80 nits   (scRGB 1.0 = 80 nits)
    → PQ-encoded [0, 1]              normalise to 10 000 nits, ST 2084 OETF
    → 10-bit AVIF + NCLX/CICP        primaries 9 / transfer 16 / matrix 9

AVIF encoding needs ``pillow-heif`` (optional dependency).  ``is_available()``
reports whether it can be used so callers can fall back to the stdlib 16-bit PNG.
"""
from __future__ import annotations

import numpy as np

# Linear Rec.709 → linear Rec.2020 (Bradford-adapted), per ITU-R BT.2087.
_M_709_TO_2020 = np.array([
    [0.627403895934699, 0.329283038377884, 0.043313065687417],
    [0.069097289358232, 0.919540395075459, 0.011362315566309],
    [0.016391438875150, 0.088013307877226, 0.895595253247624],
], dtype=np.float32)

# SMPTE ST 2084 (PQ) constants.
_PQ_M1 = 0.1593017578125
_PQ_M2 = 78.84375
_PQ_C1 = 0.8359375
_PQ_C2 = 18.8515625
_PQ_C3 = 18.6875

_SCRGB_NITS = 80.0        # scRGB 1.0 == 80 nits (Windows reference white)
_PQ_PEAK_NITS = 10000.0   # PQ encodes absolute luminance up to 10 000 nits

DEFAULT_QUALITY = 90      # libheif AVIF quality (0–100; -1 would be lossless)

_AVAIL: "bool | None" = None


def is_available() -> bool:
    """True if AVIF HDR export is usable (pillow-heif importable)."""
    global _AVAIL
    if _AVAIL is None:
        try:
            import pillow_heif  # noqa: F401
            _AVAIL = True
        except Exception:
            _AVAIL = False
    return _AVAIL


def _pq_oetf(linear: np.ndarray) -> np.ndarray:
    """Linear luminance [0, 1] (1.0 = 10 000 nits) → PQ signal [0, 1]."""
    lp = np.power(np.clip(linear, 0.0, 1.0), _PQ_M1)
    return np.power((_PQ_C1 + _PQ_C2 * lp) / (1.0 + _PQ_C3 * lp), _PQ_M2)


def save_hdr_avif(frame: np.ndarray, path: str,
                  quality: int = DEFAULT_QUALITY) -> None:
    """
    Save *frame* (float32 BGRA scRGB linear) as a 10-bit PQ / Rec.2020 AVIF.

    Raises if pillow-heif is unavailable or libheif has no AVIF encoder; callers
    should catch and fall back to ``tonemapping.save_hdr_png``.
    """
    import pillow_heif
    from pillow_heif import (
        HeifColorPrimaries as CP,
        HeifTransferCharacteristics as TC,
        HeifMatrixCoefficients as MC,
    )

    rgb = frame[:, :, 2::-1].astype(np.float32)        # BGR→RGB, drop alpha
    rgb = np.maximum(rgb, 0.0)                          # scRGB can dip < 0
    h, w = rgb.shape[:2]

    # Rec.709 → Rec.2020 in linear light.
    rgb2020 = (rgb.reshape(-1, 3) @ _M_709_TO_2020.T).reshape(h, w, 3)
    rgb2020 = np.maximum(rgb2020, 0.0)

    # Absolute luminance → PQ signal.
    lin = rgb2020 * (_SCRGB_NITS / _PQ_PEAK_NITS)       # normalise to PQ peak
    pq  = _pq_oetf(lin)

    # libheif's RGB;16 path takes full-range uint16 and quantises to bit_depth.
    data = (np.clip(pq, 0.0, 1.0) * 65535.0 + 0.5).astype("<u2").tobytes()

    pillow_heif.encode(
        "RGB;16", (w, h), data, path,
        bit_depth=10, quality=quality, chroma=444,
        color_primaries=int(CP.ITU_R_BT_2020_2_AND_2100_0),    # 9
        transfer_characteristics=int(TC.ITU_R_BT_2100_0_PQ),   # 16  (plural key!)
        matrix_coefficients=int(MC.ITU_R_BT_2020_2_NON_CONSTANT_LUMINANCE),  # 9
        full_range_flag=1, save_nclx_profile=True,
    )
