"""DYST (did you see that? 👀) — media scanning, validation and pairing.

Phase 2: folder scan with validation (corrupt files are skipped with a
warning), sidecar audio pairing (same base name in the same folder) and
per-file display settings (same-named .json/.txt, see AGENTS.md).
"""

from __future__ import annotations

import json
import logging
import os
import random
from dataclasses import dataclass, field

log = logging.getLogger("dyst.media")

IMAGE_EXTS = {".png", ".gif", ".apng", ".webp", ".jpg", ".jpeg", ".bmp"}
VIDEO_EXTS = {".mp4", ".webm", ".avi", ".mov", ".mkv"}
# Audio sidecars, in preference order (first available wins).
AUDIO_EXTS = [".mp3", ".wav", ".ogg", ".flac", ".m4a"]
VALID_MODES = {"fit", "cover", "stretch"}


@dataclass
class MediaItem:
    path: str
    kind: str  # "image" | "video"
    settings: dict = field(default_factory=dict)  # per-file overrides, see AGENTS.md
    sidecar_audio: str | None = None  # paired audio file, if any


def kind_of(path: str) -> str | None:
    """Return the kind ("image"/"video") for a file path by extension."""
    ext = os.path.splitext(path)[1].lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    return None


def validate_image(path: str) -> bool:
    """True if Pillow can decode the image; logs a warning + False otherwise."""
    try:
        from PIL import Image

        with Image.open(path) as im:
            im.verify()
        return True
    except Exception as exc:
        log.warning("media: skipping unreadable image %s (%s)", path, exc)
        return False


def validate_video(path: str) -> bool:
    """True if OpenCV can open the video and decode at least one frame."""
    try:
        import cv2

        cap = cv2.VideoCapture(path)
        try:
            if not cap.isOpened():
                raise ValueError("cannot open")
            ok, _ = cap.read()
            if not ok:
                raise ValueError("no decodable frames")
        finally:
            cap.release()
        return True
    except Exception as exc:
        log.warning("media: skipping unplayable video %s (%s)", path, exc)
        return False


def find_sidecar(path: str) -> str | None:
    """Find an audio sidecar: same base name (case-insensitive), same folder.

    Preference order: AUDIO_EXTS (.mp3 > .wav > .ogg > .flac > .m4a).
    Returns the sidecar path or None.
    """
    folder = os.path.dirname(path) or "."
    stem = os.path.splitext(os.path.basename(path))[0].lower()
    found: dict[str, str] = {}
    try:
        for name in os.listdir(folder):
            nbase, next_ = os.path.splitext(name)
            if nbase.lower() == stem and next_.lower() in AUDIO_EXTS:
                found.setdefault(next_.lower(), os.path.join(folder, name))
    except OSError:
        pass
    for ext in AUDIO_EXTS:
        if ext in found:
            return found[ext]
    return None


def _list_folder(folder: str, exts: set, kind: str) -> list[MediaItem]:
    if not os.path.isdir(folder):
        return []
    items = []
    for name in sorted(os.listdir(folder)):
        if os.path.splitext(name)[1].lower() not in exts:
            continue  # sidecar audio / settings files are NOT media
        full = os.path.join(folder, name)
        if kind == "image" and not validate_image(full):
            continue
        if kind == "video" and not validate_video(full):
            continue
        items.append(MediaItem(full, kind, load_settings(full), find_sidecar(full)))
    return items


def scan(root: str) -> list[MediaItem]:
    """Scan root/images + root/videos + root/gifs, validate files, return a merged pool."""
    items = _list_folder(os.path.join(root, "images"), IMAGE_EXTS, "image")
    items += _list_folder(os.path.join(root, "videos"), VIDEO_EXTS, "video")
    items += _list_folder(os.path.join(root, "gifs"), IMAGE_EXTS, "image")
    if not items:
        log.warning("media: no supported files found under %s", root)
    else:
        n_img = sum(1 for i in items if i.kind == "image")
        n_snd = sum(1 for i in items if i.sidecar_audio)
        log.info("media: found %d item(s) (images=%d videos=%d, with sidecar audio=%d)",
                 len(items), n_img, len(items) - n_img, n_snd)
    return items


def pick_from(pool: list[MediaItem]) -> MediaItem | None:
    """Uniform random pick from an already-scanned pool."""
    return random.choice(pool) if pool else None


def is_av1(path: str) -> bool:
    """True if the video's codec is AV1 (fourcc 'AV01').

    Used to route AV1 files away from QtMultimedia (which can't hardware-
    decode AV1 on some platforms) toward OpenCV + ffmpeg-extracted audio.
    """
    try:
        import cv2  # lazy: only needed for codec detection

        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            return False
        fc = int(cap.get(cv2.CAP_PROP_FOURCC))
        cap.release()
        codec = "".join(chr((fc >> (8 * i)) & 0xFF) for i in range(4)).upper()
        return codec == "AV01"
    except Exception:
        return False


# ---------------------------------------------------------------- settings

def _parse_txt_settings(path: str) -> dict:
    """Parse a simple key=value (or key: value) .txt settings file."""
    data = {}
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            sep = "=" if "=" in line else ":"
            if sep not in line:
                continue
            key, _, val = line.partition(sep)
            data[key.strip().lower()] = val.strip()
    return data


def _validate_settings(path: str, raw: dict) -> dict:
    """Keep only known, valid keys. Unknown/invalid entries are dropped
    (with a warning) rather than rejecting the whole file."""
    out = {}
    mode = raw.get("mode")
    if mode is not None:
        if isinstance(mode, str) and mode.lower() in VALID_MODES:
            out["mode"] = mode.lower()
        else:
            log.warning("media: %s: invalid mode %r (use fit/cover/stretch)", path, mode)
    duration = raw.get("duration")
    if duration is not None:
        try:
            d = float(duration)
            if d > 0:
                out["duration"] = d
            else:
                log.warning("media: %s: duration must be > 0", path)
        except (TypeError, ValueError):
            log.warning("media: %s: invalid duration %r", path, duration)
    volume = raw.get("volume")
    if volume is not None:
        try:
            v = float(volume)
            if 0.0 <= v <= 1.0:
                out["volume"] = v
            else:
                log.warning("media: %s: volume must be 0..1", path)
        except (TypeError, ValueError):
            log.warning("media: %s: invalid volume %r", path, volume)
    image_display = raw.get("image_display_seconds")
    if image_display is not None:
        try:
            d = float(image_display)
            if d > 0:
                out["image_display_seconds"] = d
            else:
                log.warning("media: %s: image_display_seconds must be > 0", path)
        except (TypeError, ValueError):
            log.warning("media: %s: invalid image_display_seconds %r", path, image_display)
    fade = raw.get("fade_seconds")
    if fade is not None:
        try:
            f = float(fade)
            if f >= 0:
                out["fade_seconds"] = f
            else:
                log.warning("media: %s: fade_seconds must be >= 0", path)
        except (TypeError, ValueError):
            log.warning("media: %s: invalid fade_seconds %r", path, fade)
    return out


def load_settings(path: str) -> dict:
    """Load per-file display settings from a same-named sidecar.

    Priority: <base>.json, then <base>.txt. Returns {} if none/invalid.
    JSON form:  {"mode": "cover", "duration": 3, "volume": 0.8}
    TXT form:   mode=cover / duration=3 / volume=0.8 (one per line)
    """
    base = os.path.splitext(path)[0]
    for ext in (".json", ".txt"):
        spath = base + ext
        if not os.path.isfile(spath):
            continue
        try:
            if ext == ".json":
                with open(spath, "r", encoding="utf-8") as fh:
                    raw = json.load(fh)
                if not isinstance(raw, dict):
                    raise ValueError("root must be an object")
            else:
                raw = _parse_txt_settings(spath)
        except Exception as exc:
            log.warning("media: invalid settings file %s (%s) — ignored", spath, exc)
            return {}
        return _validate_settings(spath, raw)
    return {}


def pick_random(root: str) -> MediaItem | None:
    """Convenience: scan then pick a random item."""
    return pick_from(scan(root))