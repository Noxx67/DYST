"""autostart — Windows Run-key registration, driven by `config.json`.

The `autostart` key in config.json is the SINGLE source of truth:

  true  -> every launch (boot or manual) (re)registers this app in the
           Windows login Run key — Windows then starts it at every login —
           and the app keeps running silently.
  false -> a BOOT launch (Windows started us via the Run key; we know
           because the registered command carries the `--autostart` flag)
           removes the Run key and the app stops immediately. A MANUAL
           launch with the key still present just clears the stale key and
           keeps running.

Windows-only (`os.name == "nt"`); everywhere else every call is a safe no-op.
"""

from __future__ import annotations

import logging
import os
import sys

log = logging.getLogger("dyst.autostart")

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUN_VALUE = "DYST"
AUTOSTART_FLAG = "--autostart"


def _registry():
    """winreg accessor or None (non-Windows / import failure)."""
    if os.name != "nt":
        return None
    try:
        import winreg
        return winreg
    except OSError:  # pragma: no cover - Win32-only edge
        return None


def _command() -> str:
    """The exact command we register (always carries the --autostart flag so
    a boot launch is distinguishable from a manual one).

    Frozen exe:  "C:\\...\\DYST.exe" --autostart
    Dev:         "C:\\...\\python.exe" "C:\\...\\main.py" --autostart
    """
    from dyst.config import get_base_dir

    if getattr(sys, "frozen", False):
        return '"%s" %s' % (sys.executable, AUTOSTART_FLAG)
    main = os.path.join(get_base_dir(), "main.py")
    return '"%s" "%s" %s' % (sys.executable, main, AUTOSTART_FLAG)


def get_command() -> str | None:
    """The stored Run value for this app, or None when not registered."""
    reg = _registry()
    if reg is None:
        return None
    try:
        with reg.OpenKey(reg.HKEY_CURRENT_USER, _RUN_KEY, 0, reg.KEY_READ) as key:
            value, _ = reg.QueryValueEx(key, _RUN_VALUE)
        return value if isinstance(value, str) else None
    except OSError:
        return None


def is_enabled() -> bool:
    """True when the Run key for this app currently exists."""
    return get_command() is not None


def enable() -> bool:
    """Register (or refresh) the Run key so the app starts with Windows.

    Idempotent — call on every launch while config `autostart` is true so a
    moved/updated executable always points at the current path.
    """
    reg = _registry()
    if reg is None:
        log.warning("autostart: not supported on this OS")
        return False
    try:
        with reg.OpenKey(reg.HKEY_CURRENT_USER, _RUN_KEY, 0, reg.KEY_SET_VALUE) as key:
            reg.SetValueEx(key, _RUN_VALUE, 0, reg.REG_SZ, _command())
        log.debug("autostart: registered Run key -> %s", _command())
        return True
    except OSError as exc:
        log.warning("autostart: could not write the Run key (%s)", exc)
        return False


def disable() -> bool:
    """Remove the Run key. No-op (True) when it was never set."""
    reg = _registry()
    if reg is None:
        return False
    try:
        with reg.OpenKey(reg.HKEY_CURRENT_USER, _RUN_KEY, 0, reg.KEY_SET_VALUE) as key:
            reg.DeleteValue(key, _RUN_VALUE)
        log.debug("autostart: removed Run key")
        return True
    except FileNotFoundError:
        return True  # already gone
    except OSError as exc:
        log.warning("autostart: could not remove the Run key (%s)", exc)
        return False