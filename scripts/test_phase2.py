"""Phase 2 verification: validation + sidecar pairing + sidecar playback.

Run:  python scripts/test_phase2.py
Exits 0 if all checks pass.

Covered (PLAN.md Phase 2 verify):
- corrupt image / corrupt video are EXCLUDED from the pool (with warnings)
- sidecar pairing: same base name, same folder; .mp3 > .wav precedence;
  case-insensitive match
- settings still attach (regression)
- image + sidecar: overlay loads and finishes; audio player active
- video-av1 + sidecar: sidecar used as audio source, NO temp file created
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

from PySide6.QtWidgets import QApplication  # noqa: E402

from dyst import media  # noqa: E402
from dyst.overlay import OverlayWindow  # noqa: E402

app = QApplication(sys.argv[:1])


def make_wav(path: str, seconds: float = 0.3) -> None:
    """Tiny valid wav (440 Hz sine) so QMediaPlayer can actually play it."""
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


def make_test_video(path: str) -> bool:
    import cv2
    import numpy as np

    out = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), 10, (64, 48))
    if not out.isOpened():
        return False
    for i in range(10):
        frame = np.zeros((48, 64, 3), np.uint8)
        frame[:, :] = (i * 20, 128, 255 - i * 20)
        out.write(frame)
    out.release()
    return True


def wait_finished(win: OverlayWindow, timeout: float = 10.0) -> bool:
    flag = {"done": False}
    win.finished.connect(lambda: flag.update(done=True))
    deadline = time.time() + timeout
    while not flag["done"] and time.time() < deadline:
        app.processEvents()
        time.sleep(0.02)
    return flag["done"]


def main() -> int:
    td = tempfile.mkdtemp()
    imgs = os.path.join(td, "images")
    vids = os.path.join(td, "videos")
    os.makedirs(imgs)
    os.makedirs(vids)

    from PIL import Image

    # valid + corrupt images
    Image.new("RGBA", (64, 48), (0, 255, 0, 255)).save(os.path.join(imgs, "good.png"))
    Image.new("RGBA", (64, 48), (255, 0, 0, 255)).save(os.path.join(imgs, "UPPER.PNG"))
    with open(os.path.join(imgs, "broken.png"), "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\nthis is not really a png at all")
    # valid + corrupt videos
    assert make_test_video(os.path.join(vids, "good.mp4")), "test video generation failed"
    with open(os.path.join(vids, "broken.mp4"), "wb") as f:
        f.write(b"\x00\x00\x00\x18ftypisomgarbage-not-a-video")
    # sidecars: good.png gets .wav; UPPER.PNG gets .mp3; BOTH extensions for c.png
    make_wav(os.path.join(imgs, "good.wav"))
    with open(os.path.join(imgs, "upper.mp3"), "wb") as f:
        f.write(b"ID3 fake mp3 bytes")  # pairing test only
    Image.new("RGBA", (32, 32), (0, 0, 255, 255)).save(os.path.join(imgs, "c.png"))
    make_wav(os.path.join(imgs, "c.wav"))
    with open(os.path.join(imgs, "c.mp3"), "wb") as f:
        f.write(b"ID3 fake mp3 bytes")
    # settings sidecar still works
    with open(os.path.join(imgs, "good.json"), "w") as f:
        f.write('{"mode": "cover", "duration": 2}')

    pool = media.scan(td)
    names = {os.path.basename(i.path) for i in pool}
    print("pool:", sorted(names))

    # 1. corrupt files excluded
    assert "broken.png" not in names, "corrupt image should be skipped"
    assert "broken.mp4" not in names, "corrupt video should be skipped"
    assert "good.png" in names and "good.mp4" in names
    print("PASS corrupt image+video excluded from pool")

    # 2. sidecar pairing + precedence
    by_name = {os.path.basename(i.path): i for i in pool}
    assert by_name["good.png"].sidecar_audio.endswith("good.wav")
    assert by_name["UPPER.PNG"].sidecar_audio.endswith("upper.mp3"), \
        "case-insensitive sidecar match failed"
    assert by_name["c.png"].sidecar_audio.endswith("c.mp3"), \
        ".mp3 should win over .wav (AUDIO_EXTS order)"
    assert by_name["good.mp4"].sidecar_audio is None
    print("PASS sidecar pairing (same folder, case-insensitive, .mp3>.wav precedence)")

    # 3. settings attach (regression)
    assert by_name["good.png"].settings.get("mode") == "cover"
    assert by_name["good.png"].settings.get("duration") == 2
    print("PASS settings sidecar still attaches")

    # 4. image + sidecar: overlay plays with audio player and finishes
    win = OverlayWindow()
    item = by_name["good.png"]
    assert win.load(item.path, "image", image_seconds=0.5, fade_seconds=0.1,
                    sidecar_audio=item.sidecar_audio)
    assert win._audio_player is not None, "sidecar audio player missing for image"
    win.show()
    win.start()
    assert wait_finished(win), "image+sidecar overlay never finished"
    print("PASS image + sidecar audio overlay loads, plays, finishes")

    # 5. video-av1 + sidecar: sidecar is the audio source, no temp extraction
    vid_item = by_name["good.mp4"]
    win = OverlayWindow()
    assert win.load(vid_item.path, "video-av1", fade_seconds=0.1,
                    sidecar_audio=os.path.join(imgs, "good.wav"))
    assert win._audio_player is not None
    assert win._temp_audio is None, "sidecar should avoid temp extraction"
    win.show()
    win.start()
    assert wait_finished(win), "av1+sidecar overlay never finished"
    print("PASS video-av1 + sidecar: sidecar used as audio, no temp file")

    print("\nAll Phase 2 checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())