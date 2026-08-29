"""Config loading, validation and defaults for DYST (did you see that? 👀).

Phase 0 implementation. Locked schema lives in AGENTS.md §8 — if you change
anything here, update that section too.
"""

from __future__ import annotations

import copy
import json
import logging
import os
from typing import Any, Dict

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
    "fade_seconds": 0.2,
    # Display
    "monitor": "primary",
    # Audio
    "audio_volume": 0.8,
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
    "fade_seconds": (_is_nonnegative, DEFAULTS["fade_seconds"]),
    "monitor": (_is_monitor, DEFAULTS["monitor"]),
    "audio_volume": (_is_volume, DEFAULTS["audio_volume"]),
    "download_max_height": (lambda v: _is_num(v) and v > 0 and float(v).is_integer(), DEFAULTS["download_max_height"]),
    "rescan_seconds": (lambda v: _is_num(v) and v >= 0 and float(v).is_integer(), DEFAULTS["rescan_seconds"]),
    "autostart": (_is_bool, DEFAULTS["autostart"]),
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

    if not os.path.isfile(path):
        log.info("config: no file at %s — using defaults", path)
        return cfg

    try:
        with open(path, "r", encoding="utf-8") as fh:
            user = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("config: failed to read %s (%s) — using defaults", path, exc)
        return cfg

    if not isinstance(user, dict):
        log.warning("config: root of %s is not an object — using defaults", path)
        return cfg

    for key, (validator, default) in _TOP_LEVEL_RULES.items():
        if key not in user:
            continue
        if not validator(user[key]):
            log.warning("config: invalid value for '%s' (%r) — using default %r", key, user[key], default)
            continue
        cfg[key] = user[key]

    if "chroma_key" in user:
        cfg["chroma_key"] = _validate_chroma_key(user["chroma_key"])

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