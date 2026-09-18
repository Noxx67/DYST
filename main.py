"""DYST (did you see that? 👀) — application entry point.

Modes:
  --roll        simulate one odds roll and print the result (headless)
  --test        play one random media file from the media folder, then exit
  --play PATH   play a specific file, then exit
  --daemon      run the chance loop (ticker + overlays) with a tray icon
  --no-tray     run the daemon without the tray icon (headless/debug)
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

from dyst import __version__, chroma as chroma_mod, config as cfg, media, precache as precache_mod
from dyst.hotkey import register_hotkey, unregister_hotkey, HotkeyFilter
from dyst import killswitch
from dyst import notify
from dyst.media import _parse_txt_settings, _validate_settings

log = logging.getLogger("dyst.main")


def resolve_speed_pitch(settings: dict, config: dict) -> tuple[float, float]:
    """Resolve the effective (speed, pitch) from per-file settings + config.

    `speed_pitch` (per-file > global) sets BOTH speed and pitch to the same
    value and OVERRIDES the individual speed/pitch keys; otherwise per-file
    speed/pitch override global. All default to 1.0.
    Values can be single numbers or tuples (lo, hi) for randomization.
    """
    def _resolve_val(val, default):
        if val is None:
            return default
        if isinstance(val, tuple) and len(val) == 2:
            try:
                lo, hi = val
                return random.uniform(lo, hi)
            except (TypeError, ValueError):
                return default
        try:
            return float(val)
        except (TypeError, ValueError):
            return default

    sp = settings.get("speed_pitch")
    if sp is None:
        sp = config.get("speed_pitch", 0.0)
    # Handle range tuples (lo, hi) for randomization
    if isinstance(sp, tuple) and len(sp) == 2:
        try:
            lo, hi = sp
            sp = random.uniform(lo, hi)
        except (TypeError, ValueError):
            sp = 0.0
    else:
        # Single value - try to convert to float
        try:
            sp = float(sp or 0.0)
        except (TypeError, ValueError):
            sp = 0.0
    if sp > 0:
        return sp, sp
    speed = _resolve_val(settings.get("speed"), config.get("speed", 1.0))
    pitch = _resolve_val(settings.get("pitch"), config.get("pitch", 1.0))
    return speed, pitch


def resolve_scale(settings: dict, config: dict) -> tuple[float, float]:
    """Resolve the effective custom-mode (scale_x, scale_y).

    The uniform `scale` key sets BOTH axes to the same value (scale 2 =
    twice as wide AND twice as tall). For a range value (e.g. "0.3~0.6")
    ONE random value is drawn and applied to both axes, so X and Y always
    match. It is overwritten when BOTH `scale_x` and `scale_y` are given
    explicitly in the per-file settings (then the two per-axis values are
    used as-is — each may randomize independently); a lone
    `scale_x`/`scale_y` overrides just its own axis. Without any per-file
    scale key, the global `scale_x`/`scale_y` config applies. Values can
    be single numbers or (lo, hi) tuples for randomization.
    """
    def _r(v, d):
        if v is None:
            return d
        if isinstance(v, tuple) and len(v) == 2:
            try:
                lo, hi = v
                return random.uniform(lo, hi)
            except (TypeError, ValueError):
                return d
        try:
            return float(v)
        except (TypeError, ValueError):
            return d

    sx = settings.get("scale_x")
    sy = settings.get("scale_y")
    s = settings.get("scale")
    if sx is not None and sy is not None:
        # BOTH per-axis keys given explicitly -> they overwrite `scale`.
        return _r(sx, 1.0), _r(sy, 1.0)
    # Draw the uniform `scale` ONCE so both axes share the same value.
    s_resolved = _r(s, None)
    if s_resolved is None:
        return _r(sx, config.get("scale_x", 1.0)), \
               _r(sy, config.get("scale_y", 1.0))
    return _r(sx, s_resolved), _r(sy, s_resolved)


def resolve_playback_caps(settings: dict, config: dict) -> tuple[int, float]:
    """Resolve the effective playback caps (max_playback_height,
    max_playback_fps) from per-file settings + config. Per-file sidecar
    wins over global; 0 = no cap; defaults are the config ones (480/30).
    The SAME values must be used for the precache call and the overlay, so
    the cached alpha-mask geometry matches playback exactly."""
    def _v(key: str, default):
        val = (settings or {}).get(key)
        if val is None:
            val = config.get(key)
        try:
            v = float(val)
        except (TypeError, ValueError):
            v = default
        return int(v) if key == "max_playback_height" else v

    return (_v("max_playback_height", 480),
            _v("max_playback_fps", 30.0))


def get_base_dir() -> str:
    """Returns the directory of the .exe when compiled, or main.py when running in dev."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))



def _setup_logging(debug: bool) -> None:
    level = logging.DEBUG if debug else logging.INFO
    handlers = [
        logging.StreamHandler(sys.stdout),
    ]
    try:
        handlers.append(logging.FileHandler(
        os.path.join(get_base_dir(), "app.log"),
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


def _apply_console_mode(config: dict) -> None:
    """Honour the `show_console` config flag on the frozen (PyInstaller) exe.

    The exe is built with `console=True` so it CAN show a live log terminal.
    When `show_console` is False (background mode), detach from that console
    with FreeConsole() as early as possible and neutralise stdout/stderr so
    any later logging/print writes are harmless (logs still go to app.log).
    """
    if config.get("show_console", False):
        return  # terminal mode: keep the console, logs stream to it
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return  # dev runs keep their stdout; nothing to detach
    try:
        import ctypes

        ctypes.windll.kernel32.FreeConsole()
    except Exception:
        pass
    # stdout/stderr now point at a closed console; neutralise them.
    import io

    sys.stdout = io.StringIO()
    sys.stderr = io.StringIO()


def _spawn_overlay(config: dict, item: media.MediaItem, pre: bool = False) -> "OverlayWindow | None":
    """Spawn one overlay for *item*.

    pre=True (one-shot modes: --play / --test): if the video needs chroma key
    and has no valid cache yet, it is PRE-PROCESSED synchronously first (with
    progress logs) and then played from the cache — the first playback is
    already smooth. pre=False (daemon tick path): the caller (spawner) handles
    preprocessing asynchronously; if the cache is still missing here the
    overlay falls back to live keying instead of blocking.
    """
    from dyst.overlay import OverlayWindow  # lazy: only Qt modes need it

    settings = item.settings or {}
    # Chroma key (green/blue screen removal): decided ONCE here, passed to
    # the overlay. Global enabled + exceptions (config.json) plus a per-file
    # "chroma": false override. Videos that need chroma can't use QtMultimedia
    # (it decodes internally), so they route to "video-chroma" (OpenCV frames
    # + sidecar-or-extracted audio via ffmpeg). Images key at load time.
    chroma_key_cfg = config.get("chroma_key", {})
    # Per-file "chroma_key" preset override (green/blue/weak/strong
    # variants): each file can use its own preset independent of the
    # global one. "chroma: false" (per-file) still wins and disables
    # keying. chroma_preset_cfg sets enabled=True (choosing a preset
    # means filter this file).
    per_preset = settings.get("chroma_key") if settings else None
    if per_preset:
        chroma_key_cfg = cfg.chroma_preset_cfg(per_preset, chroma_key_cfg)
        # Per-file "custom" preset: ranges come from this sidecar
        # (chroma_hue_range / chroma_saturation_range /
        # chroma_value_range) — inherited from global otherwise.
        if str(per_preset).strip().lower() == "custom":
            chroma_key_cfg = cfg.apply_custom_chroma_ranges(
                chroma_key_cfg, settings)
    use_chroma = chroma_mod.should_apply(item.path, chroma_key_cfg, settings)
    chroma_params = chroma_key_cfg if use_chroma else None
    # Playback caps (resolution/fps) — per-file sidecar can override the
    # global values; the same values must reach the precache and the overlay
    # so cached masks and decoded frames share a geometry.
    max_pb_h, max_pb_fps = resolve_playback_caps(settings, config)
    log.debug("chroma: %s for %s (enabled=%s, preset=%r, hue=%s, sat=%s, val=%s, despill=%s)",
              "ON" if use_chroma else "OFF",
              os.path.basename(item.path),
              chroma_key_cfg.get("enabled"), chroma_key_cfg.get("preset"),
              chroma_key_cfg.get("hue_range"), chroma_key_cfg.get("saturation_range"),
              chroma_key_cfg.get("value_range"), chroma_key_cfg.get("despill"))
    # Chroma videos play from the pre-processed alpha-mask cache when one
    # exists (precompute once, then ~2-5 ms/frame instead of ~50-60 ms keying).
    # See dyst/precache.py for the cache policy (lazy, invalidated on media or
    # settings change).
    cached = None
    if item.kind == "video":
        if use_chroma:
            cached = precache_mod.cache_ready(item.path, chroma_key_cfg,
                                              max_pb_h, max_pb_fps)
            if cached is None and pre:
                log.info("precache: preprocessing %s (first use with the "
                         "current chroma settings) — one-time",
                         os.path.basename(item.path))
                cached = precache_mod.ensure(
                    item.path, chroma_key_cfg,
                    progress=lambda i, n: log.info("precache: keyed %d/%d frames", i, n),
                    max_height=max_pb_h, max_fps=max_pb_fps)
            kind = "video-chroma-cached" if cached else "video-chroma"
            if cached:
                log.debug("precache: cache HIT for %s — playing from cached "
                          "alpha masks (%d frames, %.1fs video)",
                          os.path.basename(item.path),
                          cached["meta"]["frame_count"],
                          cached["meta"]["frame_count"] / cached["meta"]["fps"])
        else:
            kind = "video-av1" if media.is_av1(item.path) else "video-qt"
    else:
        kind = item.kind
    # Per-file overrides from the same-named .json/.txt sidecar (AGENTS.md).
    # NOTE: image_display_seconds / fade_out_seconds overrides only apply to images;
    # videos use the global config values (per-file values are ignored).
    # mode: per-file sidecar wins; falls back to the global config `mode`
    # (default "fit"). The removed "cover" value is rejected upstream by
    # media._validate_settings / config validation.
    mode = settings.get("mode") or config.get("mode", "fit")
    # Custom-mode layout (position/scale/flip/rotation) — only used when
    # mode == "custom"; per-file sidecar wins over the global config keys.
    custom = {}
    for key, default in (("position_x", 0.5), ("position_y", 0.5),
                         ("flip_h", False), ("flip_v", False),
                         ("rotation", 0.0)):
        custom[key] = settings.get(key, config.get(key, default))
    # scale: the uniform `scale` key stretches BOTH axes (per-file wins over
    # global defaults); it is overwritten when BOTH `scale_x` AND `scale_y`
    # are given explicitly in the per-file settings (see resolve_scale).
    custom["scale_x"], custom["scale_y"] = resolve_scale(settings, config)
    # max_duration: hard cap on the whole overlay (visual + audio). Per-file
    # wins over global; 0 = no cap. Uses "in settings" (not `or`) so a per-file
    # 0 can explicitly disable a global cap.
    max_duration = (settings["max_duration"] if "max_duration" in settings
                    else config.get("max_duration", 0.0))
    # speed / pitch: `speed_pitch` (per-file > global) sets BOTH to the
    # same value and overrides the individual keys; otherwise per-file
    # speed/pitch win over global. Defaults 1.0 (no change).
    speed, pitch = resolve_speed_pitch(settings, config)
    # opacity: per-file wins over global; 1.0 = fully opaque (default).
    opacity = float(settings.get("opacity", config.get("opacity", 1.0)))
    volume = config["volume"] * float(settings.get("volume", 1.0))
    if item.kind == "image":
        image_seconds = settings.get("image_display_seconds",
                                     settings.get("duration",
                                                  config["image_display_seconds"]))
        fade_out_seconds = settings.get("fade_out_seconds", config["fade_out_seconds"])
        fade_in_seconds = settings.get("fade_in_seconds", config["fade_in_seconds"])
    else:
        image_seconds = config["image_display_seconds"]
        fade_out_seconds = config["fade_out_seconds"]
        fade_in_seconds = 0.0  # videos don't fade in (they also end instantly)
    # Monitor: global-only config ("primary" or a 0-based index). The overlay
    # resolves it to a QScreen and falls back to primary on bad values.
    win = OverlayWindow(monitor=config.get("monitor", "primary"))
    if not win.load(item.path, kind,
                    image_seconds=image_seconds,
                    fade_out_seconds=fade_out_seconds,
                    fade_in_seconds=fade_in_seconds,
                    opacity=opacity,
                    volume=volume,
                    mode=mode,
                    custom=custom,
                    chroma=chroma_params,
                    cached=cached,
                    max_duration=max_duration,
                    speed=speed,
                    pitch=pitch,
                    sidecar_audio=item.sidecar_audio,
                    max_playback_height=max_pb_h,
                    max_playback_fps=max_pb_fps,
                    end_on_audio_end=settings.get("end_on_audio_end",
                                                  config.get("end_on_audio_end", False))):
        return None
    win.show()
    win.start()
    return win


def _handle_autostart(config: dict, args) -> bool:
    """Sync the Windows Run key with the `autostart` config (source of truth).

    Returns True to keep running, False when the app has already unregistered
    and stopped (boot launch while autostart is off).

    - autostart ON  -> (re)register the Run key on every launch (a moved or
      updated executable re-points itself), then keep running silently.
    - autostart OFF -> if this launch came FROM the Run key (the stored
      command carries --autostart): the PC just booted us with a stale
      registration — remove the key and exit immediately. If it was a manual
      launch, still clear any stale key but keep running.
    """
    if os.name != "nt":
        return True
    from dyst import autostart as aut  # lazy: winreg is Windows-only

    if config.get("autostart", False):
        if aut.enable():
            log.info("autostart: on — registered to start with Windows (Run key)")
        return True

    stored = aut.get_command()
    if args.autostart and stored is not None:
        # Windows booted us via the Run key, but the config now says off:
        # unregister ourselves and stop, so next boot doesn't start DYST.
        aut.disable()
        log.info("autostart: off — removed the Run key and stopping "
                 "(config says autostart is off)")
        return False
    if stored is not None:
        # Manual launch with a stale registration — tidy it up, keep running.
        aut.disable()
        log.info("autostart: off — removed stale Run key (manual launch)")
    return True


def _run_daemon(app, config: dict, use_tray: bool = True) -> None:
    """Chance loop: ticker + overlays + tray icon (Pause/Resume + Quit).

    Chroma-video lazy pre-processing: when a trigger picks an uncached chroma
    video, the ticker PAUSES, the video is preprocessed in a worker thread
    (already-playing overlays keep running), the overlay then spawns from the
    cache, and the ticker resumes. Later occurrences of the same video spawn
    instantly from the cache. Several triggers landing on the same video
    while it preprocesses are grouped on one job.
    """
    import threading
    from dyst.overlay import OverlayWindow
    from dyst.ticker import Ticker
    from PySide6.QtCore import QObject, Signal

    overlays = []
    ticker_holder: list = []          # ticker is created after the spawner
    precache_jobs: dict = {}          # cache_key -> {"items": [...], "thread": Thread}

    class _PreCacheDone(QObject):
        done = Signal()

    active_path_counts: dict[str, int] = {}
    reserved_paths: set[str] = set()

    def _item_play_once(item: media.MediaItem) -> bool:
        return media.effective_play_once(item.settings, config.get("play_once", False))

    def _play_once_available(item: media.MediaItem, allow_reserved: bool = False) -> bool:
        if not _item_play_once(item):
            return True
        key = media.path_key(item.path)
        if active_path_counts.get(key, 0) > 0:
            return False
        if key in reserved_paths and not allow_reserved:
            return False
        return True

    def _claim_play_once(item: media.MediaItem) -> bool:
        if not _item_play_once(item):
            return True
        key = media.path_key(item.path)
        if active_path_counts.get(key, 0) > 0 or key in reserved_paths:
            return False
        reserved_paths.add(key)
        return True

    def _release_play_once(item: media.MediaItem) -> None:
        if not _item_play_once(item):
            return
        reserved_paths.discard(media.path_key(item.path))

    def _activate_play_once(item: media.MediaItem) -> None:
        if not _item_play_once(item):
            return
        key = media.path_key(item.path)
        reserved_paths.discard(key)
        active_path_counts[key] = active_path_counts.get(key, 0) + 1

    def _deactivate_play_once(item: media.MediaItem) -> None:
        if not _item_play_once(item):
            return
        key = media.path_key(item.path)
        active_path_counts[key] = max(0, active_path_counts.get(key, 0) - 1)
        if active_path_counts[key] == 0:
            del active_path_counts[key]

    def _try_spawn(item: media.MediaItem, allow_reserved: bool = False) -> bool:
        if not _play_once_available(item, allow_reserved=allow_reserved):
            log.debug("play_once: %s already active/reserved — skipping duplicate", item.path)
            return False
        # enforce max_concurrent limit (0 = unlimited)
        cap = int(config.get("max_concurrent", 0) or 0)
        if cap > 0 and len(overlays) >= cap:
            log.debug("max_concurrent (%s) reached – skipping spawn", cap)
            return False
        win = _spawn_overlay(config, item)
        if win is None:
            _release_play_once(item)
            return False
        _activate_play_once(item)
        overlays.append(win)

        def done(w=win, item=item):
            if w in overlays:
                overlays.remove(w)
            _deactivate_play_once(item)

        win.finished.connect(done)
        return True

    def _on_precache_done(key: str) -> None:
        log.debug("precache: job %s finished callback (GUI thread)", key[:8])
        entry = precache_jobs.pop(key, None)
        if entry:
            entry["sig"] = None  # allow the signal bridge to be GC'd now
        # Resume the chance loop once no preprocessing job remains — unless
        # the user paused from the tray while the job was running.
        if not precache_jobs and ticker_holder and ticker_holder[0]:
            tray = getattr(app, "_tray", None)
            if tray is not None and tray.paused:
                log.debug("precache: done but the tray is PAUSED — loop stays paused")
            else:
                log.info("precache: done — chance loop resumed")
                ticker_holder[0].start()
        if entry:
            # Cache is ready now (or preprocessing failed — _spawn_overlay
            # falls back to live keying); spawn everything that was queued.
            for item in entry["items"]:
                _try_spawn(item, allow_reserved=True)

    def _precache_worker(item: media.MediaItem, chroma_cfg: dict, sig, max_h: int, max_fps: float) -> None:
        try:
            precache_mod.ensure(
                item.path, chroma_cfg,
                progress=lambda i, n: log.info("precache: keyed %d/%d frames", i, n),
                max_height=max_h, max_fps=max_fps)
        finally:
            log.debug("precache: worker thread done, emitting completion")
            sig.done.emit()  # queued -> GUI thread

    def spawner(item: media.MediaItem) -> bool:
        cap = int(config.get("max_concurrent", 0) or 0)
        if cap > 0 and len(overlays) >= cap:
            log.debug("max_concurrent (%s) reached – skipping spawn", cap)
            return False
        settings = item.settings or {}
        chroma_key_cfg = config.get("chroma_key", {})
        per_preset = settings.get("chroma_key")
        if per_preset:
            chroma_key_cfg = cfg.chroma_preset_cfg(per_preset, chroma_key_cfg)
            if str(per_preset).strip().lower() == "custom":
                chroma_key_cfg = cfg.apply_custom_chroma_ranges(
                    chroma_key_cfg, settings)
        # Same playback caps the overlay will use — the precache must be
        # built with them so mask geometry matches playback.
        max_pb_h, max_pb_fps = resolve_playback_caps(settings, config)
        if (item.kind == "video"
                and chroma_mod.should_apply(item.path, chroma_key_cfg, settings)
                and precache_mod.cache_ready(item.path, chroma_key_cfg,
                                             max_pb_h, max_pb_fps) is None):
            # First (or parallel) trigger(s) on an uncached chroma video:
            # pause the loop, preprocess once, spawn from cache afterwards.
            key = precache_mod.cache_key(item.path, chroma_key_cfg,
                                         max_pb_h, max_pb_fps)
            entry = precache_jobs.setdefault(key, {"items": [], "thread": None})
            if entry["thread"] is None:
                if not _claim_play_once(item):
                    log.debug("play_once: %s already reserved — skipping precache queue", item.path)
                    return False
                if ticker_holder and ticker_holder[0]:
                    ticker_holder[0].stop()  # pause the chance loop
                    log.info("precache: chance loop PAUSED while %s is "
                             "preprocessed (one-time)", os.path.basename(item.path))
                sig = _PreCacheDone()
                sig.done.connect(lambda k=key: _on_precache_done(k))
                # Keep the signal bridge referenced from the GUI side: the
                # worker thread only holds it until emit(); if nothing else
                # references it, PySide6 GCs the QObject before the queued
                # event is delivered and the completion callback never runs.
                entry["sig"] = sig
                entry["thread"] = threading.Thread(
                    target=_precache_worker,
                    args=(item, chroma_key_cfg, sig, max_pb_h, max_pb_fps), daemon=True)
                entry["thread"].start()
            if item not in entry["items"]:
                entry["items"].append(item)
            return True  # trigger consumed; the overlay spawns after preprocessing
        return _try_spawn(item)


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

    def _picker() -> media.MediaItem | None:
        eligible = [
            item for item in pool
            if not (media.effective_play_once(item.settings, config.get("play_once", False))
                    and active_path_counts.get(media.path_key(item.path), 0) > 0)
        ]
        return media.pick_from(eligible)

    ticker = Ticker(_picker, spawner, config, app)
    app._ticker = ticker  # keep alive & parented to app
    ticker_holder.append(ticker)  # spawner can pause/resume for preprocessing
    ticker.start()

    # Tray icon: the only visible handle on the app. Pause stops the chance
    # loop AND dismisses whatever is on screen; Quit exits cleanly.
    if use_tray:
        from dyst.tray import Tray

        def _on_tray_pause(paused: bool) -> None:
            if paused:
                ticker.stop()
                for win in list(overlays):
                    try:
                        win.dismiss()
                    except Exception:
                        log.debug("tray: overlay dismiss failed", exc_info=True)
                log.info("tray: PAUSED — no media will appear until resumed")
            else:
                if precache_jobs:
                    # A one-time precache paused the loop; resuming is handled
                    # by _on_precache_done when the job finishes.
                    log.debug("tray: resume requested while precache runs — "
                              "the loop resumes afterwards")
                else:
                    ticker.start()
                    log.info("tray: RESUMED")

        app._tray = Tray(app, on_pause=_on_tray_pause, on_quit=app.quit, parent=app)
        app._tray.start()

    log.info("daemon: running (odds=1/%s, tick=%ss)",
             config["odds"], config["tick_seconds"])
    if not pool:
        log.warning("daemon: no media found - chance loop will idle "
                    "(run scripts/make_test_asset.py or drop files in media/)")


def _kill_switch(app, config: dict) -> None:
    """Dead man's switch fired: notify the user, then quit the app.

    The notification is rendered by a detached helper process (see
    dyst/notify.py) so it stays visible after the app exits. Silenced by
    config `kill_notify: false`.
    """
    hotkey = config.get("kill_hotkey", "") or "hotkey"
    log.warning("main: kill switch pressed (%s) — terminating", hotkey)
    if config.get("kill_notify", True):
        notify.show("DYST — kill switch",
                    f"Kill switch pressed ({hotkey}). Overlays stopped, app closed.")
    app.quit()


def _run_qt(config: dict, args) -> int:
    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv[:1])

    # Global hotkey kill switch (dead man's switch): register if configured.
    # Empty string = disabled. Windows only (no-op on other platforms).
    kill_hotkey = config.get("kill_hotkey", "")
    hotkey_filter = HotkeyFilter()
    # On non-Windows, install the native event filter (no-op on Windows —
    # the hotkey is polled via QTimer instead).
    if sys.platform != "win32":
        app.installNativeEventFilter(hotkey_filter)
    _hotkey_registered = register_hotkey(kill_hotkey, lambda: _kill_switch(app, config))
    if _hotkey_registered:
        log.info("main: kill hotkey active (%s)", kill_hotkey)

    # Out-of-process watchdog: the poller above lives on the GUI thread, so
    # it never fires when the app is wedged (stuck overlay, hung decode).
    # This detached process watches the same combo and force-terminates us
    # if we don't exit within the grace period (see dyst/killswitch.py).
    _watchdog_started = False
    if _hotkey_registered:
        _watchdog_started = killswitch.start(
            kill_hotkey, notify_enabled=config.get("kill_notify", True))
        if _watchdog_started:
            log.info("main: kill-switch watchdog active (force-kill after %ss "
                     "if unresponsive)", killswitch.GRACE_SECONDS)

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
                for k in ("image_display_seconds", "fade_out_seconds", "fade_in_seconds",
                          "mode", "volume", "weight", "opacity", "chroma",
                          "position_x", "position_y", "scale_x", "scale_y", "scale",
                          "flip_h", "flip_v", "rotation", "max_duration",
                          "speed", "pitch", "speed_pitch",
                          "chroma_hue_range", "chroma_saturation_range", "chroma_value_range",
                          "max_playback_height", "max_playback_fps",
                          "end_on_audio_end"):
                    if k in raw and raw[k] is not None:
                        existing[k] = raw[k]
            break  # Found settings, stop looking
        win = _spawn_overlay(config, item, pre=True)
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
        win = _spawn_overlay(config, item, pre=True)
        if win is None:
            return 1
        win.finished.connect(app.quit)
        log.info("test: playing %s (%s)", item.path, item.kind)

    elif args.daemon:
        _run_daemon(app, config, use_tray=not getattr(args, "no_tray", False))

    try:
        return app.exec()
    finally:
        # Tear the tray icon down first so a leftover icon can never outlive
        # the process (daemon mode only; None otherwise).
        tray = getattr(app, "_tray", None)
        if tray is not None:
            tray.stop()
        # Cleanup hotkey on exit — MUST run after the event loop, otherwise
        # the kill switch unregisters itself before it can ever fire.
        if _hotkey_registered:
            unregister_hotkey()
            hotkey_filter.setEnabled(False)
        # The watchdog exits by itself when this process dies; terminate it
        # now anyway so a normal quit never leaves a stray helper behind.
        if _watchdog_started:
            killswitch.stop()


def main(argv=None) -> int:
    _reconfigure_console()
    parser = argparse.ArgumentParser(prog="dyst", description=f"{cfg.APP_NAME}")
    parser.add_argument("--test", action="store_true",
                        help="play one random media file, then exit")
    parser.add_argument("--play", metavar="PATH", help="play a specific media file, then exit")
    parser.add_argument("--daemon", action="store_true",
                        help="run the chance loop (ticker) with a tray icon")
    parser.add_argument("--no-tray", action="store_true",
                        help="run the daemon without the tray icon (headless/debug)")
    parser.add_argument("--roll", action="store_true",
                        help="simulate one roll and print the result")
    # Internal: detached notification helper (see dyst/notify.py), used so a
    # notification can outlive the app (kill switch). Not for human use.
    parser.add_argument("--notify", nargs=3, metavar=("TITLE", "MESSAGE", "TIMEOUT_MS"),
                        help=argparse.SUPPRESS)
    # Internal: detached kill-switch watchdog (see dyst/killswitch.py). The
    # separate process must be able to kill us even when we are wedged.
    parser.add_argument("--killwatchdog", nargs=4,
                        metavar=("HOTKEY", "PID", "GRACE_MS", "NOTIFY"),
                        help=argparse.SUPPRESS)
    parser.add_argument("--autostart", action="store_true",
                        help="(written by the app itself into the Windows Run key) "
                             "marks a boot-launch so the app can self-remove when config says off")
    default_config_path = os.path.join(get_base_dir(), "config.json")
    parser.add_argument("--config", default=default_config_path, help="path to config file")
    args = parser.parse_args(argv)

    if args.killwatchdog:
        hotkey, pid, grace_ms, notify_flag = args.killwatchdog
        try:
            grace = float(grace_ms) / 1000.0
            parent_pid = int(pid)
        except ValueError:
            print("ERROR: bad --killwatchdog arguments")
            return 1
        return killswitch.run(hotkey, parent_pid, grace,
                              str(notify_flag) not in ("0", "false", "False"))

    if args.notify:
        try:
            timeout_ms = int(args.notify[2])
        except ValueError:
            timeout_ms = notify.DEFAULT_TIMEOUT_MS
        return 0 if notify.show_blocking(args.notify[0], args.notify[1], timeout_ms) else 1

    config = cfg.load_config(args.config)
    _apply_console_mode(config)
    _setup_logging(config["debug"])
    log.info("%s starting (version %s)", cfg.APP_NAME, __version__)
    log.debug("loaded config: %r", config)

    if args.roll:
        chance = 1.0 / config["odds"]
        result = random.random() < chance
        print(f"roll: odds=1/{config['odds']} chance={chance:.6f} "
              f"result={'SUCCESS' if result else 'fail'}")
        return 0

    # for pyinstaller to run on daemon automatically (and --autostart alone
    # boots the daemon too — the flag only marks a registry-boot launch)
    if not (args.test or args.play or args.daemon or args.roll or args.autostart):
        args.daemon = True
    if args.autostart and not (args.test or args.play or args.roll):
        args.daemon = True

    if args.test or args.play or args.daemon:
        # Autostart sync (Windows Run key) — daemon only, so one-shot
        # --roll/--play/--test runs never touch the registry. When this
        # returns False the app has already unregistered + stopped.
        if args.daemon and not _handle_autostart(config, args):
            return 0
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