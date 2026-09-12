"""DYST (did you see that? 👀) — chroma-key pre-processing cache.

The expensive part of chroma keying is computing the per-frame alpha mask
(HSV inRange + erode + blur ≈ 50–60 ms per 720p frame in Python). This
module pre-computes those masks ONCE per (media file × chroma settings) and
caches them on disk; playback then just decodes the video frame and applies
the cached alpha (~2–5 ms/frame), so even 60 fps clips play in real time.

Cache layout (beside the app, shared across daemon sessions):
  .cache/precache/<key>/meta.json   — source, mtime/size, settings fingerprint,
                                      fps, frame count, width/height
  .cache/precache/<key>/masks.raw   — raw uint8 alpha masks, N × H × W,
                                      memory-mapped at read time

Lazy policy (per user request): media is NOT preprocessed up-front. The FIRST
trigger that picks a video (or an explicit `--play`) preprocesses it — with
the chance loop paused while it runs — and every later occurrence reuses the
cache. `ensure()` invalidates automatically when the media file or the chroma
settings change; delete the `.cache/` folder to clear everything.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil

import cv2
import numpy as np

from dyst import chroma
from dyst.config import get_base_dir

log = logging.getLogger("dyst.precache")

CACHE_VERSION = 1


def cache_root(base_dir: str | None = None) -> str:
    """Directory holding all pre-processing caches (created on demand)."""
    root = os.path.join(base_dir or get_base_dir(), ".cache", "precache")
    os.makedirs(root, exist_ok=True)
    return root


def _fingerprint(params: dict) -> str:
    """Stable fingerprint of the chroma settings this cache depends on."""
    relevant = {k: params.get(k) for k in
                ("preset", "hue_range", "saturation_range", "value_range", "despill")}
    return hashlib.sha1(json.dumps(relevant, sort_keys=True).encode()).hexdigest()[:12]


def cache_key(path: str, params: dict) -> str:
    """Cache dir name: chroma settings + the media file's identity (path,
    size, mtime) — changing either invalidates the cache automatically."""
    st = os.stat(path)
    ident = "|".join([
        os.path.abspath(path), str(st.st_size), str(int(st.st_mtime)),
        _fingerprint(params), f"v{CACHE_VERSION}",
    ])
    return hashlib.sha1(ident.encode()).hexdigest()[:16]


def cache_ready(path: str, params: dict) -> dict | None:
    """Return a cache handle {dir, masks, meta} if a valid cache exists,
    else None (caller may then preprocess). The memmap is opened lazily
    (np.memmap reads pages on demand — no full-file load)."""
    try:
        d = os.path.join(cache_root(), cache_key(path, params))
        meta_path = os.path.join(d, "meta.json")
        if not os.path.isfile(meta_path):
            return None
        with open(meta_path, "r", encoding="utf-8") as fh:
            meta = json.load(fh)
        if not os.path.isfile(os.path.join(d, "masks.raw")):
            return None
        masks = np.memmap(os.path.join(d, "masks.raw"), dtype=np.uint8,
                          mode="r", shape=(meta["frame_count"], meta["h"], meta["w"]))
        return {"dir": d, "masks": masks, "meta": meta}
    except (OSError, ValueError, KeyError):
        return None


def ensure(path: str, params: dict, progress=None) -> dict | None:
    """Preprocess *path* with chroma *params* and cache the alpha masks.

    Blocking call (≈50–60 ms per frame; callers should pause spawning or run
    it on a worker thread). `progress(done, total)` is invoked periodically.
    Returns the same handle as `cache_ready()` on success, None on failure
    (caller falls back to live keying). Existing cache is reused when valid.
    """
    hit = cache_ready(path, params)
    if hit is not None:
        log.debug("precache: cache HIT for %s", os.path.basename(path))
        return hit

    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        log.warning("precache: cannot open video %s", path)
        return None
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        key = cache_key(path, params)
        d = os.path.join(cache_root(), key)
        tmp = d + ".tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp, exist_ok=True)
        h = w = None
        count = 0
        # Stream masks straight to disk — no giant RAM copy, so long clips
        # can be preprocessed too (np.memmap reads pages on demand later).
        with open(os.path.join(tmp, "masks.raw"), "wb") as raw:
            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                if h is None:
                    h, w = frame.shape[:2]
                raw.write(chroma._screen_mask(frame, params).tobytes())
                count += 1
                if progress and (count % 25 == 0 or count == total):
                    progress(count, total if total > 0 else count)
    except OSError as exc:
        log.warning("precache: failed preprocessing %s (%s)", path, exc)
        shutil.rmtree(tmp, ignore_errors=True)
        return None
    finally:
        cap.release()
    if not count:
        log.warning("precache: no frames decoded from %s", path)
        shutil.rmtree(tmp, ignore_errors=True)
        return None

    try:
        st = os.stat(path)
        meta = {
            "version": CACHE_VERSION,
            "source": os.path.abspath(path),
            "size": st.st_size,
            "mtime": int(st.st_mtime),
            "fingerprint": _fingerprint(params),
            "fps": fps,
            "frame_count": count,
            "h": h, "w": w,
        }
        with open(os.path.join(tmp, "meta.json"), "w", encoding="utf-8") as fh:
            json.dump(meta, fh, indent=2)
        if os.path.isdir(d):
            shutil.rmtree(d)
        os.rename(tmp, d)
    except OSError as exc:
        log.warning("precache: failed writing cache for %s (%s)", path, exc)
        shutil.rmtree(tmp, ignore_errors=True)
        return None

    log.info("precache: cached %d keyed frames for %s (%.1fs video, key %s)",
             count, os.path.basename(path), count / fps, key)
    masks_arr = np.memmap(os.path.join(d, "masks.raw"), dtype=np.uint8,
                          mode="r", shape=(count, h, w))
    return {"dir": d, "masks": masks_arr, "meta": meta}
