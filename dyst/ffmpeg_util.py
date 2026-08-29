"""DYST (did you see that? 👀) — ffmpeg helpers (AV1 audio extraction).

AV1 videos can't be hardware-decoded on all platforms (QtMultimedia spams
stderr and shows no video). DYST's AV1 strategy: decode video frames via
OpenCV (software) and play the audio from a temp file extracted here with
ffmpeg. This module finds ffmpeg and does the extraction.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile

log = logging.getLogger("dyst.ffmpeg_util")


def find_ffmpeg() -> str | None:
    """Locate ffmpeg: PATH first, then well-known WinGet install dirs
    (WinGet only updates PATH for NEW shells)."""
    found = shutil.which("ffmpeg")
    if found:
        return found

    home = os.path.expanduser("~")
    candidates = [
        os.path.join(home, "AppData", "Local", "Microsoft", "WinGet", "Links", "ffmpeg.exe"),
    ]
    packages_dir = os.path.join(home, "AppData", "Local", "Microsoft", "WinGet", "Packages")
    try:
        if os.path.isdir(packages_dir):
            for entry in os.listdir(packages_dir):
                if "ffmpeg" in entry.lower():
                    candidates.append(os.path.join(packages_dir, entry))
    except OSError:
        pass
    for cand in candidates:
        if os.path.isfile(cand):
            return cand
    return None


def extract_audio(video_path: str) -> str | None:
    """Extract the audio track of *video_path* to a small temp .m4a (AAC).

    Returns the temp file path, or None if ffmpeg is missing / extraction
    failed. The caller is responsible for deleting the temp file.
    """
    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        log.warning("ffmpeg not found — AV1 audio disabled (video will be silent)")
        return None

    tmp = tempfile.mktemp(suffix=".m4a", prefix="dyst_audio_")
    cmd = [ffmpeg, "-y", "-v", "error", "-i", video_path,
           "-vn", "-c:a", "aac", "-b:a", "160k", tmp]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not os.path.isfile(tmp):
        log.warning("audio extraction failed for %s (%s)", video_path,
                    result.stderr.strip()[:120])
        try:
            os.remove(tmp)
        except OSError:
            pass
        return None
    log.debug("extracted audio to %s", tmp)
    return tmp