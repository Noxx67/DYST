"""Generate test media for DYST development (Phase 1a).

Writes into the media folder:
  media/images/test_scare.png   - transparent PNG with a red square
  media/videos/test_scare.mp4   - 1s, 320x180, solid GREEN background with a
                                  moving red square (green is for the later
                                  chroma-key test)

Usage: python scripts/make_test_asset.py [media_root]
"""

from __future__ import annotations

import os
import sys

import cv2
import numpy as np
from PIL import Image, ImageDraw

W, H = 320, 180
FPS = 30
FRAMES = 30  # ~1 second


def make_image(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rectangle((100, 30, 220, 150), fill=(255, 0, 0, 255))
    img.save(path)
    print(f"wrote {path}")


def make_video(path: str) -> bool:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    out = cv2.VideoWriter(path, fourcc, FPS, (W, H))
    if not out.isOpened():
        print(f"WARNING: could not open VideoWriter for {path} "
              "(codec unavailable) - video test skipped")
        return False
    for i in range(FRAMES):
        frame = np.zeros((H, W, 3), np.uint8)
        frame[:, :] = (0, 255, 0)  # green background (BGR)
        x = int(10 + 300 * i / (FRAMES - 1))
        cv2.rectangle(frame, (x, 70), (x + 60, 110), (0, 0, 255), -1)  # red square
        out.write(frame)
    out.release()
    print(f"wrote {path}")
    return True


def main() -> int:
    root = sys.argv[1] if len(sys.argv) > 1 else "media"
    make_image(os.path.join(root, "images", "test_scare.png"))
    make_video(os.path.join(root, "videos", "test_scare.mp4"))
    print("done. Run: python main.py --test")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())