"""DYST (did you see that? 👀) — chroma key (green/blue screen removal).

Phase 3 (redone): HSV range mask -> feathered alpha for still images AND
every video frame (OpenCV pipeline — the same code path for both). Output is
always RGBA/BGRA with ~0 alpha where the screen colour was and ~255 on the
subject.

Improvements over the original Phase 3 version (user request: "masked wrong"
on green-screen clips, e.g. the elephant video):
- despill is ON by default (green fringe on subject edges was the #1
  "masked wrong" look; it is cheap edge-only pixel work),
- small holes in the subject spill are CLOSED (morphological fill),
- ``calibrate()`` re-centres the hue window on the video's ACTUAL screen
  colour (sampled from frame corners), so clips shot against a screen
  whose tint differs from the fixed preset window key correctly.

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

# Half-widths for re-centring the hue window on the detected screen colour
# (matched to each preset's original width).
_PRESET_HALFWIDTH = {
    "green": 25, "weak green": 32, "strong green": 18,
    "blue": 15, "weak blue": 27, "strong blue": 12, "": 25,
}

_ERODE = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)) if cv2 is not None else None


def calibrate(cap, params: dict) -> dict:
    """Re-centre ``hue_range`` on the video's real screen colour.

    Samples corner patches from up to 8 frames spread across the video, keeps
    the pixels that plausibly ARE the screen (saturation >= the configured
    floor, hue near the configured band), and returns a params copy whose
    hue window is centred on the measured hue (same width as the preset).
    If nothing screen-like is found, the original params are returned.

    Caller (precache) uses the result for the cached masks; playback only
    consumes existing masks, so calibration affects neither sync nor speed.
    """
    if cv2 is None:
        return params
    preset = str(params.get("preset") or "")
    hue_lo, hue_hi = params["hue_range"]
    sat_floor = params["saturation_range"][0]
    half = _PRESET_HALFWIDTH.get(preset, 25)
    hues: list = []
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0) or 1
    n_samples = min(8, max(1, total))
    positions = sorted({int(i * (total - 1) / max(1, n_samples - 1)) for i in range(n_samples)})
    for pos in positions:
        cap.set(cv2.CAP_PROP_POS_FRAMES, pos)
        ok, frame = cap.read()
        if not ok:
            continue
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        h_px, s_px = hsv[:, :, 0], hsv[:, :, 1]
        for cy, cx in ((0, 0), (0, hsv.shape[1] - 1), (hsv.shape[0] - 1, 0),
                       (hsv.shape[0] - 1, hsv.shape[1] - 1)):
            patch_h = h_px[max(0, cy - 8):cy + 8, max(0, cx - 8):cx + 8].ravel()
            patch_s = s_px[max(0, cy - 8):cy + 8, max(0, cx - 8):cx + 8].ravel()
            sel = (patch_s >= sat_floor) & (patch_h >= hue_lo - 10) & (patch_h <= hue_hi + 10)
            if sel.sum() >= 64:  # at least a 8x8 patch of plausible screen
                hues.extend(patch_h[sel].tolist())
    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
    if not hues:
        return params
    center = int(np.median(hues))
    lo = max(0, center - half)
    hi = min(179, center + half)
    out = dict(params)
    out["hue_range"] = [lo, hi] if lo <= hi else params["hue_range"]
    out["calibrated"] = center
    log.debug("chroma: calibrated hue window to %s (measured bg hue %s)",
              out["hue_range"], center)
    return out


def _screen_mask(bgr: "np.ndarray", params: dict) -> "np.ndarray":
    """Feathered keep-mask (0..255): 255 = subject, 0 = screen.

    inRange over hue/saturation/value -> slight erosion (removes bg spill
    creep into the subject) -> invert -> morphological CLOSE (fills small
    holes punched in the subject by reflected screen colour) -> Gaussian
    blur (soft, feathered edge).
    """
    hue = params.get("hue_range", [35, 85])
    sat = params.get("saturation_range", [40, 255])
    val = params.get("value_range", [40, 255])
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, (hue[0], sat[0], val[0]), (hue[1], sat[1], val[1]))
    mask = cv2.erode(mask, _ERODE, iterations=1)
    alpha = cv2.bitwise_not(mask)                       # subject = 255
    alpha = cv2.morphologyEx(alpha, cv2.MORPH_CLOSE, _ERODE, iterations=1)
    return cv2.GaussianBlur(alpha, (5, 5), 0)


def _despill(bgra: "np.ndarray", params: dict) -> None:
    """Green/blue spill removal on edge pixels (default ON).

    On partially-transparent edge pixels (the subject/background boundary)
    pull the dominant screen channel down so reflected screen colour doesn't
    tint the subject's outline. Which channel is pulled is decided from the
    hue window's centre (<= 90 -> green screen, else blue screen).
    """
    hue = params.get("hue_range", [35, 85])
    pull_green = (hue[0] + hue[1]) / 2.0 <= 90
    ch = 1 if pull_green else 2  # BGR: green=1, blue=2
    edge = (bgra[:, :, 3] > 0) & (bgra[:, :, 3] < 255)
    if not edge.any():
        return
    chan = bgra[:, :, ch].astype(np.int16)
    chan[edge] -= 40
    bgra[:, :, ch] = np.clip(chan, 0, 255).astype(np.uint8)


def chroma_key_frame(bgr: "np.ndarray", params: dict) -> "np.ndarray":
    """BGR video frame in -> BGRA frame out with the screen removed.

    The 4th channel (alpha) comes from the feathered mask; the original RGB
    pixels are kept untouched (except the default despill).
    """
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
    """PIL RGBA image in -> PIL RGBA image out with the screen removed.

    Existing alpha is preserved (the chroma mask only *removes* where it
    was already transparent, e.g. a transparent PNG's own alpha wins).
    """
    from PIL import Image  # local: only needed here

    rgba = np.asarray(img.convert("RGBA"))
    bgr = cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGR)
    keyed = chroma_key_frame(bgr, params)  # BGRA
    out = cv2.cvtColor(keyed, cv2.COLOR_BGRA2RGBA)
    if rgba.shape[2] == 4:
        out[:, :, 3] = np.minimum(out[:, :, 3], rgba[:, :, 3])
    return Image.fromarray(out, "RGBA")


def should_apply(path: str, chroma_cfg: dict, settings: dict | None = None) -> bool:
    """Decide whether chroma key applies to *path*.

    Caller-side gate (spec: exceptions/enabled are not the pipeline's job):
    - chroma_cfg.enabled False -> no
    - basename (case-insensitive) in chroma_cfg.exceptions -> no
    - per-file setting "chroma": False -> no (per-file wins over global)
    - otherwise -> yes
    """
    if not chroma_cfg.get("enabled", True):
        return False
    if settings is not None and settings.get("chroma") is False:
        return False
    name = os.path.basename(path).lower()
    return name not in {e.lower() for e in chroma_cfg.get("exceptions", [])}