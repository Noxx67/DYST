"""Quick sanity checks for the updated chroma key pipeline."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
from dyst import chroma as chroma_mod


def test_parse_params():
    base = {"preset": "custom", "despill": True}
    # Test Case 1: Custom Black Removal (sidecar-style top-level keys)
    p1 = chroma_mod._parse_params({
        **base,
        "chroma_hue_range": [0, 179],
        "chroma_saturation_range": [0, 255],
        "chroma_value_range": [0, 35],
    })
    assert p1["mode"] == "luma"
    assert p1["value_range"] == [0, 35]

    # Test Case 2: Custom Red Removal (resolved chroma dict-style nested keys)
    p2 = chroma_mod._parse_params({
        "preset": "custom",
        "hue_range": [0, 15],
        "saturation_range": [50, 255],
        "value_range": [40, 255],
        "despill": True,
    })
    assert p2["mode"] == "chroma"
    assert p2["hue_range"] == [0, 15]

    # Preset black
    p3 = chroma_mod._parse_params({"preset": "black", "despill": False})
    assert p3["mode"] == "luma"
    assert p3["value_range"] == [0, 35]

    # Preset white
    p4 = chroma_mod._parse_params({"preset": "white", "despill": True})
    assert p4["mode"] == "luma"
    assert p4["value_range"] == [220, 255]

    # Preset green
    p5 = chroma_mod._parse_params({"preset": "green", "despill": True})
    assert p5["mode"] == "chroma"
    assert p5["hue_range"] == [35, 85]

    # Unknown preset fallback
    p6 = chroma_mod._parse_params({"preset": "unknown", "despill": False})
    assert p6["mode"] == "chroma"
    assert p6["hue_range"] == [0, 179]

    print("test_parse_params: PASS")


def test_despill_covers_near_opaque_border():
    # Green spill on a fully/near-opaque subject edge must be neutralized too
    # (an earlier version only desaturated semi-transparent pixels, which left
    # green fringes on subject borders like the FNAF jumpscare).
    bgra = np.zeros((2, 2, 4), dtype=np.uint8)
    bgra[:, :] = (0, 200, 0, 255)  # BGR = pure green, alpha = opaque
    params = {"preset": "green", "hue_range": [35, 85],
              "saturation_range": [40, 255], "value_range": [40, 255],
              "despill": True}
    chroma_mod._despill(bgra, params)
    assert bgra[0, 0, 1] == 0, f"green not despilled on opaque pixel: {bgra[0, 0, 1]}"
    assert bgra[0, 0, 3] == 255, "alpha must be untouched"
    # luma (black/white) keying must NOT despill at all
    bgra2 = np.zeros((2, 2, 4), dtype=np.uint8)
    bgra2[:, :] = (0, 200, 0, 128)
    chroma_mod._despill(bgra2, {"preset": "black", "despill": True})
    assert bgra2[0, 0, 1] == 200, "luma keying should disable despill"
    print("test_despill_covers_near_opaque_border: PASS")


def test_mask_and_despill():
    # Black luma key on a dark image
    img = np.zeros((10, 10, 3), dtype=np.uint8)
    img[:, :] = (20, 20, 20)  # dark gray
    params = {
        "preset": "custom",
        "chroma_hue_range": [0, 179],
        "chroma_saturation_range": [0, 255],
        "chroma_value_range": [0, 35],
        "despill": True,
    }
    alpha = chroma_mod._screen_mask(img, params)
    assert alpha.dtype == np.uint8
    # Pixels should be mostly transparent (removed)
    mean_alpha = alpha.mean()
    assert mean_alpha < 128, f"Expected mostly transparent for dark pixels, got {mean_alpha}"

    # Red chroma key: background should be removed, subject kept
    img = np.zeros((10, 10, 3), dtype=np.uint8)
    img[:, :5] = (0, 0, 255)   # red background (BGR)
    img[:, 5:] = (255, 0, 0)   # blue subject (BGR)
    params = {
        "preset": "custom",
        "chroma_hue_range": [0, 15],
        "chroma_saturation_range": [50, 255],
        "chroma_value_range": [40, 255],
        "despill": True,
    }
    alpha = chroma_mod._screen_mask(img, params)
    left_mean = alpha[:, :5].mean()
    right_mean = alpha[:, 5:].mean()
    assert left_mean < 128, f"Expected transparent red background, got {left_mean}"
    assert right_mean > 128, f"Expected opaque blue subject, got {right_mean}"

    # Despill should run and not crash
    bgra = np.zeros((10, 10, 4), dtype=np.uint8)
    bgra[:, :] = (0, 0, 255, 128)
    chroma_mod._despill(bgra, params)
    assert bgra.dtype == np.uint8

    # White luma key on bright image
    img = np.full((10, 10, 3), (255, 255, 255), dtype=np.uint8)
    params = {
        "preset": "custom",
        "chroma_hue_range": [0, 179],
        "chroma_saturation_range": [0, 30],
        "chroma_value_range": [220, 255],
        "despill": True,
    }
    alpha = chroma_mod._screen_mask(img, params)
    mean_alpha = alpha.mean()
    assert mean_alpha < 128, f"Expected mostly transparent for white pixels, got {mean_alpha}"

    print("test_mask_and_despill: PASS")


if __name__ == "__main__":
    test_parse_params()
    test_despill_covers_near_opaque_border()
    test_mask_and_despill()
    print("All chroma luma tests passed.")
