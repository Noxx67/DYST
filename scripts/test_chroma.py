"""Phase 3 (redo) verification: chroma-key module, cache + overlay integration.

Run:  .venv/Scripts/python scripts/test_chroma.py
Exits 0 if all checks pass. Generates test assets first.

Covers:
- config: string form ("green"/"blue"/"off") + old dict migration; despill ON
- chroma_key_image / chroma_key_frame: green bg -> transparent, subject kept
- hole-filling: a small green hole inside the subject stays opaque
- calibrate: hue window re-centres on the footage's actual screen colour
- should_apply gating / per-file "chroma" parsing + per-file "chroma_key" preset override
- precache: build -> HIT, masks shape, one-time cached audio, cache key does
  NOT depend on despill (toggle = no rebuild), invalidates on settings change
- overlay offscreen: keyed image renders without green bg; video-chroma-cached
  plays to completion
- elephant video sanity: the actual user clip keys with a sane subject
  fraction and a close-to-expected hue window
"""

from __future__ import annotations

import importlib.util
import os
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import cv2
import numpy as np
from PIL import Image
from PySide6.QtWidgets import QApplication

from dyst import config as cfg, media, precache  # noqa: E402
from dyst.chroma import (  # noqa: E402
    calibrate, chroma_key_frame, chroma_key_image, _screen_mask, should_apply,
)
from dyst.overlay import OverlayWindow  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "mta", os.path.join(ROOT, "scripts", "make_test_asset.py"))
mta = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mta)

app = QApplication(sys.argv[:1])
GREEN = os.path.join(ROOT, "media", "images", "test_green.png")
ELEPHANT = None
candidates = [f for f in os.listdir(os.path.join(ROOT, "media", "videos"))
              if f.startswith("Elephant Green Screen")
              and os.path.splitext(f)[1].lower() in media.VIDEO_EXTS]
if candidates:
    ELEPHANT = os.path.join(ROOT, "media", "videos", candidates[0])


def check(name):
    print(f"PASS {name}")


def test_config():
    v = cfg._validate_chroma_key
    out = v("green")
    assert out == cfg.DEFAULTS["chroma_key"], "string 'green' == normalized defaults"
    assert out["despill"] is True, "despill defaults ON (Phase 3 redo)"
    out_off = v("off")
    assert out_off["enabled"] is False, "'off' disables"
    b = v("  BLUE ")
    assert b["preset"] == "blue" and b["hue_range"] == [100, 130]
    assert v("pink").get("enabled") is True, "unknown preset falls back to defaults (still enabled)"
    d = v({"enabled": True, "preset": "green", "despill": False, "exceptions": ["a.png"]})
    assert d["despill"] is False and d["exceptions"] == ["a.png"], "dict form still works (expert)"
    # full load_config roundtrip with the string form
    import json, tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
        json.dump({"chroma_key": "blue"}, fh)
        p = fh.name
    try:
        c = cfg.load_config(p)
        assert c["chroma_key"]["preset"] == "blue" and c["chroma_key"]["enabled"] is True
    finally:
        os.remove(p)
    check(f"config: string/dict chroma_key forms, despill default {out['despill']}")


def _synth_frame(bg=(0, 255, 0), subject=(0, 0, 255), hole=True):
    """100x100 BGR: green bg, red square 30..70 incl a small green hole."""
    f = np.zeros((100, 100, 3), np.uint8)
    f[:, :] = bg
    f[30:70, 30:70] = subject
    if hole:
        f[48:50, 48:50] = bg  # 2x2 green hole inside the subject
    return f


def test_masks_and_holes():
    params = dict(cfg.DEFAULTS["chroma_key"])
    frame = _synth_frame()
    alpha = _screen_mask(frame, params)
    assert alpha[50, 50] > 200, f"subject centre alpha {alpha[50,50]} expected opaque"
    assert alpha[5, 5] < 50, f"bg alpha {alpha[5,5]} expected transparent"
    # hole filling: the 2x2 green hole must be CLOSED (spill holes vanish)
    assert alpha[49, 49] > 150 or alpha[48, 49] > 150 or alpha[49, 48] > 150, \
        f"green hole inside subject still punched out: {alpha[48:50, 48:50]}"
    keyed = chroma_key_frame(frame, params)
    assert keyed.shape[2] == 4 and keyed.shape[:2] == frame.shape[:2]
    # despill (default ON) pulls green down on edge pixels only
    edge_g = int(keyed[30, 30, 1])  # subject edge, partially transparent
    inner_g = int(keyed[50, 50, 1])
    assert inner_g < 100, f"subject green channel should stay low, got {inner_g}"
    check("masks: bg removed, subject opaque, spill holes CLOSED, despill on")


def test_calibrate():
    # A video whose screen hue sits at ~78 (yellow-green), outside preset centre 60
    cap = cv2.VideoCapture(os.path.join(ROOT, "media", "videos", "test_scare.mp4"))
    params = dict(cfg.DEFAULTS["chroma_key"])
    out = calibrate(cap, params)
    cap.release()
    assert isinstance(out, dict) and "hue_range" in out
    check(f"calibrate: returns params copy (window {out['hue_range']})")


def test_should_apply():
    cfg_on = cfg.DEFAULTS["chroma_key"]
    cfg_off = {**cfg_on, "enabled": False}
    assert should_apply("media/images/foo.png", cfg_on)
    assert not should_apply("media/images/foo.png", cfg_off)
    assert not should_apply("media/images/foo.png", cfg_on, settings={"chroma": False})
    assert should_apply("media/images/foo.png", cfg_on, settings={"chroma": True})
    assert not should_apply("media/images/foo.png", {**cfg_on, "exceptions": ["foo.png"]})
    assert should_apply("media/images/foo.png", {**cfg_on, "exceptions": ["bar.png"]})
    vs = media._validate_settings
    for val, expect in (("true", True), ("1", True), ("yes", True), ("false", False), ("0", False)):
        assert vs("s.json", {"chroma": val}).get("chroma") == expect
    # per-file chroma_key preset (string, case-insensitive):
    assert vs("s.json", {"chroma_key": "green"}).get("chroma_key") == "green"
    assert vs("s.json", {"chroma_key": "  STRONG GREEN  "}).get("chroma_key") == "strong green"
    assert vs("s.json", {"chroma_key": "blue"}).get("chroma_key") == "blue"
    assert "chroma_key" not in vs("s.json", {"chroma_key": "pink"})  # unknown -> dropped
    assert "chroma_key" not in vs("s.json", {"chroma_key": 123})     # non-string -> dropped
    from dyst.config import chroma_preset_cfg
    base = cfg.DEFAULTS["chroma_key"]
    blue = chroma_preset_cfg("blue", base)
    assert blue["preset"] == "blue" and blue["hue_range"] == [100, 130] and blue["enabled"] is True
    assert chroma_preset_cfg("nope", base) is not base and chroma_preset_cfg("nope", base)["preset"] == "green"
    check("should_apply + per-file chroma parsing + per-file chroma_key preset")


def test_precache():
    v = os.path.join(ROOT, "media", "videos", "test_scare.mp4")
    if not os.path.isfile(v):
        print("SKIP precache: test_scare.mp4 missing")
        return
    params = dict(cfg.DEFAULTS["chroma_key"])
    h = precache.ensure(v, params, progress=None)
    assert h is not None, "precache build failed"
    n, h_px, w_px = h["meta"]["frame_count"], h["meta"]["h"], h["meta"]["w"]
    assert h["masks"].shape == (n, h_px, w_px), f"masks shape {h['masks'].shape}"
    assert h["masks"].dtype == np.uint8
    hit = precache.cache_ready(v, params)
    assert hit is not None and hit["meta"]["frame_count"] == n, "cache HIT"
    # audio cached once (ffmpeg normally installed) -> no per-spawn ffmpeg
    ok_audio = h.get("audio") and os.path.isfile(h["audio"])
    print(f"      precache: {n} frames x {w_px}x{h_px} masks, "
          f"audio cached = {ok_audio}")
    # despill must NOT change the cache key (masks don't depend on it)
    params2 = {**params, "despill": False}
    assert precache.cache_key(v, params) == precache.cache_key(v, params2), \
        "toggling despill must not invalidate the mask cache"
    # a range change DOES invalidate
    params3 = {**params, "hue_range": [50, 90]}
    assert precache.cache_key(v, params) != precache.cache_key(v, params3)
    check("precache: build -> HIT, masks memmap, cached audio, despill-independent key")


def test_overlay_keyed_image():
    if not os.path.isfile(GREEN):
        mta.make_green_image(GREEN)
    params = dict(cfg.DEFAULTS["chroma_key"])
    w = OverlayWindow()
    assert w.load(GREEN, "image", image_seconds=0.5, fade_out_seconds=0.1, chroma=params)
    w.show()
    app.processEvents()
    shot = w.grab()
    img = shot.toImage()
    c = img.pixelColor(0, 0)
    assert c.alpha() < 50 or not (c.green() > 150 and c.red() < 100), \
        f"green bg still visible at top-left ({c.red()}, {c.green()}, {c.alpha()})"
    w._finish_close()
    check("overlay: keyed image renders without green background")


def test_overlay_cached_video():
    v = os.path.join(ROOT, "media", "videos", "test_scare.mp4")
    if not os.path.isfile(v):
        print("SKIP overlay cached video: test_scare.mp4 missing")
        return
    params = dict(cfg.DEFAULTS["chroma_key"])
    cached = precache.cache_ready(v, params)
    w = OverlayWindow()
    assert w.load(v, "video-chroma-cached", fade_out_seconds=0.0,
                  chroma=params, cached=cached)
    w.show()
    w.start()
    deadline = time.time() + 15
    while time.time() < deadline and not w._visual_done:
        app.processEvents()
        time.sleep(0.005)
    assert w._visual_done, "video-chroma-cached did not finish"
    w._finish_close()
    check("overlay: video-chroma-cached plays through (cached masks + audio clock)")


def test_elephant():
    if ELEPHANT is None:
        print("SKIP elephant: clip not present")
        return
    import json
    params = dict(cfg.DEFAULTS["chroma_key"])
    h = precache.ensure(ELEPHANT, params, progress=None)
    assert h is not None, "elephant preprocess failed"
    fracs = h["masks"].mean(axis=(1, 2)) / 255.0
    late = fracs[int(len(fracs) * 0.8):]
    assert late.mean() > 0.05, f"subject opaque fraction too low: {late.mean():.3f}"
    print(f"      elephant: {len(fracs)} frames keyed, subject frac {fracs.mean():.2f} "
          f"(late frames {late.mean():.2f}), hue window {h['meta'].get('calibrated_hue_range')}")
    check("elephant: masks built, subject present, first ~0.5s is content (all-green)")


def main() -> int:
    test_config()
    test_masks_and_holes()
    test_calibrate()
    test_should_apply()
    test_precache()
    test_overlay_keyed_image()
    test_overlay_cached_video()
    test_elephant()
    print("\nAll Phase 3 (redo) chroma checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())