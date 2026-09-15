"""DYST — Windows notification helper (tray balloon / toast).

Used by the kill switch to tell the user *why* the app vanished: when the
global hotkey fires the app quits immediately, so the notification must
outlive it. `show()` therefore spawns a short-lived **detached helper
process** (this same module, `--title/--message`) which owns the tray icon
and keeps it alive long enough for Windows to render the balloon.

Two entry points:
    show(title, message, timeout_ms=5000)  -> spawns the detached helper
    show_blocking(title, message, ...)     -> shows in the CURRENT process
                                              (helper-process mode)

Windows only; on other platforms everything is a no-op returning False.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys

log = logging.getLogger("dyst.notify")

# Notification defaults
DEFAULT_TIMEOUT_MS = 5000
TITLE = "DYST (did you see that? 👀)"


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def show(title: str, message: str, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> bool:
    """Show a Windows notification that survives the caller exiting.

    Launches a detached helper process running `show_blocking()`. Returns
    True if the helper was spawned (not necessarily displayed).

    Safe no-op on non-Windows or if spawning fails (logged, never raises).
    """
    if sys.platform != "win32":
        log.debug("notify: skipped (not Windows)")
        return False

    argv = _helper_argv(title, message, timeout_ms)
    try:
        _spawn_detached(argv)
        log.info("notify: shown %r", title)
        return True
    except Exception as exc:  # never let a notification failure matter
        log.warning("notify: could not spawn notification helper (%s)", exc)
        return False


def show_blocking(title: str, message: str, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> bool:
    """Show the notification in this process and block while it is visible.

    Uses Shell_NotifyIcon with a hidden message-only window. Needed because
    the balloon is tied to the tray icon's lifetime: as soon as the icon is
    deleted the notification is dismissed. Blocks for `timeout_ms`.
    """
    if sys.platform != "win32":
        print(message)
        return False

    import ctypes
    import time
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)

    NIM_ADD, NIM_MODIFY, NIM_DELETE = 0x0, 0x1, 0x2
    NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x1, 0x2, 0x4, 0x10
    NIIF_INFO, NIIF_LARGE_ICON = 0x1, 0x20
    WM_APP = 0x8000
    HWND_MESSAGE = -3
    IMAGE_ICON, LR_LOADFROMFILE, LR_DEFAULTSIZE = 1, 0x10, 0x40
    IDI_APPLICATION = 32512

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("hWnd", wintypes.HWND),
            ("uID", wintypes.UINT),
            ("uFlags", wintypes.UINT),
            ("uCallbackMessage", wintypes.UINT),
            ("hIcon", wintypes.HICON),
            ("szTip", wintypes.WCHAR * 128),
            ("dwState", wintypes.DWORD),
            ("dwStateMask", wintypes.DWORD),
            ("szInfo", wintypes.WCHAR * 256),
            ("uVersion", wintypes.UINT),
            ("szInfoTitle", wintypes.WCHAR * 64),
            ("dwInfoFlags", wintypes.DWORD),
            ("guidItem", ctypes.c_byte * 16),
            ("hBalloonIcon", wintypes.HICON),
        ]

    user32.CreateWindowExW.restype = wintypes.HWND
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p,
    ]
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.DestroyIcon.argtypes = [wintypes.HICON]
    user32.LoadIconW.restype = wintypes.HICON
    user32.LoadIconW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
    user32.LoadImageW.restype = wintypes.HICON
    user32.LoadImageW.argtypes = [
        wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
        ctypes.c_int, ctypes.c_int, wintypes.UINT,
    ]
    shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
    shell32.Shell_NotifyIconW.restype = wintypes.BOOL

    hwnd = user32.CreateWindowExW(
        0, "STATIC", TITLE, 0, 0, 0, 0, 0,
        wintypes.HWND(HWND_MESSAGE), None, None, None,
    )
    if not hwnd:
        log.warning("notify: CreateWindowExW failed (%d)", ctypes.get_last_error())
        return False

    # Prefer the app's own icon; fall back to the generic application icon.
    hicon = None
    icon_path = os.path.join(_base_dir(), "icon.ico")
    if os.path.isfile(icon_path):
        hicon = user32.LoadImageW(None, icon_path, IMAGE_ICON, 0, 0,
                                  LR_LOADFROMFILE | LR_DEFAULTSIZE)
    if not hicon:
        hicon = user32.LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION))

    nid = NOTIFYICONDATAW()
    nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
    nid.hWnd = hwnd
    nid.uID = 1
    nid.uCallbackMessage = WM_APP + 1
    nid.szTip = TITLE[:127]

    added = False
    try:
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.hIcon = hicon
        if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
            log.warning("notify: Shell_NotifyIcon(NIM_ADD) failed (%d)",
                        ctypes.get_last_error())
            return False
        added = True

        nid.uFlags = NIF_INFO
        nid.szInfoTitle = title[:63]
        nid.szInfo = message[:255]
        nid.dwInfoFlags = NIIF_INFO | (NIIF_LARGE_ICON if hicon else 0)
        nid.uVersion = int(timeout_ms)
        if not shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid)):
            log.warning("notify: Shell_NotifyIcon(NIM_MODIFY) failed (%d)",
                        ctypes.get_last_error())
            return False

        # The balloon dies with the tray icon, so hold the icon up for the
        # requested time (pumping messages keeps Windows happy).
        deadline = time.monotonic() + max(1.0, timeout_ms / 1000.0)
        while time.monotonic() < deadline:
            time.sleep(0.1)
        return True
    finally:
        if added:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
        if hicon:
            user32.DestroyIcon(hicon)
        user32.DestroyWindow(hwnd)


# --------------------------------------------------------------------------
# Helper-process plumbing
# --------------------------------------------------------------------------

def _base_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _helper_argv(title: str, message: str, timeout_ms: int) -> list:
    """Command line for the detached helper (module in dev, exe when frozen)."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--notify", title, message, str(timeout_ms)]
    python = sys.executable
    pythonw = os.path.join(os.path.dirname(python), "pythonw.exe")
    if os.path.isfile(pythonw):
        python = pythonw
    return [python, "-m", "dyst.notify",
            "--title", title, "--message", message, "--timeout", str(timeout_ms)]


def _spawn_detached(argv: list) -> None:
    """Start *argv* detached so it outlives this process."""
    DETACHED_PROCESS = 0x00000008
    CREATE_NO_WINDOW = 0x08000000
    CREATE_BREAKAWAY_FROM_JOB = 0x01000000
    flags = DETACHED_PROCESS | CREATE_NO_WINDOW
    try:
        subprocess.Popen(argv, cwd=_base_dir(), close_fds=True,
                         creationflags=flags | CREATE_BREAKAWAY_FROM_JOB)
    except OSError:
        # Breakaway is refused when the parent job forbids it — retry without.
        subprocess.Popen(argv, cwd=_base_dir(), close_fds=True, creationflags=flags)


def _cli(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="dyst.notify",
                                     description="show one DYST notification")
    parser.add_argument("--title", default=TITLE)
    parser.add_argument("--message", default="")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_MS)
    args = parser.parse_args(argv)
    return 0 if show_blocking(args.title, args.message, args.timeout) else 1


if __name__ == "__main__":
    raise SystemExit(_cli())