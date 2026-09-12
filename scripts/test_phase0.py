"""Phase 0 verification: config loader and main stub.

Run:  python scripts/test_phase0.py
Exits 0 if all checks pass.

Covered (PLAN.md Phase 0 verify):
- missing config file -> defaults
- valid config -> merged values win
- malformed JSON -> warning + defaults (no crash)
- invalid types -> per-key default fallback (no crash)
- save_config round-trip
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

# Make the package importable from the repo root.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from dyst import config as cfg  # noqa: E402


def _write(tmpdir: str, name: str, data) -> str:
    path = os.path.join(tmpdir, name)
    with open(path, "w", encoding="utf-8") as fh:
        if isinstance(data, str):
            fh.write(data)
        else:
            json.dump(data, fh, indent=2)
    return path


def main() -> int:
    failures = []
    # load_config always resolves a relative media_folder to an absolute path
    # against the app/exe dir (so config.json + media/ stay beside the exe).
    _resolved_defaults = {
        **cfg.DEFAULTS,
        "media_folder": os.path.abspath(
            os.path.join(cfg.get_base_dir(), cfg.DEFAULTS["media_folder"])
        ),
    }
    with tempfile.TemporaryDirectory() as tmp:
        # 1. Missing file -> defaults (with media_folder resolved)
        c = cfg.load_config(os.path.join(tmp, "missing.json"))
        assert c == _resolved_defaults, f"missing file: expected defaults, got {c}"
        print("PASS missing config -> defaults")

        # 2. Valid config -> user values win
        user = {
            "tick_seconds": 5.0,
            "odds": 50,
            "max_concurrent": 0,
            "chroma_key": {"enabled": False, "hue_range": [40, 80]},
            "volume": 0.5,
        }
        p = _write(tmp, "valid.json", user)
        c = cfg.load_config(p)
        assert c["tick_seconds"] == 5.0, c
        assert c["odds"] == 50, c
        assert c["max_concurrent"] == 0, c
        assert c["volume"] == 0.5, c
        assert c["reroll_in_same_tick"] is True, c  # untouched -> default
        assert c["chroma_key"]["enabled"] is False, c
        assert c["chroma_key"]["hue_range"] == [40, 80], c
        assert c["chroma_key"]["saturation_range"] == [40, 255], c
        print("PASS valid config -> merged")

        # 3. Malformed JSON -> defaults, no crash
        p = _write(tmp, "bad.json", "{ this is not json ")
        c = cfg.load_config(p)
        assert c == _resolved_defaults, c
        print("PASS malformed JSON -> defaults")

        # 4. Root not an object -> defaults
        p = _write(tmp, "root.json", [1, 2, 3])
        c = cfg.load_config(p)
        assert c == _resolved_defaults, c
        print("PASS non-object root -> defaults")

        # 5. Invalid values -> per-key default fallback
        bad = {
            "tick_seconds": -3,          # <= 0
            "odds": "abc",               # non-numeric
            "volume": 5,           # > 1
            "monitor": "ultrawide",      # not 'primary'/int
            "max_concurrent": 2.5,       # non-integer
            "chroma_key": {"hue_range": [400, 500]},  # out of range
            "fade_out_seconds": -1,      # negative
            "chroma_key": "not-a-dict",
        }
        p = _write(tmp, "badvals.json", bad)
        c = cfg.load_config(p)
        assert c["tick_seconds"] == cfg.DEFAULTS["tick_seconds"], c
        assert c["odds"] == cfg.DEFAULTS["odds"], c
        assert c["volume"] == cfg.DEFAULTS["volume"], c
        assert c["monitor"] == cfg.DEFAULTS["monitor"], c
        assert c["max_concurrent"] == cfg.DEFAULTS["max_concurrent"], c
        assert c["fade_out_seconds"] == cfg.DEFAULTS["fade_out_seconds"], c
        assert c["chroma_key"] == cfg.DEFAULTS["chroma_key"], c
        print("PASS invalid values -> defaults")

        # 5b. Chroma-key presets: named values fill in the ranges; explicit
        # per-key ranges still override a preset; unknown presets ignored.
        for name, (hr, sr, vr) in [
            ("green", ([35, 85], [40, 255], [40, 255])),
            ("weak green", ([28, 92], [20, 255], [30, 255])),
            ("strong green", ([42, 78], [60, 255], [50, 255])),
            ("blue", ([100, 130], [40, 255], [40, 255])),
            ("weak blue", ([90, 145], [20, 255], [30, 255])),
            ("strong blue", ([105, 130], [60, 255], [50, 255])),
        ]:
            c = cfg.load_config(_write(tmp, "preset.json", {"chroma_key": {"preset": name}}))
            ck = c["chroma_key"]
            assert ck["preset"] == name, ck
            assert ck["hue_range"] == hr, (name, ck["hue_range"])
            assert ck["saturation_range"] == sr, (name, ck["saturation_range"])
            assert ck["value_range"] == vr, (name, ck["value_range"])
        # case + whitespace insensitive
        c = cfg.load_config(_write(tmp, "preset2.json", {"chroma_key": {"preset": "  BLUE "}}))
        assert c["chroma_key"]["preset"] == "blue"
        assert c["chroma_key"]["hue_range"] == [100, 130]
        # explicit ranges override the preset per-key; other preset ranges stay
        c = cfg.load_config(_write(tmp, "preset3.json",
                                   {"chroma_key": {"preset": "green", "hue_range": [40, 80]}}))
        ck = c["chroma_key"]
        assert ck["preset"] == "green" and ck["hue_range"] == [40, 80]
        assert ck["saturation_range"] == [40, 255]
        # unknown preset -> warning + fall back to the default preset, no crash
        c = cfg.load_config(_write(tmp, "preset4.json", {"chroma_key": {"preset": "pink"}}))
        assert c["chroma_key"]["preset"] == cfg.DEFAULTS["chroma_key"]["preset"] == "green"
        assert c["chroma_key"]["hue_range"] == [35, 85]
        print("PASS chroma presets: green/weak/strong + blue, explicit ranges win, unknown ignored")

        # 6. save_config round-trip
        p = _write(tmp, "roundtrip.json", cfg.DEFAULTS)
        cfg.save_config(p, {**cfg.DEFAULTS, "odds": 7, "debug": True})
        with open(p, encoding="utf-8") as fh:
            again = json.load(fh)
        assert again["odds"] == 7 and again["debug"] is True, again
        print("PASS save_config round-trip")

    if failures:
        print(f"\n{len(failures)} FAILURE(S):")
        for f in failures:
            print(" -", f)
        return 1
    print("\nAll Phase 0 checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())