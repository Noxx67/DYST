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

    img_item = media.MediaItem(
        os.path.join(ROOT, "media", "images", "test_scare.png"), "image")
    # Use the deterministic generated test clip (not the first video in the
    # pool, which is now a downloaded AV1 file — slower and non-deterministic).
    vid_item = media.MediaItem(
        os.path.join(ROOT, "media", "videos", "test_scare.mp4"), "video")

    # 2. Image overlay
    win = OverlayWindow()
    assert win.load(img_item.path, "image", image_seconds=0.2, fade_out_seconds=0.1)
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
    assert win.load(vid_item.path, "video", fade_out_seconds=0.1)
    win.show()
    win.start()
    t0 = time.time()
    assert wait_finished(win, timeout=10.0), "video overlay never finished"
    elapsed = time.time() - t0
    assert not win.isVisible(), "video overlay still visible after finish"
    print(f"PASS video overlay: played to end + faded (%.1fs)" % elapsed)

    # 3b. Cover modes (morestufftoadd feature 1): old "cover" is gone,
    # replaced by cover-height (fit screen horizontally) / cover-width (fit
    # screen vertically); stretch and fit unchanged. Each mode must load,
    # show and render offscreen without crashing; invalid modes fall back to fit.
    assert media.VALID_MODES == {"fit", "cover-height", "cover-width", "stretch", "custom"}, media.VALID_MODES
    print("PASS cover modes: %s" % sorted(media.VALID_MODES))
    for mode in ("fit", "stretch", "cover-height", "cover-width"):
        w = OverlayWindow()
        assert w.load(img_item.path, "image", image_seconds=0.2,
                      fade_out_seconds=0.05, mode=mode), f"load failed for mode {mode}"
        assert w._mode == mode, f"mode not honoured: {w._mode!r}"
        w.show()
        shot = w.grab()
        assert not shot.isNull(), f"grab() returned null for mode {mode}"
        w._finish_close()
    print("PASS cover modes: all 4 render offscreen")
    w = OverlayWindow()
    assert w.load(img_item.path, "image", image_seconds=0.2, fade_out_seconds=0.05, mode="cover")
    assert w._mode == "fit", f"removed 'cover' should fall back to fit, got {w._mode!r}"
    w._finish_close()
    print("PASS removed 'cover' falls back to fit")

    # 3c. Custom mode (morestufftoadd feature 2): position/scale/flip/rotate.
    # Scale is relative to the aspect-preserving fit size: window 1000x500,
    # image 100x100 -> fit = 5 -> disp 500x500 at (1,1). scale_x=0.5 ->
    # disp_w=250; position_x=0 pins the left edge at screen left (x=0),
    # position_x=1 pins the right edge at screen right (x=750). scale_y=2
    # -> disp_h=1000 (overflows, but position still centers/crops).
    assert "custom" in media.VALID_MODES, media.VALID_MODES
    print("PASS custom mode recognised")
    w = OverlayWindow()
    w.setGeometry(0, 0, 1000, 500)
    assert w.load(img_item.path, "image", image_seconds=0.2, fade_out_seconds=0.05,
                  mode="custom", custom={"position_x": 0.0, "position_y": 0.0,
                                         "scale_x": 0.5, "scale_y": 1.0})
    assert w._mode == "custom"
    x, y, dw, dh = w._custom_target(100, 100)
    assert (dw, dh) == (250.0, 500.0), (dw, dh)
    assert (x, y) == (0.0, 0.0), (x, y)  # 0/0 pins top-left
    w._position_x, w._position_y = 1.0, 1.0
    x, y, dw, dh = w._custom_target(100, 100)
    assert x == 750.0 and y == 0.0, (x, y)  # right edge at screen right
    # Positions beyond 0..1 are allowed (clamped to -1..2) so media can
    # peek in / be cropped at the screen edges. Add vertical slack
    # (scale_y=0.5 -> disp_h=250) so y can actually move in a 500px window.
    w._scale_y = 0.5
    w._position_x, w._position_y = 1.5, 2.0
    x, y, dw, dh = w._custom_target(100, 100)
    assert (dw, dh) == (250.0, 250.0), (dw, dh)
    assert x == 1125.0, x  # (1000-250) * 1.5: pokes past the right edge (cropped)
    assert y == 500.0, y  # (500-250) * 2: fully below the screen bottom
    w._position_x = -1.0
    x, _, _, _ = w._custom_target(100, 100)
    assert x == -750.0, x  # pushed all the way off-screen left
    w._finish_close()
    print("PASS custom position range -1..2 (peek/crop off-screen)")

    w = OverlayWindow()
    w.setGeometry(0, 0, 1000, 500)
    assert w.load(img_item.path, "image", image_seconds=0.2, fade_out_seconds=0.05,
                  mode="custom", custom={"position_x": -1.5, "position_y": 3.0})
    assert w._position_x == -1.0 and w._position_y == 2.0, (w._position_x, w._position_y)  # clamped
    w._finish_close()
    print("PASS custom position clamp to [-1, 2]")
    # Flip + rotation render without crashing, and the result isn't fully
    # transparent (the red square must still land somewhere on the widget).
    w = OverlayWindow()
    w.setGeometry(0, 0, 400, 400)
    assert w.load(img_item.path, "image", image_seconds=0.2, fade_out_seconds=0.05,
                  mode="custom", custom={"flip_h": True, "flip_v": True,
                                         "rotation": 45.0, "scale_x": 0.5,
                                         "scale_y": 0.5})
    w.show()
    shot = w.grab()
    img = shot.toImage()
    assert not img.isNull()
    red = False
    for px in range(0, 400, 8):
        for py in range(0, 400, 8):
            c = img.pixelColor(px, py)
            if c.red() > 200 and c.green() < 100 and c.blue() < 100:
                red = True
                break
        if red:
            break
    assert red, "no red pixels rendered in custom mode (flip+rotate)"
    w._finish_close()
    print("PASS custom mode renders (flip H/V + rotation 45deg, red visible)")

    # 3d. max_duration (morestufftoadd feature 3): a hard cap on the whole
    # overlay that CLOSES INSTANTLY with NO fade-out. An image set to display
    # 10s with fade 1.0 but capped at 0.3s must finish in ~0.3s (if it faded,
    # it would take ~1.3s); the same with a 5.4s sidecar mp3 must STILL finish
    # fast because the audio is force-stopped too.
    w = OverlayWindow()
    assert w.load(img_item.path, "image", image_seconds=10.0, fade_out_seconds=1.0,
                  max_duration=0.3)
    w.show(); w.start()
    t0 = time.time()
    assert wait_finished(w, timeout=5.0), "max_duration image never finished"
    wall = time.time() - t0
    assert wall < 1.0, f"max_duration should close instantly (no fade): {wall:.2f}s"
    w = OverlayWindow()
    assert w.load(img_item.path, "image", image_seconds=10.0, fade_out_seconds=1.0,
                  sidecar_audio=os.path.join(ROOT, "media", "images", "bear5.mp3"),
                  max_duration=0.3)
    w.show(); w.start()
    t0 = time.time()
    assert wait_finished(w, timeout=5.0), "max_duration image+sidecar never finished"
    wall = time.time() - t0
    assert wall < 1.0, f"max_duration did not stop the sidecar audio instantly: {wall:.2f}s"
    print("PASS max_duration closes instantly (no fade, audio force-stopped)")
    # Video: a 1s clip capped at 0.3s with fade 1.0 must finish early (instantly).
    w = OverlayWindow()
    assert w.load(vid_item.path, "video", fade_out_seconds=1.0, max_duration=0.3)
    w.show(); w.start()
    t0 = time.time()
    assert wait_finished(w, timeout=5.0), "max_duration video never finished"
    wall = time.time() - t0
    assert wall < 1.0, f"max_duration did not cap the video instantly: {wall:.2f}s"
    print("PASS max_duration caps video early, no fade")

    # 3e. speed (morestufftoadd feature 4): a 1s video at 2x finishes in
    # ~0.5s; at 0.5x it takes ~2s. Gifs/audio speed via the same path.
    w = OverlayWindow()
    assert w.load(vid_item.path, "video", fade_out_seconds=0.0, speed=2.0)
    w.show(); w.start()
    t0 = time.time()
    assert wait_finished(w, timeout=5.0), "2x video never finished"
    wall = time.time() - t0
    assert wall < 1.0, f"2x video should finish in ~0.5s, took {wall:.2f}s"
    w = OverlayWindow()
    assert w.load(vid_item.path, "video", fade_out_seconds=0.0, speed=0.5)
    w.show(); w.start()
    t0 = time.time()
    assert wait_finished(w, timeout=5.0), "0.5x video never finished"
    wall = time.time() - t0
    assert wall >= 1.5, f"0.5x video should take ~2s, finished in {wall:.2f}s"
    print("PASS speed: video at 2x finishes ~0.5s, at 0.5x ~2s")

    # 3f. speed/pitch audio baking: with both == 1 the original file is
    # returned untouched; with pitch != 1 a temp file is produced when
    # ffmpeg is available (and the bake keeps the same duration).
    from dyst import ffmpeg_util
    w = OverlayWindow()
    assert w.load(img_item.path, "image", image_seconds=1.0, fade_out_seconds=0.0)
    src = os.path.join(ROOT, "media", "images", "bear5.mp3")
    assert w._prepare_audio_file(src) == src, "speed/pitch 1/1 must return the original"
    if ffmpeg_util.find_ffmpeg() is not None:
        w2 = OverlayWindow()
        assert w2.load(img_item.path, "image", image_seconds=1.0, fade_out_seconds=0.0,
                       speed=1.5, pitch=2.0)
        baked = w2._prepare_audio_file(src)
        assert baked != src and os.path.isfile(baked), f"bake failed: {baked}"
        assert baked in w2._temp_files, "baked temp not tracked for cleanup"
        print("PASS pitch+speed bake: temp file produced, tracked for cleanup")
    else:
        print("SKIP pitch bake check: ffmpeg not available")

    # 3g. speed also scales IMAGE display time and fades (1/speed).
    w = OverlayWindow()
    assert w.load(img_item.path, "image", image_seconds=1.0, fade_out_seconds=0.0, speed=2.0)
    w.show(); w.start()
    t0 = time.time()
    assert wait_finished(w, timeout=5.0), "2x image never finished"
    wall = time.time() - t0
    assert wall < 0.8, f"2x image should display ~0.5s, took {wall:.2f}s"
    # fade also scales: 1s hold + 1s fade at 2x -> ~0.5 + 0.5 = ~1.0s (unscaled 2s).
    w = OverlayWindow()
    assert w.load(img_item.path, "image", image_seconds=1.0, fade_out_seconds=1.0, speed=2.0)
    w.show(); w.start()
    t0 = time.time()
    assert wait_finished(w, timeout=5.0), "2x image+fade never finished"
    wall = time.time() - t0
    assert wall < 1.5, f"2x image+fade should be ~1.0s, took {wall:.2f}s"
    print("PASS speed scales image display time and fade-out")

    # 3h. speed_pitch (combined) overrides speed/pitch. Priority: per-file
    # speed_pitch > global speed_pitch > per-file speed/pitch > global.
    import main as main_mod
    r = main_mod.resolve_speed_pitch
    assert r({}, {"speed_pitch": 2.0, "speed": 1.0, "pitch": 1.0}) == (2.0, 2.0)
    assert r({"speed_pitch": 3.0}, {"speed_pitch": 2.0, "speed": 1.0, "pitch": 1.0}) == (3.0, 3.0)
    # global speed_pitch overrides per-file individual speed/pitch.
    assert r({"speed": 1.5}, {"speed_pitch": 2.0, "speed": 1.0, "pitch": 1.0}) == (2.0, 2.0)
    # no speed_pitch anywhere -> per-file speed/pitch override global.
    assert r({"speed": 1.5, "pitch": 0.5}, {"speed": 1.0, "pitch": 1.0}) == (1.5, 0.5)
    print("PASS speed_pitch overrides speed/pitch (per-file > global)")

    # 3i. max_duration x speed race: max firing mid-fade must not close the
    # overlay twice (natural end starts a fade; max force-closes instantly).
    w = OverlayWindow()
    assert w.load(img_item.path, "image", image_seconds=10.0, fade_out_seconds=1.0,
                  max_duration=0.3, speed=2.0)
    w.show()
    count = {"n": 0}
    w.finished.connect(lambda: count.update(n=count["n"] + 1))
    w._start_fade()        # fade begins (natural end path)
    w._on_max_duration()   # max fires DURING the fade
    w._fade_finished()     # fade animation finishes afterwards
    app.processEvents()
    assert count["n"] == 1, f"double close on max-mid-fade race ({count['n']} emits)"
    print("PASS one-shot close guard (max_duration mid-fade race emits finished once)")

    # 3j. fade_in (morestufftoadd feature 5): opacity 0->1 BEFORE the display
    # clock, so lifetime = fade_in + display + fade_out. An image with
    # fade_in=0.4, display=0.2, fade_out=0 must finish ~0.6s (unscaled 0.2s
    # without fade-in), and mid-fade the window must be semi-transparent.
    w = OverlayWindow()
    assert w.load(img_item.path, "image", image_seconds=0.2, fade_out_seconds=0.0,
                  fade_in_seconds=0.4)
    w.show()
    assert w.windowOpacity() == 0.0, "overlay must start fully transparent"
    w.start()
    t0 = time.time()
    deadline = t0 + 0.15
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.01)
    mid = w.windowOpacity()
    assert 0.0 < mid < 0.9, f"fade-in should be mid-animation, opacity={mid}"
    t_end = time.time()
    assert wait_finished(w, timeout=5.0), "fade-in image never finished"
    wall = time.time() - t0
    assert wall >= 0.5, f"fade-in should delay the end (~0.6s total): {wall:.2f}s"
    assert wall < 2.0, f"fade-in image took too long: {wall:.2f}s"
    print(f"PASS fade-in: opacity animates (mid={mid:.2f}), lifetime ~0.6s")
    # Fade-in scales with speed: 0.4s fade-in at 2x -> ~0.2s; total ~0.4s.
    w = OverlayWindow()
    assert w.load(img_item.path, "image", image_seconds=0.2, fade_out_seconds=0.0,
                  fade_in_seconds=0.4, speed=2.0)
    w.show(); w.start()
    t0 = time.time()
    assert wait_finished(w, timeout=5.0), "2x fade-in image never finished"
    wall = time.time() - t0
    assert wall < 0.55, f"2x fade-in total should be ~0.3s, took {wall:.2f}s"
    print("PASS fade-in scales with speed (0.4s fade-in at 2x)")

    # 3k. opacity (morestufftoadd feature): the base window opacity, with
    # fades composing on top (fade-in 0 -> opacity, fade-out opacity -> 0).
    w = OverlayWindow()
    assert w.load(img_item.path, "image", image_seconds=0.2, fade_out_seconds=0.0,
                  opacity=0.5)
    assert w._opacity == 0.5
    w.show(); w.start()
    app.processEvents()
    assert abs(w.windowOpacity() - 0.5) < 0.01, w.windowOpacity()
    w._finish_close()
    print("PASS opacity: image window sits at 0.5 with no fades")
    # Videos get it too (set synchronously in start()).
    w = OverlayWindow()
    assert w.load(vid_item.path, "video-qt", fade_out_seconds=0.0, opacity=0.4)
    w.show(); w.start()
    app.processEvents()
    assert abs(w.windowOpacity() - 0.4) < 0.01, w.windowOpacity()
    w._finish_close()
    print("PASS opacity: video window at 0.4")
    # Fade-in composes: ends AT the base opacity, not 1.0.
    w = OverlayWindow()
    assert w.load(img_item.path, "image", image_seconds=0.2, fade_out_seconds=0.05,
                  fade_in_seconds=0.2, opacity=0.6)
    w.show(); w.start()
    assert w._fade_in.endValue() == 0.6, w._fade_in.endValue()
    # let the fade-in finish, then check the fade-out starts from 0.6
    t0 = time.time()
    while time.time() - t0 < 1.0 and w.windowOpacity() < 0.59:
        app.processEvents(); time.sleep(0.01)
    assert abs(w.windowOpacity() - 0.6) < 0.05, w.windowOpacity()
    w._start_fade()
    assert w._fade.startValue() == 0.6, w._fade.startValue()
    w._finish_close()
    print("PASS opacity composes with fades (fade-in ends at 0.6, fade-out starts at 0.6)")

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