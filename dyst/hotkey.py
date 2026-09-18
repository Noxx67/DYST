"""DYST — global hotkey kill switch (dead man's switch).

Registers a configurable global hotkey via Win32 RegisterHotKey.
When pressed, fires a callback (typically terminating the app).
This is a safety net for when overlays go haywire and the tray
menu is unreachable.

Windows only; no-op on other platforms.

Usage:
    from dyst.hotkey import register_hotkey, unregister_hotkey

    # In your Qt app startup:
    hotkey_filter = HotkeyFilter()  # placeholder, no-op on Windows
    app.installNativeEventFilter(hotkey_filter)  # optional, no-op on Windows
    register_hotkey(config["kill_hotkey"], app.quit)

    # On shutdown:
    unregister_hotkey()
    hotkey_filter.setEnabled(False)

How it works (Windows):
    Uses Win32 RegisterHotKey to reserve the combo at the OS level
    (prevents other apps from capturing it), plus a QTimer that
    polls GetAsyncKeyState every 50ms with edge detection. This
    avoids PySide6 native event filter reliability issues.

    Because that QTimer lives on the GUI thread it cannot fire while
    the app is wedged. `dyst/killswitch.py` therefore runs a second,
    independent 50 ms poll in a detached process and force-terminates
    the app if it does not quit within a short grace period — see
    `killswitch.start()`.
"""

from __future__ import annotations

import logging
import sys

log = logging.getLogger("dyst.hotkey")

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    from PySide6.QtCore import QTimer

    WM_HOTKEY = 0x0312
    MOD_ALT = 0x0001
    MOD_CONTROL = 0x0002
    MOD_SHIFT = 0x0004
    MOD_WIN = 0x0008

    # Hotkey parsing (modifier bits, VK codes) is shared with the
    # out-of-process watchdog — see dyst/killswitch.py.
    from dyst.killswitch import parse_hotkey  # noqa: F401

    _user32 = ctypes.windll.user32
    _HOTKEY_ID = 0x5542  # arbitrary unique ID for DYST

    # Internal state
    _registered = False
    _callback = None
    _timer = None
    _prev_keys = 0

    def register_hotkey(hotkey_str: str, callback) -> bool:
        """Register a global hotkey that fires *callback* when pressed.

        Returns True on success, False on failure (invalid string,
        hotkey already in use by another app, etc.).

        The callback is invoked on the GUI thread when the hotkey fires.
        """
        global _registered, _callback, _timer, _prev_keys

        if not hotkey_str or not hotkey_str.strip():
            log.info("hotkey: disabled (empty string)")
            return False

        try:
            mods, vk = parse_hotkey(hotkey_str)
        except ValueError as exc:
            log.warning("hotkey: invalid hotkey %r — %s", hotkey_str, exc)
            return False

        ok = _user32.RegisterHotKey(None, _HOTKEY_ID, mods, vk)
        if not ok:
            err = ctypes.GetLastError()
            log.warning("hotkey: RegisterHotKey failed (error %d) — "
                         "maybe already in use by another app", err)
            return False

        _registered = True
        _callback = callback
        log.info("hotkey: registered %r — press to kill", hotkey_str)

        # Cache the combo for the polling loop.
        _poll_keys._mods = mods
        _poll_keys._vk = vk

        # Start a QTimer that polls key state. RegisterHotKey reserves
        # the combo at the OS level so no other app can capture it; the
        # timer detects the press reliably regardless of Qt's event
        # routing. Uses edge detection (only fires once per press).
        _timer = QTimer()
        _timer.setInterval(50)  # 20 Hz — fast enough for key presses, minimal CPU
        _timer.timeout.connect(_poll_keys)
        _timer.start()
        _prev_keys = 0

        return True

    def unregister_hotkey() -> None:
        """Unregister the hotkey. Safe to call even if not registered."""
        global _registered, _callback, _timer, _prev_keys
        if _timer is not None:
            _timer.stop()
            _timer.deleteLater()
            _timer = None
        if _registered:
            _user32.UnregisterHotKey(None, _HOTKEY_ID)
            _registered = False
            _callback = None
            _prev_keys = 0
            log.info("hotkey: unregistered")

    def _poll_keys() -> None:
        """Poll GetAsyncKeyState for all keys in the combo.

        Fires the callback on the transition from "not all pressed" to
        "all pressed". Ignores repeats while held down.
        """
        global _prev_keys
        if not _registered or not _callback:
            return

        # Get the combo from the last successful registration
        # Parse once and cache — but we need the vk/mods. Store at register time.
        # Actually, store them in globals at register time.
        if not hasattr(_poll_keys, '_mods') or not hasattr(_poll_keys, '_vk'):
            return  # not initialized

        mods = _poll_keys._mods
        vk = _poll_keys._vk

        # Check each key: high bit = currently down
        keys_down = 0
        if mods & MOD_CONTROL:
            if _user32.GetAsyncKeyState(0x11) & 0x8000:  # VK_CONTROL
                keys_down |= MOD_CONTROL
        if mods & MOD_SHIFT:
            if _user32.GetAsyncKeyState(0x10) & 0x8000:  # VK_SHIFT
                keys_down |= MOD_SHIFT
        if mods & MOD_ALT:
            if _user32.GetAsyncKeyState(0x12) & 0x8000:  # VK_MENU
                keys_down |= MOD_ALT
        if mods & MOD_WIN:
            if _user32.GetAsyncKeyState(0x5B) & 0x8000 or _user32.GetAsyncKeyState(0x5C) & 0x8000:
                keys_down |= MOD_WIN
        if _user32.GetAsyncKeyState(vk) & 0x8000:
            keys_down |= 0xF000  # key slot

        # Edge detection: transition from not-all-pressed to all-pressed
        if keys_down == mods | 0xF000 and _prev_keys != mods | 0xF000:
            try:
                _callback()
            except Exception as exc:
                log.error("hotkey: callback error — %s", exc)
        _prev_keys = keys_down

    class HotkeyFilter:
        """Placeholder — kept for API compatibility.

        The polling approach in _poll_keys() replaces nativeEventFilter.
        This class exists so main.py can still create HotkeyFilter()
        and call setEnabled(False) on shutdown without errors.
        """

        def __init__(self):
            self._enabled = True

        def setEnabled(self, enabled: bool) -> None:
            self._enabled = enabled
