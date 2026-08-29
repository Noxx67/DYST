"""DYST (did you see that? 👀) — download videos from a URL list.

Reads URLs line-by-line from a text file (default: video-urls.txt) and downloads
each video (video+audio muxed, NO audio extraction) into media/videos/ using yt-dlp.

Usage:
    python scripts/download_videos.py [urls_file] [output_dir]

Notes:
- Blank lines and lines starting with '#' are skipped.
- A failed URL is logged and skipped; the rest still download.
- Requires yt-dlp on PATH (or installed as a Python script).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)  # so the downloader can read dyst.config

from dyst import config as cfg  # noqa: E402


def find_ytdlp() -> str | None:
    return shutil.which("yt-dlp")


def find_ffmpeg() -> str | None:
    """Locate ffmpeg: PATH first, then well-known WinGet install dirs.

    WinGet adds ffmpeg to PATH only after the shell is restarted, so an open
    PowerShell won't see it. We check the known locations ourselves and pass
    the result to yt-dlp via --ffmpeg-location.
    """
    found = shutil.which("ffmpeg")
    if found:
        return found

    home = os.path.expanduser("~")
    links_dir = os.path.join(home, "AppData", "Local", "Microsoft", "WinGet", "Links")
    candidates = [
        os.path.join(links_dir, "ffmpeg.exe"),
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


def read_urls(path: str) -> list[str]:
    urls = []
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            urls.append(line)
    return urls


def download(url: str, out_dir: str, ytdlp: str, ffmpeg: str, max_height: int = 720) -> bool:
    """Download a single URL and route the file to the correct folder.

    - GIFs (extension ``.gif``) are moved to ``media/gif/``.
    - All other video files (``mp4``, ``webm``, ``avi`` …) are moved to
      ``media/videos/``.

    The function uses the **most recent file in ``out_dir``** as the
    downloaded file (yt‑dlp writes the merged result there). The move
    operation is a simple ``shutil.move``; if the file is already in the
    target folder the call is a no‑op.
    """
    os.makedirs(out_dir, exist_ok=True)
    template = os.path.join(out_dir, "%(title)s [%(id)s].%(ext)s")

    # Choose the best format. Tenor: prefer transparency-friendly formats
    # so alpha is preserved; avoid converting to GIF because ffmpeg's gif
    # encoder strips alpha/transparency.
    if "tenor.com" in url.lower():
        format_selector = "best[ext=webm]/best[ext=gif]/best"
    else:
        format_selector = (
            f"bestvideo[height<={max_height}]+bestaudio/"
            f"best[height<={max_height}]/best"
        )

    cmd = [
        ytdlp,
        "-f", format_selector,
        "--ffmpeg-location", os.path.dirname(ffmpeg),
        "-o", template,
        "--no-playlist",
    ]

    cmd.append(url)

    print(f"\n--- Downloading: {url}")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(f"FAIL  {url} (yt-dlp exit {result.returncode})")
        return False

    # ------------------------------------------------------------------
    # Find the file that was just downloaded (most recent in out_dir).
    # ------------------------------------------------------------------
    candidates = []
    for f in os.listdir(out_dir):
        fp = os.path.join(out_dir, f)
        if os.path.isfile(fp):
            candidates.append((os.path.getmtime(fp), fp))
    if not candidates:
        print(f"SKIP  {url} – no file found in {out_dir}")
        return True

    # Newest file wins
    candidates.sort(reverse=True)
    _, downloaded = candidates[0]
    ext = os.path.splitext(downloaded)[1].lower()

    # Decide destination based on source type.
    is_tenor = "tenor.com" in url.lower()
    is_gif_source = is_tenor or ext == ".gif"

    if is_gif_source:
        dest_dir = os.path.join(ROOT, "media", "gifs")
    else:
        dest_dir = os.path.join(ROOT, "media", "videos")
    os.makedirs(dest_dir, exist_ok=True)

    # Tenor sources: keep the downloaded file as-is so transparency is preserved.
    # Non-Tenor .gif sources: convert to GIF if needed (legacy behavior).
    final_path = downloaded
    if is_gif_source and not is_tenor and ext != ".gif":
        gif_path = os.path.splitext(downloaded)[0] + ".gif"
        conv = subprocess.run(
            [
                ffmpeg,
                "-i", downloaded,
                "-vf", "fps=10,scale=320:-1:flags=lanczos",
                "-c:v", "gif",
                gif_path,
            ],
            capture_output=True,
        )
        if conv.returncode == 0:
            try:
                os.remove(downloaded)
            except OSError:
                pass
            final_path = gif_path
        else:
            # Conversion failed – fall back to videos folder.
            dest_dir = os.path.join(ROOT, "media", "videos")

    # Tenor fallback: if yt-dlp gave us a non-transparent format (usually MP4),
    # try to grab the original transparent GIF from Tenor's CDN directly.
    if is_tenor and os.path.splitext(final_path)[1].lower() not in (".gif", ".webm"):
        gif_candidate = _try_tenor_gif(ytdlp, url, ffmpeg, out_dir)
        if gif_candidate:
            try:
                os.remove(final_path)
            except OSError:
                pass
            final_path = gif_candidate
            print(f"FIX   {url}  – replaced MP4 with Tenor direct GIF for transparency")

    dest_path = os.path.join(dest_dir, os.path.basename(final_path))
    if os.path.normpath(final_path) == os.path.normpath(dest_path):
        print(f"OK    {url}  (already in {dest_dir})")
        return True

    try:
        shutil.move(final_path, dest_path)
    except FileNotFoundError:
        print(f"SKIP  {url} – file disappeared before move")
        return True

    print(f"OK    {url}  ->  {dest_dir}")
    return True


def _try_tenor_gif(ytdlp: str, url: str, ffmpeg: str, out_dir: str) -> str | None:
    """Try to fetch the transparent GIF directly from Tenor's CDN.

    yt-dlp's generic Tenor extractor often only exposes an MP4. This helper
    probes the available formats via ``--dump-single-json`` and, if it finds
    a ``media.tenor.com`` URL, tries to swap the extension to ``.gif`` and
    download that directly so alpha is preserved.
    """
    probe = subprocess.run(
        [
            ytdlp,
            "--dump-single-json",
            "--no-download",
            "--ffmpeg-location", os.path.dirname(ffmpeg),
            url,
        ],
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        return None

    try:
        data = json.loads(probe.stdout)
        for fmt in data.get("formats", []):
            raw_url = fmt.get("url", "")
            if "media.tenor.com" not in raw_url:
                continue
            if raw_url.endswith(".gif"):
                gif_url = raw_url
            elif raw_url.endswith(".mp4"):
                gif_url = raw_url[:-4] + ".gif"
            elif raw_url.endswith(".webm"):
                gif_url = raw_url[:-5] + ".gif"
            else:
                continue

            gif_path = os.path.join(out_dir, f"tenor_gif_{data.get('id', 'tenor')}.gif")
            try:
                req = urllib.request.Request(gif_url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    if resp.status == 200 and resp.headers.get("Content-Type", "").startswith("image/"):
                        with open(gif_path, "wb") as fh:
                            fh.write(resp.read())
                        return gif_path
            except Exception:
                continue
    except Exception:
        pass

    return None


def main(argv=None) -> int:
    urls_file = argv[0] if argv and len(argv) > 0 else os.path.join(ROOT, "video-urls.txt")
    out_dir = argv[1] if argv and len(argv) > 1 else os.path.join(ROOT, "media", "videos")
    # Resolution cap: CLI arg > config.json (download_max_height) > 1080.
    config = cfg.load_config(os.path.join(ROOT, "config.json"))
    max_height = int(argv[2]) if argv and len(argv) > 2 else int(config["download_max_height"])

    ytdlp = find_ytdlp()
    if ytdlp is None:
        print("ERROR: yt-dlp not found on PATH. Install with: pip install yt-dlp")
        return 1

    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        print("ERROR: ffmpeg not found. Install it (e.g. 'winget install Gyan.FFmpeg')"
              " or restart your terminal after installing.")
        return 1
    print(f"Using ffmpeg: {ffmpeg}")

    if not os.path.isfile(urls_file):
        print(f"ERROR: no URL list at {urls_file}")
        return 1

    urls = read_urls(urls_file)
    if not urls:
        print(f"No URLs in {urls_file} (blank/#-lines are ignored).")
        return 0

    print(f"Using yt-dlp: {ytdlp}")
    print(f"Max height: {max_height}px (config download_max_height)")
    print(f"Downloading {len(urls)} URL(s) into {out_dir} ...")

    ok = failed = 0
    for url in urls:
        if download(url, out_dir, ytdlp, ffmpeg, max_height):
            ok += 1
        else:
            failed += 1

    print(f"\nDone: {ok} ok, {failed} failed.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
