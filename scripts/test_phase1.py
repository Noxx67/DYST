"""Phase 1 verification: overlay playback + ticker rules (offscreen).

Run:  python scripts/test_phase1.py
Exits 0 if all checks pass. Generates test assets first.

Covered (PLAN.md Phase 1/1a):
- media scan finds generated image + video; kinds correct
- image overlay: transparent window flags, loads, shows, fades, emits finished
- video overlay: frame loop runs to end, then finished
- ticker: odds=1 + cap=3 -> exactly 3 spawns in one tick (burst);
  huge odds -> no crash/no spawns; spawn-refused stops the burst
"""

from __future__ import annotations

import importlib.util
import os
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from dyst import config as cfg, media  # noqa: E402
from dyst.overlay import OverlayWindow  # noqa: E402
from dyst.ticker import Ticker  # noqa: E402

# Load scripts/make_test_asset without requiring scripts to be a package.
_spec = importlib.util.spec_from_file_location(
    "mta", os.path.join(ROOT, "scripts", "make_test_asset.py"))
mta = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mta)

app = QApplication(sys.argv[:1])


def wait_finished(win: OverlayWindow, timeout: float = 10.0) -> bool:
    flag = {"done": False}
    win.finished.connect(lambda: flag.update(done=True))
    deadline = time.time() + timeout
    while not flag["done"] and time.time() < deadline:
        app.processEvents()
        time.sleep(0.01)
    return flag["done"]


def main() -> int:
    failures = []

    # 1. Test assets + scan
    mta.make_image(os.path.join(ROOT, "media", "images", "test_scare.png"))
    mta.make_video(os.path.join(ROOT, "media", "videos", "test_scare.mp4"))
    pool = media.scan(os.path.join(ROOT, "media"))
    kinds = {i.kind for i in pool}
    assert "image" in kinds and "video" in kinds, f"scan pool missing kinds: {pool}"
    print(f"PASS media scan -> {len(pool)} items (kinds: {sorted(kinds)})")

    img_item = next(i for i in pool if i.kind == "image")
    # Use the deterministic generated test clip (not the first video in the
    # pool, which is now a downloaded AV1 file — slower and non-deterministic).
    vid_item = media.MediaItem(
        os.path.join(ROOT, "media", "videos", "test_scare.mp4"), "video")

    # 2. Image overlay
    win = OverlayWindow()
    assert win.load(img_item.path, "image", image_seconds=0.2, fade_seconds=0.1)
    flags = win.windowFlags()
    assert flags & Qt.FramelessWindowHint, "missing FramelessWindowHint"
    assert flags & Qt.WindowStaysOnTopHint, "missing WindowStaysOnTopHint"
    assert flags & Qt.Tool, "missing Tool (taskbar/alt-tab)"
    assert win.testAttribute(Qt.WA_TranslucentBackground)
    assert win.testAttribute(Qt.WA_ShowWithoutActivating)
    assert win.testAttribute(Qt.WA_TransparentForMouseEvents)
    print("PASS image overlay flags (translucent/topmost/tool/click-through no-focus)")
    win.show()
    win.start()
    assert wait_finished(win, timeout=5.0), "image overlay never finished"
    assert not win.isVisible(), "image overlay still visible after finish"
    print("PASS image overlay: shown -> auto-dismissed (%.2fs)" % 0.2)

    # 3. Video overlay (frame loop to end)
    win = OverlayWindow()
    assert win.load(vid_item.path, "video", fade_seconds=0.1)
    win.show()
    win.start()
    t0 = time.time()
    assert wait_finished(win, timeout=10.0), "video overlay never finished"
    elapsed = time.time() - t0
    assert not win.isVisible(), "video overlay still visible after finish"
    print(f"PASS video overlay: played to end + faded (%.1fs)" % elapsed)

    # 4. Ticker: burst capped at max_concurrent
    calls = []
    pick = lambda: media.MediaItem("fake.png", "image")
    spawn = lambda it: (calls.append(it), True)[1]
    t = Ticker(pick, spawn, {**cfg.DEFAULTS, "odds": 1, "max_concurrent": 3})
    t.tick()
    assert len(calls) == 3, f"expected 3 spawns in burst, got {len(calls)}"
    print("PASS ticker burst: odds=1, cap=3 -> 3 spawns in one tick")

    # 5. Ticker: huge odds -> nothing spawned, no crash
    calls = []
    t = Ticker(pick, spawn, {**cfg.DEFAULTS, "odds": 10**9})
    t.tick()
    assert len(calls) == 0, f"expected 0 spawns, got {len(calls)}"
    print("PASS ticker: odds=1e9 -> no spawns")

    # 6. Ticker: spawner refusal stops burst
    calls = []
    spawn_limited = lambda it: (calls.append(it) or False)
    t = Ticker(pick, spawn_limited, {**cfg.DEFAULTS, "odds": 1, "max_concurrent": 10})
    t.tick()
    assert len(calls) == 1, f"expected burst to stop after refusal, got {len(calls)}"
    print("PASS ticker: spawn refused -> burst stops")

    if failures:
        print(f"\n{len(failures)} FAILURE(S):")
        for f in failures:
            print(" -", f)
        return 1
    print("\nAll Phase 1 checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())