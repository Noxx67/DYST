"""Tray icon (Pause/Resume + Quit) verification — offscreen-safe.

Run:  python scripts/test_tray.py
Exits 0 if all checks pass.

Covers the logic that does NOT need a real click on the tray:
- the icon asset loads (or a fallback image is drawn)
- a menu toggle flips the pause state and the "Pause"/"Resume" label
- the GUI-thread poll applies pause/quit to the callbacks exactly once each
- a real pystray icon can be created and stopped (SKIP when no tray exists)

The actual click -> flag -> callback plumbing is what keeps the tray thread
from touching Qt, so it is the important part to test.
"""

from __future__ import annotations

import os
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from dyst import tray as tray_mod  # noqa: E402

app = QApplication(sys.argv[:1])


class _DummyIcon:
    """Stands in for pystray.Icon in the callback tests."""

    def __init__(self):
        self.title = ""
        self.updates = 0

    def update_menu(self):
        self.updates += 1


def main() -> int:
    # 1. icon asset (or drawn fallback) always yields a usable image
    img = tray_mod._load_icon_image()
    w_px, h_px = img.size
    assert w_px > 0 and h_px > 0, "icon image is empty"
    print(f"PASS icon image loaded ({w_px}x{h_px})")

    events = []
    t = tray_mod.Tray(app, on_pause=lambda p: events.append(("pause", p)),
                      on_quit=lambda: events.append(("quit",)))

    # 2. toggle flips state + label (callbacks run on the pystray thread)
    icon = _DummyIcon()
    assert t._pause_text() == "Pause"
    t._menu_toggle(icon, None)
    assert t.paused is True, "toggle did not pause"
    assert t._pause_text() == "Resume", "menu label did not change"
    assert icon.updates == 1 and "paused" in icon.title, (icon.title, icon.updates)
    print("PASS toggle: state + 'Resume' label + icon title updated")

    # 3. the GUI poll applies the pause exactly once
    t._poll()
    assert events == [("pause", True)], events
    t._poll()  # idempotent: no second callback for the same state
    assert events == [("pause", True)], events
    print("PASS poll: pause applied once (idempotent)")

    # 4. toggling back resumes
    t._menu_toggle(icon, None)
    assert t.paused is False
    t._poll()
    assert events == [("pause", True), ("pause", False)], events
    print("PASS poll: resume applied")

    # 5. set_paused (programmatic) goes through the same path
    t.set_paused(True)
    t._poll()
    assert events[-1] == ("pause", True), events
    t.set_paused(False)
    t._poll()
    assert events[-1] == ("pause", False), events
    print("PASS programmatic set_paused")

    # 6. quit flag -> quit callback (tray stopped first)
    t._menu_quit(None)
    t._poll()
    assert events[-1] == ("quit",), events
    assert t.started is False
    print("PASS quit: callback fired and tray stopped")

    # 7. a REAL pystray icon can start and stop (skipped without a tray)
    real = tray_mod.Tray(app)
    ok = real.start()
    if ok:
        time.sleep(0.3)
        assert real.started
        real.stop()
        assert not real.started
        print("PASS real tray icon: started and stopped")
    else:
        print("SKIP real tray icon (pystray unavailable in this environment)")

    # 8. integration: the real daemon wiring pauses/resumes the ticker
    import main as main_mod
    from dyst import config as cfg

    dcfg = dict(cfg.DEFAULTS)
    dcfg["media_folder"] = os.path.join(ROOT, "media", "nonexistent_tray_pool")
    dcfg["tick_seconds"] = 100000.0   # never actually fires during the test
    dcfg["odds"] = 10 ** 9
    dcfg["rescan_seconds"] = 0
    quit_calls = []
    orig_quit = app.quit
    app.quit = lambda: quit_calls.append(True)   # observe the tray's Quit path
    try:
        main_mod._run_daemon(app, dcfg, use_tray=True)
        assert app._tray is not None and app._tray.started, "daemon did not create a tray"
        assert app._ticker._timer.isActive(), "ticker should be running"
        app._tray._menu_toggle(_DummyIcon(), None)      # click Pause
        app._tray._poll()
        assert not app._ticker._timer.isActive(), "Pause did not stop the ticker"
        app._tray._menu_toggle(_DummyIcon(), None)      # click Resume
        app._tray._poll()
        assert app._ticker._timer.isActive(), "Resume did not restart the ticker"
        app._tray._menu_quit(None)                       # click Quit
        app._tray._poll()
        assert quit_calls == [True], quit_calls
        assert app._tray.started is False, "tray should stop on quit"
    finally:
        app.quit = orig_quit
    app._ticker.stop()
    print("PASS integration: tray Pause/Resume stops/starts the daemon ticker; "
          "Quit calls app.quit")

    print("\nAll tray checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())