"""DYST — out-of-process kill-switch watchdog (dead man's switch).

The in-process hotkey poller in `dyst/hotkey.py` lives on the Qt GUI
thread, which makes it useless exactly when it matters most: the app is
wedged (stuck overlay, hung decoder, blocked main loop) and never runs a
single timer tick. This module is the independent second detector. It runs
in its own **detached process**, uses only Win32 ctypes (no Qt, no event
loop, no config), so it keeps polling GetAsyncKeyState even while the app
is completely unresponsive — and it can force-terminate the app.

Behaviour on hotkey press:
  1. A detached notification is spawned (so the user learns why it died).
  2. The app gets `grace_seconds` to quit on its own — the in-process poller
     normally sees the same press and exits cleanly, in which case the
     watchdog just notices the process is gone and exits too.
  3. Still alive after the grace period → TerminateProcess. Kernel-level,
     works on a hung process.

The watchdog also polls the app's process handle and exits by itself the
moment the app is gone, so it never lingers after a normal quit.

Windows only; `start()` is a no-op elsewhere.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time

log = logging.getLogger("dyst.killswitch")

# How long the app gets to shut down by itself before it is force-killed.
GRACE_SECONDS = 1.5
# Key-state polling interval inside the watchdog process.
POLL_SECONDS = 0.05

# Modifier bits (same values Win32 RegisterHotKey uses).
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008

VK_CODES = {
    'f1': 0x70, 'f2': 0x71, 'f3': 0x72, 'f4': 0x73,
    'f5': 0x74, 'f6': 0x75, 'f7': 0x76, 'f8': 0x77,
    'f9': 0x78, 'f10': 0x79, 'f11': 0x7A, 'f12': 0x7B,
    'a': 0x41, 'b': 0x42, 'c': 0x43, 'd': 0x44, 'e': 0x45,
    'f': 0x46, 'g': 0x47, 'h': 0x48, 'i': 0x49, 'j': 0x4A,
    'k': 0x4B, 'l': 0x4C, 'm': 0x4D, 'n': 0x4E, 'o': 0x4F,
    'p': 0x50, 'q': 0x51, 'r': 0x52, 's': 0x53, 't': 0x54,
    'u': 0x55, 'v': 0x56, 'w': 0x57, 'x': 0x58, 'y': 0x59, 'z': 0x5A,
    '0': 0x30, '1': 0x31, '2': 0x32, '3': 0x33, '4': 0x34,
    '5': 0x35, '6': 0x36, '7': 0x37, '8': 0x38, '9': 0x39,
}

VK_CONTROL = 0x11
VK_SHIFT = 0x10
VK_MENU = 0x12
VK_LWIN = 0x5B
VK_RWIN = 0x5C

NOTIFY_TITLE = "DYST — kill switch"
NOTIFY_MESSAGE = ("Kill switch forced the app closed (it was not responding). "
                  "Overlays stopped.")

# Detached-subprocess creation flags.
DETACHED_PROCESS = 0x00000008
CREATE_NO_WINDOW = 0x08000000
CREATE_BREAKAWAY_FROM_JOB = 0x01000000

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _user32 = ctypes.WinDLL("user32", use_last_error=True)

    SYNCHRONIZE = 0x00100000
    PROCESS_TERMINATE = 0x0001
    WAIT_OBJECT_0 = 0x00000000

    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.WaitForSingleObject.restype = wintypes.DWORD
    _kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _kernel32.TerminateProcess.restype = wintypes.BOOL
    _kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _user32.GetAsyncKeyState.restype = ctypes.c_short
    _user32.GetAsyncKeyState.argtypes = [ctypes.c_int]


def parse_hotkey(hotkey_str: str) -> tuple[int, int]:
    """Parse 'ctrl+shift+alt+k' into (modifier bitmask, virtual key code).

    Raises ValueError for anything unsupported.
    """
    parts = hotkey_str.strip().lower().split('+')
    if len(parts) < 2:
        raise ValueError("need at least a modifier + key (e.g. ctrl+k)")
    mods = 0
    for mod in parts[:-1]:
        mod = mod.strip()
        if mod == "ctrl":
            mods |= MOD_CONTROL
        elif mod == "shift":
            mods |= MOD_SHIFT
        elif mod == "alt":
            mods |= MOD_ALT
        elif mod in ("win", "super", "meta"):
            mods |= MOD_WIN
        else:
            raise ValueError(f"unknown modifier: {mod}")
    key = parts[-1].strip()
    vk = VK_CODES.get(key)
    if vk is None:
        raise ValueError(f"unsupported key: {key!r} (use a-z, 0-9, or f1-f12)")
    return mods, vk


# --------------------------------------------------------------------------
# Detection / killing (watchdog-side only)
# --------------------------------------------------------------------------

def _key_down(vk: int) -> bool:
    return bool(_user32.GetAsyncKeyState(vk) & 0x8000)


def combo_down(mods: int, vk: int) -> bool:
    """True when every key of the combo is currently held down."""
    if mods & MOD_CONTROL and not _key_down(VK_CONTROL):
        return False
    if mods & MOD_SHIFT and not _key_down(VK_SHIFT):
        return False
    if mods & MOD_ALT and not _key_down(VK_MENU):
        return False
    if mods & MOD_WIN and not (_key_down(VK_LWIN) or _key_down(VK_RWIN)):
        return False
    return _key_down(vk)


def _parent_exited(hproc) -> bool:
    if not hproc:
        return True
    return _kernel32.WaitForSingleObject(hproc, 0) == WAIT_OBJECT_0


def _terminate(pid: int, hproc, can_terminate: bool) -> bool:
    """Force-kill the app. TerminateProcess first, taskkill as fallback."""
    if can_terminate and hproc:
        if _kernel32.TerminateProcess(hproc, 1):
            return True
    try:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                       creationflags=CREATE_NO_WINDOW,
                       capture_output=True, timeout=5)
        return True
    except Exception as exc:
        log.warning("killswitch: taskkill fallback failed (%s)", exc)
        return False


def _fire(hotkey: str, pid: int, grace_seconds: float, notify_enabled: bool,
          hproc, can_terminate: bool) -> None:
    """Kill sequence: notify, wait for a graceful exit, then force."""
    _log(f"kill switch pressed ({hotkey}) — waiting {grace_seconds:g}s for a clean exit")
    if notify_enabled:
        try:
            from dyst import notify
            notify.show(NOTIFY_TITLE, NOTIFY_MESSAGE)
        except Exception as exc:  # a notification must never block the kill
            log.warning("killswitch: could not show notification (%s)", exc)

    deadline = time.monotonic() + max(0.0, grace_seconds)
    while time.monotonic() < deadline:
        if _parent_exited(hproc):
            _log("app exited cleanly on its own — watchdog done")
            return
        time.sleep(POLL_SECONDS)

    if _parent_exited(hproc):
        return
    if _terminate(pid, hproc, can_terminate):
        _log(f"app (pid {pid}) did not respond — force-terminated")
    else:
        _log(f"could not terminate app (pid {pid}) — kill it from Task Manager")


def run(hotkey: str, parent_pid: int, grace_seconds: float = GRACE_SECONDS,
        notify_enabled: bool = True) -> int:
    """Watchdog loop (runs in the detached process). Blocks until done."""
    try:
        mods, vk = parse_hotkey(hotkey)
    except ValueError as exc:
        _log(f"invalid kill hotkey {hotkey!r} ({exc}) — watchdog not started")
        return 1

    hproc = _kernel32.OpenProcess(SYNCHRONIZE | PROCESS_TERMINATE, False, parent_pid)
    can_terminate = bool(hproc)
    if not hproc:
        # No terminate rights (e.g. privilege mismatch) — watch only, and
        # fall back to taskkill if the press ever happens.
        hproc = _kernel32.OpenProcess(SYNCHRONIZE, False, parent_pid)
    if not hproc:
        _log(f"could not open app process {parent_pid} — watchdog exiting")
        return 1

    _log(f"kill-switch watchdog armed for pid {parent_pid} ({hotkey})")
    prev_down = False
    try:
        while True:
            if _parent_exited(hproc):
                _log("app exited — watchdog done")
                return 0
            down = combo_down(mods, vk)
            if down and not prev_down:
                _fire(hotkey, parent_pid, grace_seconds, notify_enabled,
                      hproc, can_terminate)
                if _parent_exited(hproc):
                    return 0
            prev_down = down
            time.sleep(POLL_SECONDS)
    finally:
        _kernel32.CloseHandle(hproc)


# --------------------------------------------------------------------------
# Spawning / stopping the watchdog (app-side API)
# --------------------------------------------------------------------------

_WATCHDOG = None


def _base_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _watchdog_argv(hotkey: str, pid: int, grace_ms: int, notify_enabled: bool) -> list:
    notify_flag = "1" if notify_enabled else "0"
    if getattr(sys, "frozen", False):
        return [sys.executable, "--killwatchdog", hotkey, str(pid),
                str(grace_ms), notify_flag]
    python = sys.executable
    pythonw = os.path.join(os.path.dirname(python), "pythonw.exe")
    if os.path.isfile(pythonw):
        python = pythonw
    return [python, "-m", "dyst.killswitch",
            "--hotkey", hotkey, "--parent", str(pid),
            "--grace-ms", str(grace_ms), "--notify", notify_flag]


def _spawn_detached(argv: list) -> subprocess.Popen:
    flags = DETACHED_PROCESS | CREATE_NO_WINDOW
    kwargs = {"cwd": _base_dir(), "close_fds": True,
              "creationflags": flags | CREATE_BREAKAWAY_FROM_JOB}
    try:
        return subprocess.Popen(argv, **kwargs)
    except OSError:
        # Breakaway is refused when the parent job forbids it — retry without.
        kwargs["creationflags"] = flags
        return subprocess.Popen(argv, **kwargs)


def start(hotkey: str, grace_seconds: float = GRACE_SECONDS,
          parent_pid: int | None = None, notify_enabled: bool = True) -> bool:
    """Spawn the detached watchdog for this app process.

    Returns True when a watchdog was launched. No-op (False) on non-Windows,
    for an empty/invalid hotkey, or if spawning fails.
    """
    global _WATCHDOG
    if sys.platform != "win32":
        return False
    if not hotkey or not hotkey.strip():
        return False
    try:
        parse_hotkey(hotkey)
    except ValueError as exc:
        log.warning("killswitch: invalid kill hotkey %r (%s) — no watchdog",
                    hotkey, exc)
        return False

    stop()  # never run two watchdogs at once
    pid = os.getpid() if parent_pid is None else int(parent_pid)
    grace_ms = max(0, int(round(grace_seconds * 1000)))
    try:
        _WATCHDOG = _spawn_detached(_watchdog_argv(hotkey, pid, grace_ms, notify_enabled))
    except Exception as exc:
        log.warning("killswitch: could not spawn watchdog (%s)", exc)
        _WATCHDOG = None
        return False
    log.info("killswitch: watchdog process started (pid %s, grace %sms)",
             _WATCHDOG.pid, grace_ms)
    return True


def stop() -> None:
    """Terminate the watchdog we spawned (normal app shutdown)."""
    global _WATCHDOG
    if _WATCHDOG is None:
        return
    if _WATCHDOG.poll() is None:
        try:
            _WATCHDOG.terminate()
        except Exception:
            pass
    _WATCHDOG = None


# --------------------------------------------------------------------------
# Logging (the watchdog has no logging config of its own)
# --------------------------------------------------------------------------

def _log(message: str) -> None:
    log.info("killswitch: %s", message)
    try:
        path = os.path.join(_base_dir(), "app.log")
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{stamp} INFO     dyst.killswitch: {message}\n")
    except Exception:
        pass


# --------------------------------------------------------------------------
# CLI entry (detached helper process: `python -m dyst.killswitch ...`)
# --------------------------------------------------------------------------

def _cli(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="dyst.killswitch",
                                     description="DYST kill-switch watchdog")
    parser.add_argument("--hotkey", required=True)
    parser.add_argument("--parent", type=int, required=True,
                        help="pid of the DYST app process to watch")
    parser.add_argument("--grace-ms", type=int, default=int(GRACE_SECONDS * 1000))
    parser.add_argument("--notify", default="1",
                        help="1 = show a notification when force-killing")
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        return 0
    return run(args.hotkey, args.parent, args.grace_ms / 1000.0,
               str(args.notify) not in ("0", "false", "False"))


if __name__ == "__main__":
    raise SystemExit(_cli())