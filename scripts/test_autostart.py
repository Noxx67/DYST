"""Autostart behavior harness (offscreen).

Tests the four registry/config scenarios against the real HKCU Run key:
A: autostart ON  + --autostart flag (boot) -> Run key registered, daemon runs
B: autostart OFF + --autostart flag (boot) + stale key -> removes key, exits fast
C: autostart OFF, no stale key        -> daemon runs, key stays absent
D: autostart OFF, stale key, no flag (manual launch) -> removes stale key, daemon runs

Usage: QT_QPA_PLATFORM=offscreen .venv/Scripts/python .work/test_autostart.py
"""
import json
import os
import sys
import time
import subprocess

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PY = os.path.join(ROOT, ".venv", "Scripts", "python")
CFG_ON = os.path.join(ROOT, ".work", "cfg_on.json")
CFG_OFF = os.path.join(ROOT, ".work", "cfg_off.json")
os.makedirs(os.path.join(ROOT, ".work"), exist_ok=True)

json.dump({"autostart": True, "tick_seconds": 10000.0, "odds": 1000000,
           "media_folder": os.path.join(ROOT, "media", "nonexistent_pool")},
          open(CFG_ON, "w"))
json.dump({"autostart": False, "tick_seconds": 10000.0, "odds": 1000000,
           "media_folder": os.path.join(ROOT, "media", "nonexistent_pool")},
          open(CFG_OFF, "w"))

sys.path.insert(0, ROOT)
from dyst import autostart as aut


def reg_value():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Run",
                            0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, "DYST")
            return value if isinstance(value, str) else None
    except OSError:
        return None


def boot(cmd, kill_after=3.0):
    """Launch cmd, kill after kill_after (taskkill works on GUI apps).
    Returns (elapsed_ms, captured_stdout)."""
    proc = subprocess.Popen([PY, "main.py", *cmd], cwd=ROOT, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT)
    t0 = time.time()
    try:
        proc.wait(timeout=kill_after)
    except subprocess.TimeoutExpired:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
    out = b""
    if proc.stdout is not None:
        try:
            out = proc.stdout.read()
        except OSError:
            pass
    return (time.time() - t0) * 1000, out.decode("utf-8", "replace")


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)


def main():
    aut.disable()
    check(reg_value() is None, "startup: registry clean")

    # A: autostart ON + boot flag -> registered, daemon runs until killed
    aut.disable()
    ms, out = boot(["--autostart", "--config", CFG_ON])
    check(reg_value() is not None, "A: boot with autostart=on registered Run key")
    if reg_value():
        check("autostart" in reg_value(), "A: stored command carries --autostart")
    check(ms > 100 and ms < 4000, "A: daemon keeps running (%d ms)" % ms)
    if ms < 100 or "Traceback" in out:
        print("   A output:", out.strip().splitlines()[-3:] or out[:400])
    aut.disable()
    check(reg_value() is None, "A: cleanup removed Run key")

    # B: autostart OFF + boot flag + stale key -> remove + exit fast
    aut.enable()
    check(reg_value() is not None, "B: stale key installed")
    ms, out = boot(["--autostart", "--config", CFG_OFF])
    check(ms < 5000, "B: exited fast (%d ms)" % ms)
    check(reg_value() is None, "B: removed Run key (autostart off)")

    # C: autostart OFF, no stale key -> daemon runs, key stays absent
    check(reg_value() is None, "C: no stale key")
    ms, out = boot(["--daemon", "--config", CFG_OFF])
    check(ms > 2000, "C: daemon keeps running (%d ms)" % ms)
    check(reg_value() is None, "C: did not create a Run key (config off)")
    if ms < 2000 or "Traceback" in out:
        print("   C output:", out.strip().splitlines()[-3:] or out[:400])

    # D: autostart OFF, stale key, no boot flag (manual launch) -> remove + run
    aut.enable()
    check(reg_value() is not None, "D: stale key installed")
    ms, out = boot(["--daemon", "--config", CFG_OFF])
    check(ms > 2000, "D: daemon keeps running (%d ms)" % ms)
    check(reg_value() is None, "D: removed stale key (manual launch)")
    if ms < 2000 or "Traceback" in out:
        print("   D output:", out.strip().splitlines()[-3:] or out[:400])

    aut.disable()
    check(reg_value() is None, "final: registry clean")
    print("DONE")


if __name__ == "__main__":
    main()
