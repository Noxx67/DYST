"""DYST (did you see that? 👀) — application entry point.

Modes:
  --roll        simulate one odds roll and print the result (headless)
  --test        play one random media file from the media folder, then exit
  --play PATH   play a specific file, then exit
  --daemon      run the chance loop (ticker + overlays) without a tray yet
  (no flag)     status print (safe headless)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import signal
import sys

from dyst import __version__, config as cfg, media
from dyst.media import _validate_settings

log = logging.getLogger("dyst.main")


def _setup_logging(debug: bool) -> None:
    level = logging.DEBUG if debug else logging.INFO
    handlers = [
        logging.StreamHandler(sys.stdout),
    ]
    try:
        handlers.append(logging.FileHandler(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.log"),
            encoding="utf-8"))
    except OSError:
        pass  # best-effort file logging
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
    )


def _reconfigure_console() -> None:
    """Make stdout/stderr UTF-8 so the brand emoji never crashes print/logging
    on Windows consoles that default to cp1252 (Python 3.7+ reconfigure)."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def _spawn_overlay(config: dict, item: media.MediaItem) -> "OverlayWindow | None":
    from dyst.overlay import OverlayWindow  # lazy: only Qt modes need it

    # Route videos: AV1 -> OpenCV+extracted-audio (no Qt hwaccel spam);
    # everything else -> QtMultimedia (audio).
    if item.kind == "video":
        kind = "video-av1" if media.is_av1(item.path) else "video-qt"
    else:
        kind = item.kind
    settings = item.settings or {}
    # Per-file overrides from the same-named .json/.txt sidecar (AGENTS.md).
    # NOTE: image_display_seconds / fade_seconds overrides only apply to images;
    # videos use the global config values (per-file values are ignored).
    mode = settings.get("mode", "fit")
    volume = config["audio_volume"] * float(settings.get("volume", 1.0))
    if item.kind == "image":
        image_seconds = settings.get("image_display_seconds",
                                     settings.get("duration",
                                                  config["image_display_seconds"]))
        fade_seconds = settings.get("fade_seconds", config["fade_seconds"])
    else:
        image_seconds = config["image_display_seconds"]
        fade_seconds = config["fade_seconds"]
    print(f"[CONFIG] Global image_display_seconds={cfg.DEFAULTS['image_display_seconds']}, fade_seconds={cfg.DEFAULTS['fade_seconds']}")
    print(f"[CONFIG] Per-file image_display_seconds={settings.get('image_display_seconds')}, fade_seconds={settings.get('fade_seconds')}")
    print(f"[CONFIG] Using image_seconds={image_seconds}, fade_seconds={fade_seconds} for {item.kind}")
    win = OverlayWindow()
    if not win.load(item.path, kind,
                    image_seconds=image_seconds,
                    fade_seconds=fade_seconds,
                    audio_volume=volume,
                    mode=mode,
                    sidecar_audio=item.sidecar_audio):
        return None
    win.show()
    win.start()
    return win


def _run_daemon(app, config: dict) -> None:
    """Chance loop without tray: ticker + overlays. Manager (global concurrency,
    audio) lands in Phase 5."""
    from dyst.overlay import OverlayWindow
    from dyst.ticker import Ticker

    overlays = []

    def spawner(item: media.MediaItem) -> bool:
        # enforce max_concurrent limit (0 = unlimited)
        cap = int(config.get("max_concurrent", 0) or 0)
        if cap > 0 and len(overlays) >= cap:
            log.debug("max_concurrent (%s) reached – skipping spawn", cap)
            return False
        win = _spawn_overlay(config, item)
        if win is None:
            return False
        overlays.append(win)

        def done(w=win):
            if w in overlays:
                overlays.remove(w)

        win.finished.connect(done)
        return True


    def quit_now(*_a) -> None:
        log.info("daemon: stopping")
        app.quit()

    signal.signal(signal.SIGINT, lambda *_: quit_now())
    signal.signal(signal.SIGTERM, lambda *_: quit_now())

    pool = media.scan(config["media_folder"])

    # Optional periodic rescan so new media dropped into the folder is picked
    # up while the daemon runs (rescan_seconds > 0; 0 = disabled).
    rescan_s = int(config.get("rescan_seconds", 0))
    if rescan_s > 0:
        from PySide6.QtCore import QTimer

        rt = QTimer(app)
        rt.setInterval(rescan_s * 1000)

        def refresh():
            pool.clear()
            pool.extend(media.scan(config["media_folder"]))
            log.debug("daemon: pool rescanned (%d items)", len(pool))

        rt.timeout.connect(refresh)
        rt.start()
        log.info("daemon: rescanning media every %ss", rescan_s)

    ticker = Ticker(lambda: media.pick_from(pool), spawner, config, app)
    app._ticker = ticker  # keep alive & parented to app
    ticker.start()
    log.info("daemon: running (odds=1/%s, tick=%ss)",
             config["odds"], config["tick_seconds"])
    if not pool:
        log.warning("daemon: no media found - chance loop will idle "
                    "(run scripts/make_test_asset.py or drop files in media/)")


def _run_qt(config: dict, args) -> int:
    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv[:1])

    if args.play:
        path = args.play
        kind = media.kind_of(path)
        if kind is None:
            log.error("unsupported media type: %s (use image/video extensions)", path)
            return 1
        item = media.MediaItem(path, kind)
        # Load per-file sidecar settings (same-named .json/.txt)
        base, _ = os.path.splitext(path)
        for ext in (".json", ".txt"):
            spath = base + ext
            if not os.path.isfile(spath):
                continue
            try:
                if ext == ".json":
                    with open(spath, "r", encoding="utf-8") as fh:
                        raw = json.load(fh)
                    if not isinstance(raw, dict):
                        raise ValueError("root must be an object")
                else:
                    raw = _parse_txt_settings(spath)
            except Exception as exc:
                log.warning("media: invalid settings file %s (%s) — ignored", spath, exc)
                continue
            # Merge settings into item.settings (JSON wins over any existing)
            if not item.settings:
                item.settings = _validate_settings(spath, raw)
            else:
                # Merge: keep existing mode/volume, add new display settings
                existing = item.settings
                for k in ("image_display_seconds", "fade_seconds", "mode", "volume"):
                    if k in raw and raw[k] is not None:
                        existing[k] = raw[k]
            break  # Found settings, stop looking
        # Print configuration values
        settings = item.settings or {}
        print(f"[CONFIG] Global image_display_seconds={cfg.DEFAULTS['image_display_seconds']}, fade_seconds={cfg.DEFAULTS['fade_seconds']}")
        print(f"[CONFIG] Per-file image_display_seconds={settings.get('image_display_seconds')}, fade_seconds={settings.get('fade_seconds')}")
        print(f"[CONFIG] Using image_seconds={settings.get('image_display_seconds', cfg.DEFAULTS['image_display_seconds'])}, fade_seconds={settings.get('fade_seconds', cfg.DEFAULTS['fade_seconds'])} for {item.kind}")
        win = _spawn_overlay(config, item)
        if win is None:
            return 1
        win.finished.connect(app.quit)
        log.info("play: showing %s (%s)", path, kind)

    elif args.test:
        item = media.pick_random(config["media_folder"])
        if item is None:
            log.error("no media found under %s - run scripts/make_test_asset.py first",
                      config["media_folder"])
            return 1
        win = _spawn_overlay(config, item)
        if win is None:
            return 1
        win.finished.connect(app.quit)
        log.info("test: playing %s (%s)", item.path, item.kind)

    elif args.daemon:
        _run_daemon(app, config)

    return app.exec()


def main(argv=None) -> int:
    _reconfigure_console()
    parser = argparse.ArgumentParser(prog="dyst", description=f"{cfg.APP_NAME}")
    parser.add_argument("--test", action="store_true",
                        help="play one random media file, then exit")
    parser.add_argument("--play", metavar="PATH", help="play a specific media file, then exit")
    parser.add_argument("--daemon", action="store_true",
                        help="run the chance loop (ticker) without a tray icon")
    parser.add_argument("--roll", action="store_true",
                        help="simulate one roll and print the result")
    parser.add_argument("--config", default="config.json", help="path to config file")
    args = parser.parse_args(argv)

    config = cfg.load_config(args.config)
    _setup_logging(config["debug"])
    log.info("%s starting (version %s)", cfg.APP_NAME, __version__)
    log.debug("loaded config: %r", config)

    if args.roll:
        chance = 1.0 / config["odds"]
        result = random.random() < chance
        print(f"roll: odds=1/{config['odds']} chance={chance:.6f} "
              f"result={'SUCCESS' if result else 'fail'}")
        return 0

    if args.test or args.play or args.daemon:
        try:
            return _run_qt(config, args)
        except ImportError as exc:
            print("ERROR: missing dependency:", exc)
            print("DYST runs inside a virtualenv. Please run it via:")
            print("    run.bat")
            print("  or  .venv\\Scripts\\python main.py ...")
            return 1

    print(f"{cfg.APP_NAME} - Phase 1a (overlay playback spike).")
    print("Modes: --test (play random media) | --play PATH | --daemon (chance loop) | --roll")
    print("Loaded config:")
    for key, value in config.items():
        print(f"  {key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())