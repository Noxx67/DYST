"""DYST (did you see that? 👀) — chroma-key pre-processing cache.

The expensive part of chroma keying is computing the per-frame alpha mask
(HSV inRange + erode + close + blur ≈ 50–60 ms per 720p frame in Python).
This module pre-computes those masks ONCE per (media file × chroma settings)
and caches them on disk; playback then just decodes the video frame and
applies the cached alpha (~2–5 ms/frame), so even 60 fps clips play in real
time.

Cache layout (beside the app, shared across daemon sessions):
  .cache/precache/<key>/meta.json   — source, mtime/size, settings fingerprint,
                                      fps, frame count, width/height, and the
                                      one-time extracted audio file name
  .cache/precache/<key>/masks.raw   — raw uint8 alpha masks, N × H × W,
                                      memory-mapped at read time
  .cache/precache/<key>/audio.m4a   — the video's embedded audio extracted
                                      ONCE here (best-effort): playback uses
                                      it instead of running ffmpeg per spawn
                                      (sidecar audio still wins at play time)

Improvements over the original (user request: smoother playback, no audio
drops, no terminal freezes):
- masks do NOT depend on despill (edge pixel work), so toggling despill no
  longer invalidates the cache,
- the hue window is auto-calibrated to the video's actual screen colour
  (chroma.calibrate) before keying, fixing "masked wrong" clips,
- audio is extracted into the cache once, so every later spawn is instant
  and cannot fail on an ffmpeg hiccup.

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

from dyst import chroma, ffmpeg_util
from dyst.config import get_base_dir

log = logging.getLogger("dyst.precache")

CACHE_VERSION = 2


def cache_root(base_dir: str | None = None) -> str:
    """Directory holding all pre-processing caches (created on demand)."""
    root = os.path.join(base_dir or get_base_dir(), ".cache", "precache")
    os.makedirs(root, exist_ok=True)
    return root


def _fingerprint(params: dict) -> str:
    """Stable fingerprint of the chroma settings this cache depends on.

    Note: despill is deliberately NOT included — despill is edge-pixel work
    applied at playback, so toggling it must NOT rebuild the masks.
    """
    relevant = {k: params.get(k) for k in ("preset", "hue_range", "saturation_range", "value_range")}
    return hashlib.sha1(json.dumps(relevant, sort_keys=True).encode()).hexdigest()[:12]


def cache_key(path: str, params: dict, max_height: int = 0, max_fps: float = 0) -> str:
    """Cache dir name: chroma settings + playback caps + the media file's
    identity (path, size, mtime) — changing any of them invalidates the
    cache automatically. Playback caps are part of the identity because the
    cached masks are built at the capped resolution/framerate."""
    st = os.stat(path)
    ident = "|".join([
        os.path.abspath(path), str(st.st_size), str(int(st.st_mtime)),
        _fingerprint(params), f"v{CACHE_VERSION}",
        f"h{int(max_height or 0)}", f"f{float(max_fps or 0)}",
    ])
    return hashlib.sha1(ident.encode()).hexdigest()[:16]


def cache_ready(path: str, params: dict, max_height: int = 0, max_fps: float = 0) -> dict | None:
    """Return a cache handle {dir, masks, meta, audio} if a valid cache
    exists, else None (caller may then preprocess). The memmap is opened
    lazily (np.memmap reads pages on demand — no full-file load)."""
    try:
        d = os.path.join(cache_root(), cache_key(path, params, max_height, max_fps))
        meta_path = os.path.join(d, "meta.json")
        if not os.path.isfile(meta_path):
            return None
        with open(meta_path, "r", encoding="utf-8") as fh:
            meta = json.load(fh)
        masks_path = os.path.join(d, "masks.raw")
        if not os.path.isfile(masks_path):
            return None
        masks = np.memmap(masks_path, dtype=np.uint8,
                          mode="r", shape=(meta["frame_count"], meta["h"], meta["w"]))
        audio = ""
        if meta.get("audio"):
            cand = os.path.join(d, meta["audio"])
            audio = cand if os.path.isfile(cand) else ""
        return {"dir": d, "masks": masks, "meta": meta, "audio": audio}
    except (OSError, ValueError, KeyError):
        return None


def _extract_audio_into(tmp_dir: str, path: str) -> str:
    """Best-effort: extract the video's embedded audio ONCE into the cache
    dir. Returns the stored file name ("" when none). Playback uses it so no
    per-spawn ffmpeg run is needed (sidecar still wins at play time)."""
    audio_path = ffmpeg_util.extract_audio(path)
    if audio_path is None:
        return ""
    try:
        target = os.path.join(tmp_dir, "audio.m4a")
        shutil.move(audio_path, target)
        return "audio.m4a"
    except OSError:
        try:
            os.remove(audio_path)
        except OSError:
            pass
        return ""


def _resize_for_caps(frame, max_height: int):
    """Downscale *frame* to the height cap keeping aspect (INTER_AREA for
    quality downsampling). Returns (frame, (w, h)) — unchanged + original
    dims when the cap is off or the frame is already small enough."""
    h, w = frame.shape[:2]
    if max_height <= 0 or h <= max_height:
        return frame, (w, h)
    h2 = max_height
    w2 = max(2, int(round(w * max_height / h)))
    return cv2.resize(frame, (w2, h2), interpolation=cv2.INTER_AREA), (w2, h2)


def ensure(path: str, params: dict, progress=None, max_height: int = 0, max_fps: float = 0) -> dict | None:
    """Preprocess *path* with chroma *params* and cache the alpha masks.

    Blocking call (≈50–60 ms per 720p frame; callers should pause spawning or run
    it on a worker thread). `progress(done, total)` is invoked periodically.
    Returns the same handle as `cache_ready()` on success, None on failure
    (caller falls back to live keying). Existing cache is reused when valid.

    max_height / max_fps: playback caps (same values the overlay uses). Frames
    are downscaled to the height cap BEFORE keying and ONLY every Nth frame
    (N = source_fps / max_fps, rounded up) is keyed — the cache is stored at
    the capped resolution/sampled framerate, so playback can never be slower
    than what the user asked for. meta["fps"] is the EFFECTIVE (sampled)
    framerate the overlay presents at."""
    hit = cache_ready(path, params, max_height, max_fps)
    if hit is not None:
        log.debug("precache: cache HIT for %s", os.path.basename(path))
        return hit

    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        log.warning("precache: cannot open video %s", path)
        return None
    # Auto-calibrate the hue window to this video's actual screen colour
    # BEFORE keying (fixes clips whose screen tint misses the fixed preset).
    key_params = chroma.calibrate(cap, params)
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        # Frame-sampling step: present every Nth source frame so the
        # playback framerate is capped (audio untouched, duration unchanged).
        step = max(1, int(round(fps / max_fps))) if max_fps > 0 else 1
        eff_fps = fps / step
        key = cache_key(path, params, max_height, max_fps)
        d = os.path.join(cache_root(), key)
        tmp = d + ".tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp, exist_ok=True)
        h = w = None
        count = 0
        read_i = 0  # 1-based sequential read counter
        # Stream masks straight to disk — no giant RAM copy, so long clips
        # can be preprocessed too (np.memmap reads pages on demand later).
        with open(os.path.join(tmp, "masks.raw"), "wb") as raw:
            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                read_i += 1
                if read_i % step != 0:
                    continue  # frame-sampling: skip non-presented source frames
                frame, (w, h) = _resize_for_caps(frame, max_height)
                raw.write(chroma._screen_mask(frame, key_params).tobytes())
                count += 1
                if progress and (count % 25 == 0 or count == total // step):
                    progress(count, (total // step) if total > 0 else count)
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
        audio_name = _extract_audio_into(tmp, path)
        meta = {
            "version": CACHE_VERSION,
            "source": os.path.abspath(path),
            "size": st.st_size,
            "mtime": int(st.st_mtime),
            "fingerprint": _fingerprint(params),
            "fps": eff_fps,
            "frame_step": step,
            "source_fps": fps,
            "frame_count": count,
            "h": h, "w": w,
            "max_playback_height": max_height,
            "max_playback_fps": max_fps,
            "audio": audio_name,
            "calibrated_hue_range": key_params.get("hue_range"),
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

    log.info("precache: cached %d keyed frames for %s (%.1fs video%s, key %s, %dx%d @%.0ffps%s)",
             count, os.path.basename(path), count / eff_fps,
             " + cached audio" if audio_name else "", key,
             w, h, eff_fps,
             " [downscaled]" if max_height and h < src_h else "")
    masks_arr = np.memmap(os.path.join(d, "masks.raw"), dtype=np.uint8,
                          mode="r", shape=(count, h, w))
    return {"dir": d, "masks": masks_arr, "meta": meta,
            "audio": os.path.join(d, audio_name) if audio_name else ""}