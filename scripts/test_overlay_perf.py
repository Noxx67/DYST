"""Perf regression: the overlay window is sized to the media (not the whole
screen) and the frame is pre-scaled ONCE into a render cache that paintEvent
blits 1:1. This is what keeps long fades / many concurrent overlays from
re-scaling a full-screen translucent surface on every repaint."""
from __future__ import annotations

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRect  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from dyst.overlay import OverlayWindow  # noqa: E402

app = QApplication(sys.argv[:1])


def test_small_custom_window():
    w = OverlayWindow()
    w._screen_rect = QRect(0, 0, 1920, 1080)
    ok = w.load("media/images/trynna-ignore-it.jpg", "image", image_seconds=0.1,
                fade_out_seconds=0.05, mode="custom",
                custom={"scale_x": 0.25, "scale_y": 0.25,
                        "position_x": 0.5, "position_y": 0.5})
    assert ok
    assert w.width() * w.height() < 1920 * 1080 // 4, (w.width(), w.height())
    assert w._render_cache is not None
    assert (w._render_cache.width(), w._render_cache.height()) == (w.width(), w.height())
    w._finish_close()
    print("PASS small custom window is sized to the media")


def test_fit_uses_media_rect():
    w = OverlayWindow()
    w._screen_rect = QRect(0, 0, 1920, 1080)
    ok = w.load("media/images/folk.jpg", "image", image_seconds=0.1,
                fade_out_seconds=0.05, mode="fit")
    assert ok
    # tall image: fills the height but is narrower than the full screen
    assert w.height() <= 1080 and w.width() < 1920, (w.width(), w.height())
    assert w._render_cache is not None
    assert w._render_cache.width() == w.width()
    w._finish_close()
    print("PASS fit window is sized to the media rect")


def test_stretch_fills_screen():
    # stretch must distort the media to the screen aspect and cover it fully
    # (no letterbox bars). Regression for the per-axis crop bug.
    w = OverlayWindow()
    w._screen_rect = QRect(0, 0, 1920, 1080)
    ok = w.load("media/images/folk.jpg", "image", image_seconds=0.1,
                fade_out_seconds=0.05, mode="stretch")
    assert ok
    assert (w.width(), w.height()) == (1920, 1080), (w.width(), w.height())
    assert (w._render_cache.width(), w._render_cache.height()) == (1920, 1080)
    w.show()
    shot = w.grab().toImage()
    # sample the full surface including corners: all must be opaque media
    for px, py in ((0, 0), (1919, 0), (0, 1079), (1919, 1079),
                   (960, 540), (10, 1000), (1900, 50)):
        assert shot.pixelColor(px, py).alpha() >= 250, (px, py)
    w._finish_close()
    print("PASS stretch distorts media to fill the whole screen")


def test_nonuniform_custom_scale_keeps_full_image():
    # scale_x/scale_y differ -> display aspect != source aspect. The source
    # must still be mapped across both axes (not cropped).
    w = OverlayWindow()
    w._screen_rect = QRect(0, 0, 1920, 1080)
    ok = w.load("media/images/folk.jpg", "image", image_seconds=0.1,
                fade_out_seconds=0.05, mode="custom",
                custom={"scale_x": 0.3, "scale_y": 0.6,
                        "position_x": 0.5, "position_y": 0.5})
    assert ok
    # 735x685 fit to 1920x1080 -> 1.576x -> *0.3 / *0.6
    assert (w.width(), w.height()) == (348, 648), (w.width(), w.height())
    assert (w._render_cache.width(), w._render_cache.height()) == (348, 648)
    w._finish_close()
    print("PASS non-uniform custom scale keeps the full image")


def test_render_cache_refreshes_on_frame_change():
    w = OverlayWindow()
    w._screen_rect = QRect(0, 0, 1920, 1080)
    ok = w.load("media/gifs/skeleton-running.gif", "image", image_seconds=1.0,
                fade_out_seconds=0.05, mode="fit")
    assert ok
    before = w._render_cache
    w._advance_gif_frame()
    assert w._render_cache is not before  # new frame -> cache rebuilt
    assert w._render_cache is not None
    w._finish_close()
    print("PASS render cache refreshes per new frame")


if __name__ == "__main__":
    test_small_custom_window()
    test_fit_uses_media_rect()
    test_stretch_fills_screen()
    test_nonuniform_custom_scale_keeps_full_image()
    test_render_cache_refreshes_on_frame_change()
    print("All overlay perf tests passed.")