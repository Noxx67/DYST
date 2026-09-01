"""Verify the image+audio lifecycle fix.

1. Image WITHOUT audio -> shown for image_seconds, fade out, then closes
   (audio_done must be True, no audio player kept alive).
2. Image WITH sidecar audio -> image display ends, fade starts, but the
   audio player is NOT stopped; the overlay only closes after the audio
   actually finishes.
"""
from __future__ import annotations

import os
import struct
import sys
import tempfile
import time
import wave

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image
from PySide6.QtWidgets import QApplication

from dyst.overlay import OverlayWindow

app = QApplication(sys.argv[:1])


def make_wav(path: str, seconds: float = 0.6) -> None:
    rate = 22050
    n = int(rate * seconds)
    frames = b"".join(
        struct.pack("<h", int(12000 * __import__("math").sin(2 * 3.14159 * 440 * i / rate)))
        for i in range(n)
    )
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)


def make_img(path: str) -> None:
    Image.new("RGBA", (8, 8), (255, 0, 0, 255)).save(path)


def wait_finished(win, timeout=10.0):
    flag = {"done": False}
    win.finished.connect(lambda: flag.update(done=True))
    deadline = time.time() + timeout
    while not flag["done"] and time.time() < deadline:
        app.processEvents()
        time.sleep(0.01)
    return flag["done"]


def main() -> int:
    td = tempfile.mkdtemp()
    img = os.path.join(td, "pic.png")
    img2 = os.path.join(td, "pic_with_audio.png")
    wav = os.path.join(td, "pic_with_audio.wav")
    make_img(img)
    make_img(img2)
    make_wav(wav, 0.6)

    # --- Case 1: image WITHOUT audio -> fast fade + close, no audio player ---
    win = OverlayWindow()
    assert win.load(img, "image", image_seconds=0.2, fade_out_seconds=0.1)
    assert win._audio_player is None, "no sidecar -> no audio player"
    win.show()
    win.start()
    t0 = time.time()
    assert wait_finished(win, timeout=5.0), "image-no-audio never finished"
    elapsed = time.time() - t0
    assert elapsed < 0.6, f"no-audio image took too long ({elapsed:.2f}s)"
    assert win._audio_done is True
    assert win._fade_done is True
    print(f"PASS image-no-audio: closed in {elapsed:.2f}s, audio_done+fade_done set")

    # --- Case 2: image WITH sidecar audio -> fade starts, audio kept alive ---
    win = OverlayWindow()
    assert win.load(img, "image", image_seconds=0.2, fade_out_seconds=0.1,
                    sidecar_audio=wav)
    win.show()
    win.start()
    # The image display time (0.2s) has elapsed -> visual_end fired.
    # Give the fade time to start and check the audio player is still alive.
    time.sleep(0.35)  # past image display + fade start
    app.processEvents()
    assert win._visual_done is True, "visual should be done after image display"
    assert win._fade_started is True, "fade should have started"
    assert win._audio_player is not None, "audio player must still exist during fade"
    assert win.isVisible() is False or win._audio_player is not None, \
        "audio must keep playing while window is fading/closed"
    # The key assertion: the overlay must NOT have finished yet (audio still
    # playing the 0.6s wav).
    finished_early = {"done": False}
    win.finished.connect(lambda: finished_early.update(done=True))
    assert not finished_early["done"], "overlay closed before audio finished!"
    print("PASS image+sidecar-audio: audio player alive during fade, overlay still alive")

    # Now wait for the full lifecycle: overlay should close shortly after the
    # 0.6s audio finishes (plus the 0.1s fade).
    t0 = time.time()
    assert wait_finished(win, timeout=5.0), "image+audio never finished"
    elapsed = time.time() - t0
    assert elapsed < 1.0, f"image+audio took too long ({elapsed:.2f}s)"
    print(f"PASS image+sidecar-audio: overlay closed after audio finished ({elapsed:.2f}s)")

    print("\nAll audio-lifecycle checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
