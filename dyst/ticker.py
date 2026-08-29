"""DYST (did you see that? 👀) — core chance loop (Phase 1).

Minimal spec-compliant ticker: rolls 1-in-N every `tick_seconds`; on success
the (injected) picker picks a media item and the (injected) spawner is called
with it; then it re-rolls immediately in the same tick (burst) until a fail
or the per-tick cap `max_concurrent` (0 = unlimited).

NOTE: global concurrent-overlay enforcement is the manager's job (Phase 5);
the ticker only caps re-rolls *within* one tick.
"""

from __future__ import annotations

import logging
import random

from typing import Any


try:
    from PySide6.QtCore import QTimer
except ImportError:
    # Fallback stub for environments without PySide6 (e.g., headless tests)
    class _DummySignal:
        def connect(self, _):
            pass

    class _DummyTimer:
        def __init__(self, parent=None):
            self._interval = 1000
            self._parent = parent
            self.timeout = _DummySignal()
        def setInterval(self, ms: int):
            self._interval = ms
        def start(self):
            pass
        def stop(self):
            pass
    QTimer = _DummyTimer


log = logging.getLogger("dyst.ticker")


class Ticker:
    def __init__(self, picker, spawner, cfg: dict, parent=None):
        self.picker = picker    # () -> MediaItem | None
        self.spawner = spawner  # (MediaItem) -> bool : True if overlay spawned
        self.cfg = cfg
        # Ensure max_concurrent is an integer >=0; fallback to default 3 on bad config
        self._max_concurrent = self._parse_max_concurrent(self.cfg.get("max_concurrent", 3))
        self._timer = QTimer(parent)
        self._timer.timeout.connect(self.tick)
        self.set_tick_seconds(cfg.get("tick_seconds", 1.0))
        self._tick_count = 0


    def set_tick_seconds(self, seconds: float) -> None:
        """Reconfigure the timer interval from config (min 1 ms)."""
        self._timer.setInterval(max(1, int(seconds * 1000)))

    def start(self) -> None:
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    @property
    def tick_count(self) -> int:
        return self._tick_count

    def roll(self) -> bool:
        """True if this roll succeeds: random < 1/odds."""
        odds = max(1, int(self.cfg.get("odds", 1000)))
        roll_num = random.random()
        print(f"rolled: {roll_num}")
        return roll_num < 1.0 / odds

    def tick(self) -> None:
        """One timer tick: roll once, then burst on success up to the cap."""
        self._tick_count += 1
        log.debug("ticker tick %d (odds=1/%s)", self._tick_count, self.cfg.get('odds', 1000))
        if not self._roll_once():
            return
        cap = self._max_concurrent

        spawned = 0
        while True:
            log.debug("tick %d: roll SUCCESS", self._tick_count)
            item = self.picker()
            if item is None:
                log.debug("tick %d: no media to play", self._tick_count)
                break
            if not self.spawner(item):
                break  # spawn refused / load failure
            spawned += 1
            if cap != 0 and spawned >= cap:
                break
            if not self._roll_once():
                break  # burst ends when the next roll fails

    def _roll_once(self) -> bool:
        """Internal helper for a single roll, used by tick logic."""
        return self.roll()

    def _parse_max_concurrent(self, value: Any) -> int:
        """Validate max_concurrent config value.
        Returns a non‑negative integer; falls back to default 3 on invalid.
        """
        try:
            iv = int(value)
            if iv >= 0:
                return iv
        except Exception:
            pass
        log.warning("ticker: invalid max_concurrent %r – using default 3", value)
        return 3

