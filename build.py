"""DYST (did you see that? 👀) — one-shot build script.

Builds the project with PyInstaller (DYST.spec), then copies config.json and
the media/ folder into the build output so the .exe has everything it needs
next to it (external, user-editable config + media — no bundling).

Usage:
    .venv\\Scripts\\python build.py            # full build + copy
    python build.py --skip-copy                # build only

Exits 0 on success, nonzero on failure.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SPEC = ROOT / "DYST.spec"
DIST_DIR = ROOT / "dist" / "DYST"          # PyInstaller onedir output
CONFIG_SRC = ROOT / "config.json"
MEDIA_SRC = ROOT / "media"
COPY_ITEMS = ("config.json", "media")


def _run_build() -> int:
    print(f"[build] PyInstaller: {sys.executable} -m PyInstaller {SPEC.name}")
    proc = subprocess.run(
        [sys.executable, "-m", "PyInstaller", str(SPEC), "--noconfirm", "--clean"],
        cwd=str(ROOT),
    )
    if proc.returncode != 0:
        print(f"[build] FAILED (PyInstaller exit code {proc.returncode})", file=sys.stderr)
        return proc.returncode
    if not (DIST_DIR / "DYST.exe").is_file():
        print(f"[build] FAILED: output exe not found at {DIST_DIR / 'DYST.exe'}", file=sys.stderr)
        return 1
    print(f"[build] PyInstaller OK -> {DIST_DIR}")
    return 0


def _copy_runtime_files() -> int:
    """Refresh config.json and media/ inside the build output (fresh copy)."""
    for name in COPY_ITEMS:
        src = ROOT / name

        # Remove the previous copy so stale files don't linger (source of truth
        # is always the project root).
        dst = DIST_DIR / name
        try:
            if dst.exists():
                if dst.is_dir():
                    shutil.rmtree(dst)
                else:
                    dst.unlink()

            if not src.exists():
                print(f"[copy] WARNING: {src} does not exist — skipping")
                continue

            if src.is_dir():
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
            print(f"[copy] {name} -> {DIST_DIR / name}")
        except OSError as exc:
            print(f"[copy] FAILED copying {name}: {exc}", file=sys.stderr)
            return 1
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="build", description="Build DYST and stage runtime files.")
    parser.add_argument("--skip-copy", action="store_true", help="build only; don't copy config/media")
    args = parser.parse_args(argv)

    if not SPEC.is_file():
        print(f"[build] ERROR: spec not found: {SPEC}", file=sys.stderr)
        return 1

    rc = _run_build()
    if rc != 0:
        return rc

    if not args.skip_copy:
        rc = _copy_runtime_files()
        if rc != 0:
            return rc
    else:
        print("[build] --skip-copy: config/media not copied")

    total = sum(
        f.stat().st_size
        for f in DIST_DIR.rglob("*")
        if f.is_file()
    )
    print(f"[build] done — {DIST_DIR} ({total / 1048576:.1f} MB), exe: {DIST_DIR / 'DYST.exe'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())