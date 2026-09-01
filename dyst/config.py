"""Config loading, validation and defaults for DYST (did you see that? 👀).

Phase 0 implementation. Locked schema lives in AGENTS.md §8 — if you change
anything here, update that section too.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import sys
from typing import Any, Dict


def get_base_dir() -> str:
    """Returns the directory of the .exe when compiled, or application root in dev."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    # Refers to the parent directory of dyst/ config module
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


log = logging.getLogger("dyst.config")

APP_NAME = "DYST (did you see that? 👀)"

# Defaults mirror AGENTS.md §8 exactly. Missing keys in the user file merge
# over these; invalid values fall back to the default for that key.
DEFAULTS: Dict[str, Any] = {
    # Core loop
    "tick_seconds": 1.0,
    "odds": 1000,
    "max_concurrent": 3,
    "max_on_screen": 0,          # maximum number of video overlays shown at once; 0 = unlimited

    "reroll_in_same_tick": True,
    # Media
    "media_folder": "media",
    "image_display_seconds": 1.0,
    "fade_out_seconds": 0.2,
    "fade_in_seconds": 0.0,  # fade-in before display (images/gifs); 0 = off
    "opacity": 1.0,          # overlay opacity 0.0-1.0 (1 = fully opaque)
    "max_duration": 0.0,     # hard cap (seconds) on any overlay; 0 = play to natural end
    "speed": 1.0,            # playback speed multiplier (>0): videos/gifs/audio/images/fades
    "pitch": 1.0,            # audio pitch multiplier (>0): sidecar + audio-bearing media
    "speed_pitch": 0.0,      # combined speed+pitch: >0 sets BOTH and overrides speed/pitch; 0 = off
    # Display
    "monitor": "primary",
    "mode": "fit",  # how media covers the screen (fit/stretch/cover-height/cover-width/custom)
    # Custom-mode layout (only used when mode == "custom"; per-file sidecar wins)
    "position_x": 0.5,   # normalized X: 0 = left edge at screen left, 1 = right edge at screen right, -1..2 allowed (peek/crop)
    "position_y": 0.5,   # normalized Y: 0 = top edge at screen top, 1 = bottom edge at screen bottom, -1..2 allowed (peek/crop)
    "scale_x": 1.0,      # width multiplier relative to the "fit" size (1 = whole media visible, aspect kept)
    "scale_y": 1.0,      # height multiplier relative to the "fit" size
    "flip_h": False,     # mirror horizontally
    "flip_v": False,     # mirror vertically
    "rotation": 0.0,     # degrees (around the placed rect's center)
    # Audio
    "volume": 0.8,              # master volume 0.0-1.0
    # Chroma key
    "chroma_key": {
        "enabled": True,
        "exceptions": [],
        "hue_range": [35, 85],
        "saturation_range": [40, 255],
        "value_range": [40, 255],
        "despill": False,
    },
    # Misc
    "download_max_height": 1080,  # max video height (px) for the downloader
    "rescan_seconds": 0,          # daemon: re-scan media folder every N secs (0=off)
    "autostart": False,
    "show_console": False,        # true = visible log terminal; false = hidden background
    "debug": False,
}

# Per-key validators. Each returns True if the value is acceptable.
_CHROMA_KEY_DEFAULTS = DEFAULTS["chroma_key"]


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_bool(v: Any) -> bool:
    return isinstance(v, bool)


def _is_positive(v: Any) -> bool:
    return _is_num(v) and v > 0


def _is_nonnegative(v: Any) -> bool:
    return _is_num(v) and v >= 0


def _is_volume(v: Any) -> bool:
    return _is_num(v) and 0.0 <= v <= 1.0


def _is_monitor(v: Any) -> bool:
    if isinstance(v, str):
        return v == "primary"
    if isinstance(v, int) and not isinstance(v, bool):
        return v >= 0
    return False


def _is_range(v: Any, lo: int, hi: int) -> bool:
    return (
        isinstance(v, (list, tuple))
        and len(v) == 2
        and all(isinstance(x, int) and not isinstance(x, bool) for x in v)
        and lo <= v[0] <= v[1] <= hi
    )


# (validator, default) per top-level key.
_TOP_LEVEL_RULES = {
    "tick_seconds": (_is_positive, DEFAULTS["tick_seconds"]),
    "odds": (_is_positive, DEFAULTS["odds"]),
    "max_concurrent": (lambda v: _is_num(v) and v >= 0 and float(v).is_integer(), DEFAULTS["max_concurrent"]),
    "max_on_screen": (lambda v: _is_num(v) and v >= 0 and float(v).is_integer(), DEFAULTS["max_on_screen"]),

    "reroll_in_same_tick": (_is_bool, DEFAULTS["reroll_in_same_tick"]),
    "media_folder": (lambda v: isinstance(v, str) and v != "", DEFAULTS["media_folder"]),
    "image_display_seconds": (_is_positive, DEFAULTS["image_display_seconds"]),
    "fade_out_seconds": (_is_nonnegative, DEFAULTS["fade_out_seconds"]),
    "fade_in_seconds": (_is_nonnegative, DEFAULTS["fade_in_seconds"]),
    "opacity": (_is_volume, DEFAULTS["opacity"]),
    "max_duration": (_is_nonnegative, DEFAULTS["max_duration"]),
    "speed": (_is_positive, DEFAULTS["speed"]),
    "pitch": (_is_positive, DEFAULTS["pitch"]),
    "speed_pitch": (_is_nonnegative, DEFAULTS["speed_pitch"]),
    "monitor": (_is_monitor, DEFAULTS["monitor"]),
    "mode": (lambda v: isinstance(v, str) and v in ("fit", "cover-height", "cover-width", "stretch", "custom"), DEFAULTS["mode"]),
    "position_x": (lambda v: _is_num(v) and -1.0 <= v <= 2.0, DEFAULTS["position_x"]),
    "position_y": (lambda v: _is_num(v) and -1.0 <= v <= 2.0, DEFAULTS["position_y"]),
    "scale_x": (_is_positive, DEFAULTS["scale_x"]),
    "scale_y": (_is_positive, DEFAULTS["scale_y"]),
    "flip_h": (_is_bool, DEFAULTS["flip_h"]),
    "flip_v": (_is_bool, DEFAULTS["flip_v"]),
    "rotation": (_is_num, DEFAULTS["rotation"]),
    "volume": (_is_volume, DEFAULTS["volume"]),
    "download_max_height": (lambda v: _is_num(v) and v > 0 and float(v).is_integer(), DEFAULTS["download_max_height"]),
    "rescan_seconds": (lambda v: _is_num(v) and v >= 0 and float(v).is_integer(), DEFAULTS["rescan_seconds"]),
    "autostart": (_is_bool, DEFAULTS["autostart"]),
    "show_console": (_is_bool, DEFAULTS["show_console"]),
    "debug": (_is_bool, DEFAULTS["debug"]),
}


def _validate_chroma_key(value: Any) -> Dict[str, Any]:
    """Validate the nested chroma_key dict; fall back per-key to defaults."""
    if not isinstance(value, dict):
        log.warning("config: 'chroma_key' must be a dict — using defaults")
        return copy.deepcopy(_CHROMA_KEY_DEFAULTS)
    out = copy.deepcopy(_CHROMA_KEY_DEFAULTS)
    try:
        out["enabled"] = value["enabled"] if _is_bool(value["enabled"]) else _CHROMA_KEY_DEFAULTS["enabled"]
    except KeyError:
        pass
    try:
        exc = value["exceptions"]
        out["exceptions"] = (
            [e for e in exc if isinstance(e, str)] if isinstance(exc, list) else _CHROMA_KEY_DEFAULTS["exceptions"]
        )
    except KeyError:
        pass
    try:
        hr = value["hue_range"]
        out["hue_range"] = list(hr) if _is_range(hr, 0, 179) else _CHROMA_KEY_DEFAULTS["hue_range"]
    except KeyError:
        pass
    try:
        sr = value["saturation_range"]
        out["saturation_range"] = list(sr) if _is_range(sr, 0, 255) else _CHROMA_KEY_DEFAULTS["saturation_range"]
    except KeyError:
        pass
    try:
        vr = value["value_range"]
        out["value_range"] = list(vr) if _is_range(vr, 0, 255) else _CHROMA_KEY_DEFAULTS["value_range"]
    except KeyError:
        pass
    try:
        out["despill"] = value["despill"] if _is_bool(value["despill"]) else _CHROMA_KEY_DEFAULTS["despill"]
    except KeyError:
        pass
    return out


def load_config(path: str) -> Dict[str, Any]:
    """Load and validate config from *path*.

    Missing file → defaults. Missing keys → defaults. Invalid values → warning
    + fall back to the default for that key. Never raises on bad input.
    """
    cfg = copy.deepcopy(DEFAULTS)

    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                user = json.load(fh)
            if isinstance(user, dict):
                for key, (validator, default) in _TOP_LEVEL_RULES.items():
                    if key in user:
                        if not validator(user[key]):
                            log.warning("config: invalid value for '%s' (%r) — using default %r", key, user[key], default)
                        else:
                            cfg[key] = user[key]

                if "chroma_key" in user:
                    cfg["chroma_key"] = _validate_chroma_key(user["chroma_key"])

                # Deprecated alias: "fade_seconds" -> "fade_out_seconds".
                # The new key wins when both are present.
                if "fade_seconds" in user and "fade_out_seconds" not in user:
                    if _is_nonnegative(user["fade_seconds"]):
                        cfg["fade_out_seconds"] = user["fade_seconds"]
                        log.warning("config: 'fade_seconds' is deprecated — rename it to 'fade_out_seconds' in %s", path)
                    else:
                        log.warning("config: invalid 'fade_seconds' %r — ignoring", user["fade_seconds"])
            else:
                log.warning("config: root of %s is not an object — using defaults", path)
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("config: failed to read %s (%s) — using defaults", path, exc)
    else:
        log.info("config: no file at %s — using defaults", path)

    # Always converts relative media_folder paths regardless of how config loaded
    if not os.path.isabs(cfg["media_folder"]):
        cfg["media_folder"] = os.path.abspath(os.path.join(get_base_dir(), cfg["media_folder"]))

    return cfg


def save_config(path: str, cfg: Dict[str, Any]) -> None:
    """Write config back to *path* (used for autostart sync later).

    Non-fatal on error; logs a warning.
    """
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
        log.info("config: saved to %s", path)
    except OSError as exc:
        log.warning("config: could not save %s (%s)", path, exc)