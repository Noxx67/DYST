"""tray — system tray icon for the DYST daemon (Pause/Resume + Quit).

The app runs with no window, so this is the only visible handle on it: a
hidden tray icon whose right-click menu can

  * **Pause / Resume** the chance loop (paused = no media can appear; any
    overlay currently on screen is dismissed immediately), and
  * **Quit** the app entirely (same clean shutdown as the kill switch).

Threading note: pystray runs its own message loop on a daemon thread and its
menu callbacks run on THAT thread. Qt objects may only be touched from the GUI
thread, so the callbacks never call Qt (or the ticker) directly — they only
flip plain flags, and a QTimer on the GUI thread applies them via the
`on_pause` / `on_quit` callbacks. That keeps the tray safe to click at any
time, even right after startup.
"""

from __future__ import annotations

import logging
import os
import threading

from PySide6.QtCore import QObject, QTimer

from dyst.config import get_base_dir

try:
    import pystray
except Exception:  # pragma: no cover - optional dependency
    pystray = None

log = logging.getLogger("dyst.tray")

POLL_MS = 150          # GUI-thread poll interval (how fast a click is applied)
ICON_ID = "dyst"


def _fallback_image():
    """Draw a simple icon so the tray still works without icon.webp."""
    from PIL import Image, ImageDraw

    size = 64
    img = Image.new("RGBA", (size, size), (24, 24, 32, 255))
    d = ImageDraw.Draw(img)
    # A cartoon "eye" — recognisable and cheap, no font/asset dependency.
    d.ellipse((6, 16, 58, 48), fill=(240, 240, 245, 255))
    d.ellipse((20, 22, 44, 42), fill=(60, 130, 220, 255))
    d.ellipse((28, 28, 36, 36), fill=(20, 20, 28, 255))
    return img


def _load_icon_image():
    """Return a PIL image for the tray icon.

    Prefers an embedded icon generated at build time so the tray works
    without icon.ico/icon.webp next to the exe. Falls back to assets
    beside the executable/root, then to a drawn fallback so the tray
    never fails just because an asset is missing.
    """
    from PIL import Image
    import sys

    if getattr(sys, "frozen", False):
        try:
            from dyst import _embedded_icon
            import base64, io
            return Image.open(io.BytesIO(base64.b64decode(_embedded_icon.ICON_BASE64))).convert("RGBA")
        except Exception:
            pass

    for name in ("icon.ico", "icon.webp", "icon.png"):
        path = os.path.join(get_base_dir(), name)
        if not os.path.isfile(path):
            continue
        try:
            return Image.open(path).convert("RGBA")
        except Exception as exc:
            log.debug("tray: could not load %s (%s)", path, exc)
    log.debug("tray: no icon asset found — using the drawn fallback")
    return _fallback_image()


class Tray(QObject):
    """Tray icon driving the daemon through GUI-thread callbacks.

    on_pause(paused: bool) and on_quit() are called on the GUI thread by the
    internal poll timer, so the caller may safely stop/start the ticker or
    quit the app from them. `start()` returns False (and logs) when pystray is
    unavailable or the icon cannot be created — the app keeps running either
    way.
    """

    def __init__(self, app, on_pause=None, on_quit=None, parent=None):
        super().__init__(parent)
        self._app = app
        self._on_pause = on_pause
        self._on_quit = on_quit
        self._icon = None
        self._thread = None
        self._started = False
        # Written by the pystray thread, read/applied by the GUI poll timer.
        self._paused = False
        self._applied = False
        self._quit_requested = False

        self._timer = QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self._poll)

    # -- state -------------------------------------------------------------

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def started(self) -> bool:
        return self._started

    def set_paused(self, paused: bool) -> None:
        """Set the pause state programmatically (applied on the next poll)."""
        self._paused = bool(paused)

    # -- pystray callbacks (run on the pystray thread!) --------------------

    def _pause_text(self, _item=None) -> str:
        """Menu label: the action the click will perform."""
        return "Resume" if self._paused else "Pause"

    def _menu_toggle(self, icon, _item=None):
        self._paused = not self._paused
        try:
            icon.title = f"DYST — {'paused' if self._paused else 'running'}"
            icon.update_menu()
        except Exception:
            log.debug("tray: could not refresh the menu", exc_info=True)

    def _menu_quit(self, _icon=None, _item=None):
        self._quit_requested = True

    # -- GUI thread --------------------------------------------------------

    def _poll(self) -> None:
        """Apply a click made on the pystray thread (runs on the GUI thread)."""
        if self._quit_requested:
            self._quit_requested = False
            self.stop()
            if self._on_quit is not None:
                try:
                    self._on_quit()
                except Exception:
                    log.warning("tray: quit callback failed", exc_info=True)
            return
        if self._paused != self._applied:
            self._applied = self._paused
            if self._on_pause is not None:
                try:
                    self._on_pause(self._paused)
                except Exception:
                    log.warning("tray: pause callback failed", exc_info=True)

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> bool:
        """Create and run the tray icon. Never raises; False = no tray."""
        log.info("tray: start() invoked (pystray=%s)", pystray is not None)
        if self._started:
            return True
        if pystray is None:
            log.warning("tray: pystray is not installed — running without a tray icon")
            return False
        try:
            menu = pystray.Menu(
                pystray.MenuItem(self._pause_text, self._menu_toggle, default=True),
                pystray.MenuItem("Quit", self._menu_quit),
            )
            self._icon = pystray.Icon(ICON_ID, _load_icon_image(),
                                      "DYST — running", menu)
            # pystray owns a message loop; keep it off the Qt thread.
            self._thread = threading.Thread(target=self._icon.run,
                                            name="dyst-tray", daemon=True)
            self._thread.start()
            log.info("tray: pystray thread started (alive=%s)", self._thread.is_alive())
            self._timer.start()
            self._started = True
            log.info("tray: icon active (right-click for Pause / Quit)")
            return True
        except Exception:
            log.warning("tray: could not start the tray icon", exc_info=True)
            self._icon = None
            return False

    def stop(self) -> None:
        """Tear the icon down. Safe to call more than once."""
        self._timer.stop()
        self._started = False
        icon, self._icon = self._icon, None
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                log.debug("tray: icon.stop failed", exc_info=True)