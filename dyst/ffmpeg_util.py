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


def _sample_rate(path: str) -> int:
    """Best-effort probe of the audio sample rate (Hz). Fallback 48000."""
    ffprobe = shutil.which("ffprobe") or find_ffmpeg()
    if ffprobe is None:
        return 48000
    try:
        cmd = [ffprobe, "-v", "error", "-select_streams", "a:0",
               "-show_entries", "stream=sample_rate", "-of", "csv=p=0", path]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        return int(out.stdout.strip().splitlines()[0])
    except (ValueError, IndexError, OSError, subprocess.TimeoutExpired):
        return 48000


def _atempo_chain(value: float, parts: list[str] | None = None) -> list[str]:
    """Build ffmpeg atempo filter list for an arbitrary time-stretch factor.
    atempo is limited to [0.5, 2.0] per filter; split multiplicatively."""
    if parts is None:
        parts = []
    if value > 2.0:
        parts.append("atempo=2.0")
        return _atempo_chain(value / 2.0, parts)
    if value < 0.5:
        parts.append("atempo=0.5")
        return _atempo_chain(value / 0.5, parts)
    parts.append(f"atempo={value:.6f}")
    return parts


def pitch_shift(path: str, pitch: float = 1.0, speed: float = 1.0) -> str | None:
    """Bake pitch and tempo changes into a temp audio file.

    pitch: frequency multiplier (1.0 = unchanged; >1 = higher pitch).
    speed: playback-tempo multiplier, independent of pitch (1.0 = unchanged;
          >1 = faster, pitch stable).

    Uses asetrate to shift the sample rate (pitch), aresample to restore the
    rate, then an atempo chain so the final tempo = speed. Result: audio that
    plays at `speed` speed and `pitch` pitch with the ORIGINAL duration
    scaled only by 1/speed. Returns the temp file path (caller deletes it),
    or None on failure / missing ffmpeg.
    """
    if pitch == 1.0 and speed == 1.0:
        return path
    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        log.warning("ffmpeg not found — speed/pitch disabled for %s", path)
        return None
    sr = _sample_rate(path)
    tempo = speed / pitch
    filters = [f"asetrate={sr * pitch:.0f}", f"aresample={sr}"] + _atempo_chain(tempo)
    tmp = tempfile.mktemp(suffix=".m4a", prefix="dyst_speed_")
    cmd = [ffmpeg, "-y", "-v", "error", "-i", path, "-vn", "-c:a", "aac",
           "-b:a", "160k", "-af", ",".join(filters), tmp]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not os.path.isfile(tmp):
        log.warning("speed/pitch bake failed for %s (%s)", path,
                    result.stderr.strip()[:120])
        try:
            os.remove(tmp)
        except OSError:
            pass
        return None
    log.debug("baked speed=%s pitch=%s audio to %s", speed, pitch, tmp)
    return tmp