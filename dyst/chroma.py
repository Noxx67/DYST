"""DYST (did you see that? 👀) — chroma key (green/blue screen removal).

Phase 3 (redone): HSV distance-based soft alpha matte → feathered alpha for still images AND
every video frame (OpenCV pipeline — the same code path for both). Output is
always RGBA/BGRA with ~0 alpha where the screen colour was and ~255 on the
subject.

Config input is the validated ``chroma_key`` block from config.json
(normalized dict: {enabled, preset, hue_range, saturation_range,
value_range, despill}); only the ranges + despill are used here —
enabled/exceptions/per-file overrides are honoured by the caller via
``should_apply``.
"""

from __future__ import annotations

import logging
import os
import time

try:
    import cv2
    import numpy as np
except ImportError:  # pragma: no cover - headless/early-import safety
    cv2 = np = None

log = logging.getLogger("dyst.chroma")

# Per-frame performance guard (spec): warn above this many ms for ~720p.
SLOW_FRAME_MS = 50.0

# ----------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------
# Reference height for kernel scaling (720p baseline)
_REF_HEIGHT = 720.0
# Minimum kernel size (odd numbers only for cv2 morphology)
_MIN_KSIZE = 3

# ----------------------------------------------------------------------
# Preset definitions (same as config.py _CHROMA_PRESETS, kept here for
# self-contained operation when calibrate() is called with raw params)
# ----------------------------------------------------------------------
_CHROMA_PRESETS: dict[str, dict[str, list[int]]] = {
    "green": {
        "hue_range": [35, 85],
        "saturation_range": [40, 255],
        "value_range": [40, 255],
    },
    "weak green": {
        "hue_range": [28, 92],
        "saturation_range": [20, 255],
        "value_range": [30, 255],
    },
    "strong green": {
        "hue_range": [42, 78],
        "saturation_range": [60, 255],
        "value_range": [50, 255],
    },
    "blue": {
        "hue_range": [100, 130],
        "saturation_range": [40, 255],
        "value_range": [40, 255],
    },
    "weak blue": {
        "hue_range": [90, 145],
        "saturation_range": [20, 255],
        "value_range": [30, 255],
    },
    "strong blue": {
        "hue_range": [105, 130],
        "saturation_range": [60, 255],
        "value_range": [50, 255],
    },
}


# ----------------------------------------------------------------------
# Resolution-adaptive kernel helpers
# ----------------------------------------------------------------------
def _scale_factor(h: int, w: int) -> float:
    """Scale factor relative to 720p height, used for kernel sizing.
    Scales with the geometric mean of height ratio to preserve aspect feel."""
    return max(1.0, (h / _REF_HEIGHT) ** 0.5)


def _odd_ksize(val: float) -> int:
    """Round to nearest odd integer >= 3."""
    k = int(round(val))
    if k % 2 == 0:
        k += 1
    return max(_MIN_KSIZE, k)


def _get_kernels(h: int, w: int) -> tuple[cv2.typing.MatLike, cv2.typing.MatLike, int]:
    """Return (erode_kernel, close_kernel, gaussian_ksize) scaled to frame size."""
    scale = _scale_factor(h, w)
    k_erode = _odd_ksize(3 * scale)
    k_close = _odd_ksize(5 * scale)
    k_blur = _odd_ksize(7 * scale)
    erode_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k_erode, k_erode))
    close_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k_close, k_close))
    return erode_kernel, close_kernel, k_blur


# ----------------------------------------------------------------------
# Core masking: soft alpha matte via HSV distance
# ----------------------------------------------------------------------
def _hue_distance(hue: np.ndarray, centre: float) -> np.ndarray:
    """Circular hue distance in OpenCV's 0..179 scale (wrap at 180)."""
    diff = np.abs(hue.astype(np.float32) - centre)
    return np.minimum(diff, 180 - diff)


def _screen_mask(bgr: np.ndarray, params: dict) -> np.ndarray:
    if cv2 is None:
        raise RuntimeError("OpenCV not available")

    h, w = bgr.shape[:2]

    # Convert to float32 for precise color operations
    bgr_f = bgr.astype(np.float32)
    b, g, r = bgr_f[:, :, 0], bgr_f[:, :, 1], bgr_f[:, :, 2]

    # HSV conversion
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    hue = hsv[:, :, 0].astype(np.float32)
    sat = hsv[:, :, 1].astype(np.float32) / 255.0
    val = hsv[:, :, 2].astype(np.float32) / 255.0

    hue_lo, hue_hi = params["hue_range"]
    centre = (hue_lo + hue_hi) * 0.5
    hue_halfwidth = max(1.0, (hue_hi - hue_lo) * 0.5)

    is_green_key = centre <= 90

    # 1. Standard HSV-based screen probability
    dh = _hue_distance(hue, centre) / hue_halfwidth
    sat_floor = params["saturation_range"][0] / 255.0
    val_floor = params["value_range"][0] / 255.0

    sat_gate = np.clip((sat - sat_floor) / (1.0 - sat_floor + 1e-6), 0.0, 1.0)
    val_gate = np.clip((val - val_floor) / (1.0 - val_floor + 1e-6), 0.0, 1.0)
    
    screen_prob_hsv = np.clip((0.8 - dh) / 0.5, 0.0, 1.0) * sat_gate * val_gate

    # 2. Color Dominance Keying (Catches compression artifacts, yellow/teal edge tint)
    if is_green_key:
        # Green excess over red and blue
        max_rb = np.maximum(r, b)
        green_excess = np.clip((g - max_rb) / (g + max_rb + 1e-7), 0.0, 1.0)
        screen_prob_dom = green_excess * sat_gate
    else:
        # Blue excess over red and green
        max_rg = np.maximum(r, g)
        blue_excess = np.clip((b - max_rg) / (b + max_rg + 1e-7), 0.0, 1.0)
        screen_prob_dom = blue_excess * sat_gate

    # Combine both methods (take maximum screen probability)
    screen_prob = np.maximum(screen_prob_hsv, screen_prob_dom)

    # Invert to generate Keep-Alpha mask
    alpha = 1.0 - screen_prob

    # Boost alpha contrast: sharpens edge removal and guarantees solid subject interiors
    alpha = np.clip((alpha - 0.15) / 0.70, 0.0, 1.0)

    # Morphological cleaning & resolution-adaptive softening
    alpha_u8 = (alpha * 255).astype(np.uint8)
    erode_kernel, close_kernel, k_blur = _get_kernels(h, w)
    
    # Slight erode eliminates residual pale borders around subject
    alpha_u8 = cv2.erode(alpha_u8, erode_kernel, iterations=1)
    alpha_u8 = cv2.morphologyEx(alpha_u8, cv2.MORPH_CLOSE, close_kernel, iterations=1)
    alpha_u8 = cv2.GaussianBlur(alpha_u8, (k_blur, k_blur), 0)

    return alpha_u8


# ----------------------------------------------------------------------
# Despill: proportional channel clamping on ALL visible pixels
# ----------------------------------------------------------------------
def _despill(bgra: np.ndarray, params: dict) -> None:
    if not params.get("despill"):
        return

    hue_lo, hue_hi = params["hue_range"]
    diff = (hue_hi - hue_lo) % 180
    centre = (hue_lo + diff * 0.5) % 180
    is_green = centre <= 90

    alpha = bgra[:, :, 3].astype(np.float32) / 255.0
    visible = alpha > 0.05

    if not np.any(visible):
        return

    if is_green:
        r = bgra[visible, 2].astype(np.float32)
        g = bgra[visible, 1].astype(np.float32)
        b = bgra[visible, 0].astype(np.float32)
        
        max_rb = np.maximum(r, b)
        spill = np.maximum(0.0, g - max_rb)
        bgra[visible, 1] = np.clip(g - spill, 0, 255).astype(np.uint8)


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------
def calibrate(cap, params: dict) -> dict:
    """Re-centre the hue window on the video's actual screen colour."""
    if cv2 is None:
        return params

    hue_lo, hue_hi = params["hue_range"]
    sat_floor = params["saturation_range"][0]
    val_floor = params["value_range"][0]
    half = (hue_hi - hue_lo) * 0.5

    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0) or 1
    n_samples = min(12, max(1, total))
    positions = sorted({int(i * (total - 1) / max(1, n_samples - 1)) for i in range(n_samples)})

    hues: list = []
    for pos in positions:
        cap.set(cv2.CAP_PROP_POS_FRAMES, pos)
        ok, frame = cap.read()
        if not ok:
            continue
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        h_px, s_px, v_px = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]

        strip = 8
        patches = [
            h_px[:strip, :],
            h_px[-strip:, :],
            h_px[:, :strip],
            h_px[:, -strip:],
        ]
        s_patches = [
            s_px[:strip, :], s_px[-strip:, :],
            s_px[:, :strip], s_px[:, -strip:],
        ]
        v_patches = [
            v_px[:strip, :], v_px[-strip:, :],
            v_px[:, :strip], v_px[:, -strip:],
        ]

        for patch_h, patch_s, patch_v in zip(patches, s_patches, v_patches):
            mask = (patch_s >= sat_floor) & (patch_v >= val_floor)
            if not np.any(mask):
                continue
            var = cv2.Laplacian(patch_h.astype(np.float32), cv2.CV_32F, ksize=3) ** 2
            var_mask = var < 50.0
            mask = mask & var_mask
            if np.any(mask):
                hues.extend(patch_h[mask].tolist())

    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)

    if not hues:
        log.debug("chroma: calibration found no screen pixels — keeping original hue window")
        return params

    median_hue = int(np.median(hues))
    lo = max(0, median_hue - half)
    hi = min(179, median_hue + half)
    out = dict(params)
    out["hue_range"] = [lo, hi] if lo <= hi else params["hue_range"]
    out["calibrated"] = median_hue
    log.debug("chroma: calibrated hue window to %s (measured bg hue %s)",
              out["hue_range"], median_hue)
    return out


def chroma_key_frame(bgr: np.ndarray, params: dict) -> np.ndarray:
    """BGR video frame in -> BGRA frame out with the screen removed."""
    t0 = time.perf_counter()
    alpha = _screen_mask(bgr, params)
    out = cv2.cvtColor(bgr, cv2.COLOR_BGR2BGRA)
    out[:, :, 3] = alpha
    if params.get("despill"):
        _despill(out, params)
    dt = (time.perf_counter() - t0) * 1000.0
    if dt > SLOW_FRAME_MS:
        log.warning("chroma: slow frame %.0f ms (~720p should be < %d ms) — "
                    "use shorter/lower-res clips or tune ranges", dt, SLOW_FRAME_MS)
    return out


def chroma_key_image(img: "Image", params: dict) -> "Image":
    """PIL RGBA image in -> PIL RGBA image out with the screen removed."""
    from PIL import Image

    rgba = np.asarray(img.convert("RGBA"))
    bgr = cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGR)
    keyed = chroma_key_frame(bgr, params)
    out = cv2.cvtColor(keyed, cv2.COLOR_BGRA2RGBA)
    if rgba.shape[2] == 4:
        out[:, :, 3] = np.minimum(out[:, :, 3], rgba[:, :, 3])
    return Image.fromarray(out, "RGBA")


def should_apply(path: str, chroma_cfg: dict, settings: dict | None = None) -> bool:
    """Decide whether chroma key applies to *path*."""
    if not chroma_cfg.get("enabled", True):
        return False
    if settings is not None and settings.get("chroma") is False:
        return False
    name = os.path.basename(path).lower()
    return name not in {e.lower() for e in chroma_cfg.get("exceptions", [])}