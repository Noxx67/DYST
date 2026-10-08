"""Overlay playback stress bench (dev use).

Spawns N overlays at the same instant (simulates a same-tick burst), then
measures the things a user actually feels:

  * GUI event-loop lag  — a 16 ms heartbeat QTimer; overshoot p50/p95/max
    and stall counts (>50 ms, >100 ms). High = whole app stutters.
  * per-overlay presented-frame rate — count of `_prepare_current` calls
    (called once per presented frame on every playback path) vs the
    effective fps the media should run at.
  * GUI hot-method time — wall time spent inside `_prepare_current`,
    `_on_qt_frame`, `_next_frame`, `paintEvent` (instrumented by wrapping
    the class methods; no production code changes).
  * spawn cost — time each `_spawn_overlay` call takes (burst stall).

Usage:
  python scripts/bench_overlay.py [--n 8] [--seconds 8] [--seed 1] [--offscreen]
                                  [--media "sub\\string"] [--list]

Windows GUI session = real numbers. --offscreen for headless smoke runs.
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import numpy as np  # noqa: E402  (bench stats helper)

# Per-overlay instrumentation records, keyed by id(overlay).
_PER: dict[int, dict] = {}
_WINS: list = []   # live overlays for the per-second internals probe
_PROBE = [-1]      # last probe second


def _rec(win) -> dict:
    r = _PER.get(id(win))
    if r is None:
        r = {"path": os.path.basename(getattr(win, "_path", "?") or "?"),
             "kind": getattr(win, "_kind", "?"),
             "prepare": 0, "t_prepare": 0.0,
             "qt": 0, "t_qt": 0.0,
             "next": 0, "t_next": 0.0,
             "paint": 0, "t_paint": 0.0,
             "t_spawn": time.perf_counter(), "t_end": None}
        _PER[id(win)] = r
    return r


def _timed(attr: str, counter: str, seconds_attr: str | None = None):
    """Wrap an OverlayWindow method: count calls + accumulate duration."""
    def deco(fn):
        def wrap(self, *a, **k):
            t0 = time.perf_counter()
            try:
                return fn(self, *a, **k)
            finally:
                dt = time.perf_counter() - t0
                r = _rec(self)
                r[counter] += 1
                if seconds_attr:
                    r[seconds_attr] += dt
        wrap.__name__ = fn.__name__
        return wrap
    return deco


def instrument() -> None:
    from dyst.overlay import OverlayWindow
    OverlayWindow._prepare_current = _timed("prepare", "prepare", "t_prepare")(
        OverlayWindow._prepare_current)
    OverlayWindow._on_qt_frame = _timed("qt", "qt", "t_qt")(
        OverlayWindow._on_qt_frame)
    OverlayWindow._next_frame = _timed("next", "next", "t_next")(
        OverlayWindow._next_frame)
    OverlayWindow.paintEvent = _timed("paint", "paint", "t_paint")(
        OverlayWindow.paintEvent)


def pct(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    return float(np.percentile(np.asarray(xs), p))


def main() -> int:
    ap = argparse.ArgumentParser(description="DYST overlay stress bench")
    ap.add_argument("--n", type=int, default=8, help="overlays to spawn at once")
    ap.add_argument("--seconds", type=float, default=8.0, help="measurement window")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--offscreen", action="store_true", help="QT_QPA_PLATFORM=offscreen")
    ap.add_argument("--media", default="", help="substring filter on media path")
    ap.add_argument("--list", action="store_true", help="list scanned media and exit")
    args = ap.parse_args()

    if args.offscreen:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from dyst import config as cfg
    from dyst import media as media_mod
    import main as app_main

    config = cfg.load_config(os.path.join(ROOT, "config.json"))
    config["volume"] = 0.0  # mute: bench, not a listening session
    media_root = os.path.join(ROOT, config.get("media_folder", "media"))
    items = media_mod.scan(media_root)
    if args.media:
        items = [i for i in items if args.media.lower() in i.path.lower()]
    # Heaviest first: big files = the expensive decode/scale cases.
    items.sort(key=lambda i: os.path.getsize(i.path), reverse=True)
    if args.list:
        for i in items:
            print(f"{os.path.getsize(i.path):>10}  {i.kind:5}  "
                  f"{os.path.relpath(i.path, ROOT)}")
        return 0
    if not items:
        print("no media found under", media_root)
        return 1

    rng = random.Random(args.seed)
    picks = [items[i % len(items)] for i in range(args.n)]
    rng.shuffle(picks)

    instrument()
    app = QApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(False)

    # --- heartbeat: measure GUI event-loop lag -------------------------
    lags: list[float] = []
    stalls: list[tuple[float, float]] = []   # (t_since_burst_s, lag_ms)
    last = [time.perf_counter()]
    beat = QTimer()
    beat.setInterval(16)
    _T_BURST = [time.perf_counter()]
    def on_beat():  # noqa: N806
        now = time.perf_counter()
        lag = (now - last[0]) * 1000.0 - 16.0
        lags.append(lag)
        if lag > 40:
            stalls.append((now - _T_BURST[0], lag))
        last[0] = now
    beat.timeout.connect(on_beat)
    beat.start()

    # --- burst spawn ---------------------------------------------------
    spawn_times: list[tuple[str, float, object]] = []
    t_burst0 = time.perf_counter()
    for item in picks:
        t0 = time.perf_counter()
        win = app_main._spawn_overlay(config, item, pre=True)
        spawn_times.append((os.path.basename(item.path),
                            (time.perf_counter() - t0) * 1000.0, win))
    burst_ms = (time.perf_counter() - t_burst0) * 1000.0
    n_ok = sum(1 for _, _, w in spawn_times if w is not None)
    globals()['_WINS'][:] = [w for _, _, w in spawn_times if w is not None]
    # track each overlay's lifetime: reported fps must be over ALIVE time
    # (many sidecars randomize max_duration to 0.3-0.7s — dying on time is
    # design, not lag)
    for _, _, w in spawn_times:
        if w is not None:
            rec = _rec(w)
            w.finished.connect(
                lambda _r=rec: _r.__setitem__("t_end", time.perf_counter()))

    # --- measurement window --------------------------------------------
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < args.seconds:
        app.processEvents()
        time.sleep(0.002)
        # per-second probe of video overlay internals
        if int(time.perf_counter() - t0) > _PROBE[0]:
            _PROBE[0] = int(time.perf_counter() - t0)
            for w in list(_WINS):
                try:
                    if not str(getattr(w, "_kind", "")).startswith("video"):
                        continue
                    ap = (w._audio_player.playbackState()
                          if w._audio_player else None)
                    pos = (w._audio_player.position()
                           if w._audio_player else None)
                    q = (w._frame_queue.qsize()
                         if hasattr(w, "_frame_queue") else "?")
                    print(f"   [probe t={_PROBE[0]}s] closing={w._closing} "
                          f"vis_done={w._visual_done} aud_done={w._audio_done} "
                          f"{w._path[-28:]} "
                          f"kind={w._kind} timer={'ON' if w._video_timer.isActive() else 'off'}"
                          f" iv={w._video_timer.interval()}ms q={q} "
                          f"presented={w._presented} audio_state={ap} "
                          f"audio_pos={pos} pending={w._video_clock_pending} "
                          f"budget={w._decode_budget} "
                          f"prod={getattr(w, '_produced', '?')}")
                except RuntimeError:
                    pass  # C++ side already deleted
    elapsed = time.perf_counter() - t0
    beat.stop()

    # --- report ---------------------------------------------------------
    print()
    print(f"=== DYST overlay bench  n={n_ok}  window={elapsed:.1f}s  "
          f"mode={config.get('mode')} chroma={config.get('chroma_key')!r} "
          f"caps={config.get('max_playback_height')}px/"
          f"{config.get('max_playback_fps')}fps ===")
    print(f"burst spawn total: {burst_ms:.0f} ms for {len(spawn_times)} overlays "
          f"(worst single {max((m for _, m, _ in spawn_times), default=0):.0f} ms)")
    for name, ms, win in spawn_times:
        flag = "OK " if win is not None else "FAIL"
        print(f"  [{flag}] {ms:6.0f} ms  {name[:60]}")

    if stalls:
        print("stalls >40ms (t_from_burst s, lag ms): "
              + "  ".join(f"{t:.1f}/{l:.0f}" for t, l in stalls))
    if lags:
        arr = np.asarray(lags)
        print(f"\nGUI heartbeat lag (target 16 ms): mean {arr.mean():.1f}  "
              f"p50 {pct(lags,50):.1f}  p95 {pct(lags,95):.1f}  "
              f"max {arr.max():.1f} ms | stalls >50ms: {(arr>50).sum()}  "
              f">100ms: {(arr>100).sum()}")

    print("\nper-overlay presentation (fps over ALIVE time; paint = presented frame):")
    tot_gui = 0.0
    for r in _PER.values():
        t_end = r["t_end"] or (t0 + elapsed)
        alive = max(0.001, t_end - r["t_spawn"])
        presented = r["paint"]
        fps = presented / alive
        gui_ms = (r["t_prepare"] + r["t_qt"] + r["t_next"] + r["t_paint"]) * 1000.0
        tot_gui += gui_ms
        avg_prep = (r["t_prepare"] * 1000.0 / r["prepare"]) if r["prepare"] else 0.0
        print(f"  {r['path'][:50]:<50} {r['kind']:<19} "
              f"alive={alive:4.1f}s presented={presented:>4} ({fps:5.1f} fps)  "
              f"gui={gui_ms:6.0f}ms  prepare_calls={r['prepare']:>4} "
              f"avg={avg_prep:5.2f}ms")
    busy = 100.0 * tot_gui / 1000.0 / elapsed if elapsed else 0.0
    print(f"\nGUI hot-method busy time: {tot_gui/1000.0:.2f}s / {elapsed:.1f}s "
          f"= {busy:.1f}% of the window (100% = GUI saturated = lag)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
