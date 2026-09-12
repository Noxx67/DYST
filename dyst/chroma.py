"""DYST (did you see that? 👀) — chroma key (green/blue screen removal).

Phase 3: HSV range mask -> feathered alpha for still images AND every video
frame (OpenCV pipeline — the same code path for both). Output is always
RGBA/BGRA with ~0 alpha where the screen colour was and ~255 on the subject.

Config input is the validated ``chroma_key`` block from config.json:
  {enabled, preset, exceptions, hue_range, saturation_range, value_range,
   despill}
(only the ranges + despill are used here; enabled/exceptions/per-file
overrides are honoured by the caller via ``should_apply``).
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


def _screen_mask(bgr: "np.ndarray", params: dict) -> "np.ndarray":
    """Binary-later feathered keep-mask (0..255): 255 = subject, 0 = screen.

    inRange over hue/saturation/value -> slight erosion (removes bg spill
    creep into the subject) -> Gaussian blur (soft, feathered edge).
    """
    hue = params.get("hue_range", [35, 85])
    sat = params.get("saturation_range", [40, 255])
    val = params.get("value_range", [40, 255])
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, (hue[0], sat[0], val[0]), (hue[1], sat[1], val[1]))
    ker = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    mask = cv2.erode(mask, ker, iterations=1)
    mask = cv2.GaussianBlur(mask, (5, 5), 0)
    return cv2.bitwise_not(mask)  # alpha: 255 where the subject is


def _despill(bgra: "np.ndarray") -> None:
    """Basic green-spill removal (despill flag, default off).

    On partially-transparent edge pixels (the subject/background boundary)
    pull the green channel down so reflected screen colour doesn't tint the
    subject's outline. Documented as basic, per spec.
    """
    green = bgra[:, :, 1].astype(np.int16)
    edge = (bgra[:, :, 3] > 0) & (bgra[:, :, 3] < 255)
    green[edge] -= 40
    bgra[:, :, 1] = np.clip(green, 0, 255).astype(np.uint8)


def chroma_key_frame(bgr: "np.ndarray", params: dict) -> "np.ndarray":
    """BGR video frame in -> BGRA frame out with the screen removed.

    The 4th channel (alpha) comes from the feathered mask; the original RGB
    pixels are kept untouched (except optional despill).
    """
    t0 = time.perf_counter()
    alpha = _screen_mask(bgr, params)
    out = cv2.cvtColor(bgr, cv2.COLOR_BGR2BGRA)
    out[:, :, 3] = alpha
    if params.get("despill"):
        _despill(out)
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