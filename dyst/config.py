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
    "play_once": False,          # do not start the same media twice concurrently
    "reroll_in_same_tick": True,
    # Media
    "media_folder": "media",
    "image_display_seconds": 1.0,
    "end_on_audio_end": False,  # images: disappear when the sidecar audio ends (display timer still applies if shorter)
    "fade_out_seconds": 0.2,
    "fade_in_seconds": 0.0,  # fade-in before display (images/gifs); 0 = off
    "opacity": 1.0,          # overlay opacity 0.0-1.0 (1 = fully opaque)
    "max_duration": 0.0,     # hard cap (seconds) on any overlay; 0 = play to natural end
    "speed": 1.0,            # playback speed multiplier (>0): videos/gifs/audio/images/fades
    "pitch": 1.0,            # audio pitch multiplier (>0): sidecar + audio-bearing media
    "speed_pitch": 0.0,      # combined speed+pitch: >0 sets BOTH and overrides speed/pitch; 0 = off
    # Playback performance caps (0 = no cap)
    "max_playback_height": 480,  # decode/key/paint height cap (px): taller videos/GIFs are
                                  # downscaled keeping aspect BEFORE masking/copying/painting
                                  # (cheap paint at the cost of sharpness) — 0 = native res
    "max_playback_fps": 30,      # effective playback framerate cap (fps): videos above it are
                                  # frame-sampled (audio untouched) — 0 = native framerate
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
    "volume": 0.8,              # master volume/gain 0.0-5.0 (1.0 = 100%; >1 boosts)
    # Chroma key (green/blue screen removal)
    "chroma_key": {
        "enabled": True,
        "preset": "green",
        # Named presets: "" (manual ranges) | green | weak green | strong
        # green | blue | weak blue | strong blue. Simple string form in the
        # user config: "chroma_key": "green" (or "blue"/"off"); the old
        # dict form {enabled, preset, hue_range, saturation_range,
        # value_range, despill, exceptions} still loads (expert tuning).
        "exceptions": [],
        "hue_range": [35, 85],
        "saturation_range": [40, 255],
        "value_range": [40, 255],
        # Despill defaults ON (Phase 3 redo): green fringe on subject edges
        # was the #1 "masked wrong" look. Edge-only pixel work, cheap.
        "despill": True,
    },
    # Custom chroma key: ONLY used when `chroma_key` is set to "custom"
    # (or the dict form uses preset "custom"). Ignored otherwise.
    # Hue is OpenCV's 0..179 scale (green ~60, blue ~120), sat/val 0..255.
    "chroma_hue_range": [35, 85],
    "chroma_saturation_range": [40, 255],
    "chroma_value_range": [40, 255],
    # Misc
    "download_max_height": 1080,  # max video height (px) for the downloader
    "rescan_seconds": 0,          # daemon: re-scan media folder every N secs (0=off)
    "autostart": False,
    "show_console": False,        # true = visible log terminal; false = hidden background
    "debug": False,
    "kill_hotkey": "ctrl+shift+alt+k",  # global hotkey to terminate the app (Windows only; empty = disabled)
    "kill_notify": True,           # show a Windows notification when the kill switch fires
}

# Per-key validators. Each returns True if the value is acceptable.
_CHROMA_KEY_DEFAULTS = DEFAULTS["chroma_key"]

# Named chroma-key presets: a `preset` string in the config fills in the
# hue/saturation/value ranges (as defaults); explicit per-key ranges in the
# user config still override them. Hue is OpenCV's 0..179 scale
# (green ≈ 60, blue ≈ 120). "weak" = wider/fainter catch (uneven or dim
# screens), "strong" = tighter, vivid screens with less risk of punching
# out the subject.
_CHROMA_PRESETS: Dict[str, Dict[str, Any]] = {
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
    "black": {
        "hue_range": [0, 179],
        "saturation_range": [0, 255],
        "value_range": [0, 35],
    },
    "white": {
        "hue_range": [0, 179],
        "saturation_range": [0, 30],
        "value_range": [220, 255],
    },
}


def chroma_preset_cfg(preset_name: str, base_cfg: dict) -> dict:
    """Return a chroma config based on `base_cfg` with the named
    preset's hue/sat/val ranges swapped in.
    Used for per-file `chroma_key` overrides in media sidecars —
    each file can use its own preset (green, blue, weak/strong
    variants) independent of the global config. `base_cfg`
    (the global chroma_key dict) is copied so enabled/exceptions/
    despill are inherited; the result has enabled=True (choosing a
    preset means filter this file).
    """
    name = str(preset_name).strip().lower()
    if name == "custom":
        out = dict(base_cfg)
        out["preset"] = "custom"
        out["enabled"] = True
        return out
    preset = _CHROMA_PRESETS.get(name)
    if preset is None:
        log.warning("chroma_preset_cfg: unknown preset %r — using base config as-is",
                    preset_name)
        return dict(base_cfg)
    out = dict(base_cfg)
    out["preset"] = name
    out["enabled"] = True
    for k in ("hue_range", "saturation_range", "value_range"):
        out[k] = list(preset[k])
    return out


# Per-file sibling keys for the "custom" chroma_key preset.
# Ignored unless preset == "custom" (checked by the callers).
_CHROMA_CUSTOM_KEYS = {
    "chroma_hue_range": ("hue_range", 0, 179),
    "chroma_saturation_range": ("saturation_range", 0, 255),
    "chroma_value_range": ("value_range", 0, 255),
}


def apply_custom_chroma_ranges(out: dict, source: dict) -> dict:
    """Fill a chroma config's ranges from sibling keys.

    Only acts when `out["preset"] == "custom"` — otherwise returns
    `out` unchanged so the keys are ignored for every other preset.
    `source` is the dict the keys were read from (config.json or a
    per-file sidecar).

    Hue is OpenCV's 0..179 scale; saturation/value are 0..255.
    """
    if str(out.get("preset", "")).strip().lower() != "custom":
        return out
    for src_key, (dest, lo, hi) in _CHROMA_CUSTOM_KEYS.items():
        if src_key not in source:
            continue
        val = source[src_key]
        if isinstance(val, (list, tuple)) and len(val) == 2 \
                and _is_num(val[0]) and _is_num(val[1]) \
                and lo <= int(val[0]) <= int(val[1]) <= hi:
            out[dest] = [int(val[0]), int(val[1])]
        else:
            log.warning("config: invalid %s %r (need [lo, hi], %d..%d) — ignoring",
                        src_key, val, lo, hi)
    return out


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


def _is_gain(v: Any) -> bool:
    """Volume/gain value: 0.0..5.0 (1.0 = 100%, values above 1.0 boost)."""
    return _is_num(v) and 0.0 <= v <= 5.0


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
    "play_once": (_is_bool, DEFAULTS["play_once"]),
    "reroll_in_same_tick": (_is_bool, DEFAULTS["reroll_in_same_tick"]),
    "media_folder": (lambda v: isinstance(v, str) and v != "", DEFAULTS["media_folder"]),
    "image_display_seconds": (_is_positive, DEFAULTS["image_display_seconds"]),
    "end_on_audio_end": (_is_bool, DEFAULTS["end_on_audio_end"]),
    "fade_out_seconds": (_is_nonnegative, DEFAULTS["fade_out_seconds"]),
    "fade_in_seconds": (_is_nonnegative, DEFAULTS["fade_in_seconds"]),
    "opacity": (_is_volume, DEFAULTS["opacity"]),
    "max_duration": (_is_nonnegative, DEFAULTS["max_duration"]),
    "speed": (_is_positive, DEFAULTS["speed"]),
    "pitch": (_is_positive, DEFAULTS["pitch"]),
    "speed_pitch": (_is_nonnegative, DEFAULTS["speed_pitch"]),
    "max_playback_height": (lambda v: _is_num(v) and v >= 0 and float(v).is_integer(), DEFAULTS["max_playback_height"]),
    "max_playback_fps": (_is_nonnegative, DEFAULTS["max_playback_fps"]),
    "monitor": (_is_monitor, DEFAULTS["monitor"]),
    "mode": (lambda v: isinstance(v, str) and v in ("fit", "cover-height", "cover-width", "stretch", "custom"), DEFAULTS["mode"]),
    "position_x": (lambda v: _is_num(v) and -1.0 <= v <= 2.0, DEFAULTS["position_x"]),
    "position_y": (lambda v: _is_num(v) and -1.0 <= v <= 2.0, DEFAULTS["position_y"]),
    "scale_x": (_is_positive, DEFAULTS["scale_x"]),
    "scale_y": (_is_positive, DEFAULTS["scale_y"]),
    "flip_h": (_is_bool, DEFAULTS["flip_h"]),
    "flip_v": (_is_bool, DEFAULTS["flip_v"]),
    "rotation": (_is_num, DEFAULTS["rotation"]),
    "volume": (_is_gain, DEFAULTS["volume"]),
    "download_max_height": (lambda v: _is_num(v) and v > 0 and float(v).is_integer(), DEFAULTS["download_max_height"]),
    "rescan_seconds": (lambda v: _is_num(v) and v >= 0 and float(v).is_integer(), DEFAULTS["rescan_seconds"]),
    "autostart": (_is_bool, DEFAULTS["autostart"]),
    "show_console": (_is_bool, DEFAULTS["show_console"]),
    "debug": (_is_bool, DEFAULTS["debug"]),
    "kill_hotkey": (lambda v: isinstance(v, str), DEFAULTS["kill_hotkey"]),
    "kill_notify": (_is_bool, DEFAULTS["kill_notify"]),
    "chroma_hue_range": (lambda v: _is_range(v, 0, 179), DEFAULTS["chroma_hue_range"]),
    "chroma_saturation_range": (lambda v: _is_range(v, 0, 255), DEFAULTS["chroma_saturation_range"]),
    "chroma_value_range": (lambda v: _is_range(v, 0, 255), DEFAULTS["chroma_value_range"]),
}


def _validate_chroma_key(value: Any) -> Dict[str, Any]:
    """Validate the chroma_key setting; fall back per-key to defaults.

    Accepts BOTH forms:
      "chroma_key": "green"      # string: off | green | blue | weak/strong variants
      "chroma_key": { ... }      # dict: enabled/preset/ranges/despill/exceptions
    The string form is the simple user-facing way; the dict is the expert
    escape hatch (old configs keep loading unchanged)."""
    # Simple string form: "off" | a preset name.
    if isinstance(value, str):
        name = value.strip().lower()
        if name in ("off", "false", "no", "none", "disabled"):
            out = copy.deepcopy(_CHROMA_KEY_DEFAULTS)
            out["enabled"] = False
            return out
        if name == "custom":
            # Ranges come from the sibling keys (chroma_hue_range /
            # chroma_saturation_range / chroma_value_range) — see
            # apply_custom_chroma_ranges(). Default ranges kept so
            # out is always a complete chroma dict.
            out = copy.deepcopy(_CHROMA_KEY_DEFAULTS)
            out["preset"] = "custom"
            return out
        if not name:
            return copy.deepcopy(_CHROMA_KEY_DEFAULTS)
        out = copy.deepcopy(_CHROMA_KEY_DEFAULTS)
        if name in _CHROMA_PRESETS:
            p = _CHROMA_PRESETS[name]
            out["preset"] = name
            out["hue_range"] = list(p["hue_range"])
            out["saturation_range"] = list(p["saturation_range"])
            out["value_range"] = list(p["value_range"])
        else:
            log.warning("config: unknown chroma_key %r (use: off, green, blue, weak green, strong green, weak blue, strong blue, black, white) — using defaults", value)
        return out
    if not isinstance(value, dict):
        log.warning("config: 'chroma_key' must be a string or dict — using defaults")
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
    # Named presets ("green" / "weak green" / "strong green" / "blue" /
    # "weak blue" / "strong blue") fill in the ranges; explicit per-key
    # ranges below still override them, so a preset is a starting point.
    try:
        preset = value["preset"]
        if isinstance(preset, str):
            name = preset.strip().lower()
            if not name:
                pass  # "" = manual ranges; nothing to apply
            elif name == "custom":
                out["preset"] = "custom"
                out = apply_custom_chroma_ranges(out, value)
            elif name in _CHROMA_PRESETS:
                out["preset"] = name
                p = _CHROMA_PRESETS[name]
                out["hue_range"] = list(p["hue_range"])
                out["saturation_range"] = list(p["saturation_range"])
                out["value_range"] = list(p["value_range"])
            else:
                log.warning("config: unknown chroma_key preset %r (use: green, blue, weak green, strong green, weak blue, strong blue, black, white or custom) — ignoring", preset)
        else:
            log.warning("config: chroma_key 'preset' must be a string — ignoring")
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
                    # "custom" preset: take ranges from the sibling
                    # keys chroma_hue_range / chroma_saturation_range /
                    # chroma_value_range (ignored for other presets).
                    cfg["chroma_key"] = apply_custom_chroma_ranges(
                        cfg["chroma_key"], user)

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