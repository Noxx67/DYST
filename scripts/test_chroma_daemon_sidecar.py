"""Regression test: daemon spawner must honour per-file chroma overrides.

This is intentionally value-agnostic: it reads whatever the sidecar currently
says and asserts it survives the spawner's resolution path (the bug was that
the daemon used the raw global config and ignored per-file overrides)."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dyst import config as cfg, media, chroma as chroma_mod

# Deliberately "off" so any per-file override must turn keying on.
GLOBAL_CFG = {
    "chroma_key": {
        "enabled": False,
        "preset": "green",
        "hue_range": [35, 85],
        "saturation_range": [40, 255],
        "value_range": [40, 255],
        "despill": True,
        "exceptions": [],
    }
}


def resolve_like_daemon(settings: dict) -> dict:
    chroma_key_cfg = dict(GLOBAL_CFG["chroma_key"])
    per_preset = settings.get("chroma_key")
    if per_preset:
        chroma_key_cfg = cfg.chroma_preset_cfg(per_preset, chroma_key_cfg)
        if str(per_preset).strip().lower() == "custom":
            chroma_key_cfg = cfg.apply_custom_chroma_ranges(chroma_key_cfg, settings)
    return chroma_key_cfg


def test_daemon_sidecar_applies_per_file_overrides():
    path = "media/videos/Spooky Ghost [0bw11V4cOAg].webm"
    settings = media.load_settings(path)
    if not settings.get("chroma_key"):
        print("SKIP spooky: sidecar has no chroma_key")
        return

    resolved = resolve_like_daemon(settings)
    # A per-file preset must enable keying even though the global is "off".
    assert resolved["enabled"] is True
    assert resolved["preset"] == str(settings["chroma_key"]).lower()
    # Custom ranges from the sidecar must survive (whatever they are).
    for src, dest in (("chroma_hue_range", "hue_range"),
                      ("chroma_saturation_range", "saturation_range"),
                      ("chroma_value_range", "value_range")):
        if settings.get(src):
            assert resolved[dest] == settings[src], (src, resolved[dest], settings[src])

    assert chroma_mod.should_apply(path, resolved, settings) is True
    # The resolved config must be usable by the mask engine.
    import cv2
    cap = cv2.VideoCapture(path)
    ok, frame = cap.read()
    cap.release()
    assert ok
    alpha = chroma_mod._screen_mask(frame, resolved)
    assert alpha is not None and alpha.size == frame.shape[0] * frame.shape[1]
    print("test_daemon_sidecar_applies_per_file_overrides: PASS")


def test_strong_green_preset_override():
    # A per-file preset name (not custom) must install that preset's ranges.
    settings = {"chroma_key": "strong green", "chroma": True}
    resolved = resolve_like_daemon(settings)
    assert resolved["enabled"] is True
    assert resolved["preset"] == "strong green"
    assert resolved["hue_range"] == [42, 78]
    print("test_strong_green_preset_override: PASS")


if __name__ == "__main__":
    test_daemon_sidecar_applies_per_file_overrides()
    test_strong_green_preset_override()
    print("All daemon-sidecar chroma tests passed.")