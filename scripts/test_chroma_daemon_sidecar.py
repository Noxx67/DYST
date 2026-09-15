"""Regression test: daemon spawner must honour per-file chroma overrides."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dyst import config as cfg, media, chroma as chroma_mod

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


def test_spooky_ghost_sidecar():
    path = "media/videos/Spooky Ghost [0bw11V4cOAg].webm"
    settings = media.load_settings(path)
    assert settings.get("chroma") is True
    assert settings.get("chroma_key") == "custom"
    assert settings.get("chroma_hue_range") == [160, 179]
    assert settings.get("chroma_saturation_range") == [180, 255]
    assert settings.get("chroma_value_range") == [30, 50]

    # Simulate the fixed daemon spawner logic.
    chroma_key_cfg = dict(GLOBAL_CFG["chroma_key"])
    per_preset = settings.get("chroma_key")
    assert per_preset == "custom"
    chroma_key_cfg = cfg.chroma_preset_cfg(per_preset, chroma_key_cfg)
    chroma_key_cfg = cfg.apply_custom_chroma_ranges(chroma_key_cfg, settings)

    assert chroma_key_cfg["enabled"] is True
    assert chroma_key_cfg["preset"] == "custom"
    assert chroma_key_cfg["hue_range"] == [160, 179]
    assert chroma_key_cfg["saturation_range"] == [180, 255]
    assert chroma_key_cfg["value_range"] == [30, 50]

    use_chroma = chroma_mod.should_apply(path, chroma_key_cfg, settings)
    assert use_chroma is True

    # Sanity: the live mask must differ from the default green preset.
    import cv2
    import numpy as np
    cap = cv2.VideoCapture(path)
    ok, frame = cap.read()
    cap.release()
    assert ok, "need a frame for mask diff check"
    a_custom = chroma_mod._screen_mask(frame, chroma_key_cfg)
    a_green = chroma_mod._screen_mask(frame, {
        "preset": "green",
        "hue_range": [35, 85],
        "saturation_range": [40, 255],
        "value_range": [40, 255],
        "despill": True,
    })
    assert not (a_custom.mean() == a_green.mean() == 255.0)
    print("test_spooky_ghost_sidecar: PASS")


if __name__ == "__main__":
    test_spooky_ghost_sidecar()
    print("All daemon-sidecar chroma tests passed.")
