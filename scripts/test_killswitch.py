"""Kill-switch watchdog harness (Windows).

Tests the out-of-process dead man's switch (dyst/killswitch.py):

  1. parse_hotkey accepts/rejects the expected strings
  2. combo_down is False for keys nobody is pressing
  3. run() force-terminates a wedged process once the grace period expires
  4. run() notices a cooperative process quitting during the grace period
     and does NOT terminate it
  5. --e2e (opt-in): spawns the real detached watchdog against a hung
     process, injects the hotkey with keybd_event, and checks the kill

Usage: .venv/Scripts/python scripts/test_killswitch.py [--e2e]
"""
import ctypes
import os
import subprocess
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from dyst import killswitch as ks  # noqa: E402

FAILURES = []
PY = sys.executable
# Deliberately NOT the default kill combo, so a real running DYST instance
# (which watches ctrl+shift+alt+k) is never affected by the injected keys.
TEST_HOTKEY = "ctrl+shift+alt+f9"


def check(name, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{' — ' + detail if detail else ''}")
    if not ok:
        FAILURES.append(name)


def spawn_hung(seconds=600):
    return subprocess.Popen([PY, "-c", f"import time; time.sleep({seconds})"])


def kill_quiet(proc):
    if proc.poll() is None:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass


def test_parse():
    print("1) parse_hotkey")
    check("default combo parses", ks.parse_hotkey("ctrl+shift+alt+k") ==
          (ks.MOD_CONTROL | ks.MOD_SHIFT | ks.MOD_ALT, 0x4B))
    check("win modifier parses", ks.parse_hotkey("win+f1") == (ks.MOD_WIN, 0x70))
    for bad in ("k", "ctrl+", "ctrl+hyper+k", "ctrl+enter"):
        try:
            ks.parse_hotkey(bad)
            check(f"rejects {bad!r}", False)
        except ValueError:
            check(f"rejects {bad!r}", True)


def test_idle_state():
    print("2) combo_down while idle")
    mods, vk = ks.parse_hotkey(TEST_HOTKEY)
    check("not pressed -> False", ks.combo_down(mods, vk) is False)


def test_force_kill():
    print("3) wedged process is force-killed after grace")
    proc = spawn_hung()
    real_combo_down = ks.combo_down
    ks.combo_down = lambda mods, vk: True  # simulate a press immediately
    try:
        t0 = time.monotonic()
        # Generous grace: the press is only "detected" immediately, so this
        # measures the grace wait + TerminateProcess.
        rc = ks.run(TEST_HOTKEY, proc.pid, grace_seconds=0.7, notify_enabled=False)
        elapsed = time.monotonic() - t0
    finally:
        ks.combo_down = real_combo_down
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    check("run() returned 0", rc == 0, f"rc={rc}")
    check("process is dead", proc.poll() is not None,
          f"returncode={proc.returncode}")
    check("grace period was honoured", elapsed >= 0.7, f"{elapsed:.2f}s")
    kill_quiet(proc)


def test_cooperative_exit():
    print("4) cooperative process is not force-killed")
    # Exits on its own well inside the grace window.
    proc = subprocess.Popen([PY, "-c", "import time; time.sleep(0.2)"])
    real_combo_down = ks.combo_down
    ks.combo_down = lambda mods, vk: True
    try:
        rc = ks.run(TEST_HOTKEY, proc.pid, grace_seconds=3.0, notify_enabled=False)
    finally:
        ks.combo_down = real_combo_down
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    check("run() returned 0", rc == 0, f"rc={rc}")
    check("child exited on its own (code 0)", proc.returncode == 0,
          f"returncode={proc.returncode}")


def test_orphan_exit():
    print("5) detached watchdog survives the app's death, then exits alone")
    driver = (
        "import sys, time; sys.path.insert(0, r'%s'); "
        "from dyst import killswitch as ks; "
        "ks.start('%s', grace_seconds=0.5, notify_enabled=False); "
        "print(ks._WATCHDOG.pid, flush=True); time.sleep(600)"
    ) % (ROOT, TEST_HOTKEY)
    parent = subprocess.Popen([PY, "-c", driver], cwd=ROOT,
                              stdout=subprocess.PIPE, text=True)
    try:
        line = parent.stdout.readline().strip()
        wd_pid = int(line)
    except (ValueError, TypeError):
        check("watchdog pid reported", False, f"got {line!r}")
        kill_quiet(parent)
        return
    check("watchdog spawned by the app", wd_pid > 0, f"pid={wd_pid}")

    # Simulate a hard app crash — the watchdog must outlive it.
    kill_quiet(parent)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and _pid_alive(wd_pid):
        time.sleep(0.1)
    alive = _pid_alive(wd_pid)
    check("watchdog exited after the app died", not alive,
          f"pid {wd_pid} alive={alive}")
    if alive:
        subprocess.run(["taskkill", "/F", "/PID", str(wd_pid)],
                       capture_output=True)


def _pid_alive(pid):
    SYNCHRONIZE = 0x00100000
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = ctypes.c_void_p
    handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(ctypes.c_void_p(handle), 0) != 0
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def test_e2e():
    print("6) end-to-end: detached watchdog + injected key press")
    proc = spawn_hung()
    argv = [PY, "-m", "dyst.killswitch",
            "--hotkey", TEST_HOTKEY, "--parent", str(proc.pid),
            "--grace-ms", "600", "--notify", "0"]
    watchdog = subprocess.Popen(argv, cwd=ROOT,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
    time.sleep(1.5)  # let the watchdog arm
    check("watchdog is running", watchdog.poll() is None,
          f"rc={watchdog.poll()}")

    user32 = ctypes.windll.user32
    vk = {"ctrl": 0x11, "shift": 0x10, "alt": 0x12, "f9": 0x78}
    order = ["ctrl", "shift", "alt", "f9"]
    KEYUP = 0x0002
    try:
        for name in order:
            user32.keybd_event(vk[name], 0, 0, 0)
        time.sleep(0.15)
        for name in reversed(order):
            user32.keybd_event(vk[name], 0, KEYUP, 0)
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline and proc.poll() is None:
            time.sleep(0.1)
    finally:
        for name in order:
            user32.keybd_event(vk[name], 0, KEYUP, 0)

    check("injected hotkey killed the wedged process", proc.poll() is not None,
          f"alive after 8s (returncode={proc.returncode})")
    if proc.poll() is not None:
        check("force-killed (non-zero exit)", proc.returncode != 0,
              f"returncode={proc.returncode}")
    try:
        watchdog.wait(timeout=5)
        check("watchdog exited by itself", True)
    except subprocess.TimeoutExpired:
        check("watchdog exited by itself", False, "still running")
    kill_quiet(proc)
    kill_quiet(watchdog)


def main():
    if sys.platform != "win32":
        print("kill-switch tests are Windows-only")
        return 0
    e2e = "--e2e" in sys.argv
    test_parse()
    test_idle_state()
    test_force_kill()
    test_cooperative_exit()
    test_orphan_exit()
    if e2e:
        test_e2e()
    else:
        print("6) end-to-end test skipped (pass --e2e to run it)")
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {', '.join(FAILURES)}")
        return 1
    print("all kill-switch checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())