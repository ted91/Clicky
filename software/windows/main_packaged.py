"""Entry point for the packaged .app build (see clicky.spec) -- not used
by the normal `python3 app.py` dev workflow, which keeps using app.py's
own __main__ block directly from a terminal.

A packaged app has no terminal for the user to read "Uvicorn running on
..." from, so this starts the server in a background thread. Binds to
127.0.0.1 (not app.py's dev-mode 0.0.0.0) since this is meant for one
person's own Mac, not something to expose on the LAN by default.

The dashboard is a configuration/viewing surface, not something Clicky
should shove in your face on every launch -- day-to-day control is the
menu-bar icon (meetingcap, see meeting_recorder.py), which has its own
"Open Clicky Dashboard" item for whenever you actually want it. The
browser tab only auto-opens the very first time, before /setup has been
completed, since a first-time user needs somewhere to land.
"""
import os
import signal
import subprocess
import sys
import threading
import time
import webbrowser

# fcntl is POSIX-only (macOS/Linux) -- doesn't exist on Windows at all, and
# importing it unconditionally at module load would crash a Windows build
# before anything else ran. msvcrt (Windows' own file-locking API) is the
# reverse: stdlib on Windows, doesn't exist elsewhere. Both are used only
# inside _acquire_single_instance_lock() below, branched by platform.
if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

# PyInstaller extracts bundled files (templates/, static/, etc.) into a
# temporary _MEIPASS directory at runtime. app.py references them with bare
# relative paths like "static" and "templates", so we must set cwd to
# _MEIPASS before importing app -- otherwise those paths resolve against
# wherever the OS launched the .app from (typically /) and StaticFiles /
# Jinja2Templates both crash immediately with "directory does not exist".
if getattr(sys, "frozen", False):
    os.chdir(sys._MEIPASS)

import uvicorn

import paths
import settings
from app import app

PORT = 8000

# Kept as a module-level reference deliberately -- closing this file object
# (e.g. if it were a local variable and got garbage-collected) releases the
# flock automatically, defeating the whole point. Must stay open for the
# entire process lifetime.
_lock_file = None


def _try_flock(f) -> bool:
    f.seek(0)
    try:
        if sys.platform == "win32":
            # msvcrt.locking() has no separate "shared vs exclusive" mode
            # like flock -- LK_NBLCK is already exclusive & non-blocking,
            # matching fcntl.LOCK_EX | fcntl.LOCK_NB below. Locks exactly 1
            # byte at the current position (seeked to 0 above) -- arbitrary,
            # this file has no real content, just needs *a* byte for
            # Windows' byte-range locking to work.
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _read_stale_pid(lock_path):
    """Best-effort read of the PID a previous instance recorded in the
    lock file, before we open our own handle on it (which may truncate).
    Returns None on a fresh/missing/corrupt file -- never raises."""
    try:
        with open(lock_path, "r") as f:
            content = f.read().strip()
        return int(content) if content else None
    except (OSError, ValueError):
        return None


def _process_is_this_app(pid) -> bool:
    """Confirms `pid` is actually a Clicky process before we ever consider
    killing it -- PIDs get reused after a reboot, so the number recorded in
    the lock file could in principle now belong to something unrelated."""
    try:
        if sys.platform == "win32":
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=5).stdout
            return "Clicky" in out
        else:
            out = subprocess.run(
                ["ps", "-p", str(pid), "-o", "comm="],
                capture_output=True, text=True, timeout=5).stdout
            return "Clicky" in out
    except Exception:
        return False


def _kill_stale_instance(pid) -> bool:
    """Terminates a confirmed-stale Clicky process and waits for it to
    actually exit, escalating to a hard kill if it doesn't. Returns True
    once the PID is gone (so the caller can safely retry the flock)."""
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=5)
        else:
            os.kill(pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pass

    def _still_alive() -> bool:
        if sys.platform == "win32":
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=5).stdout
            return str(pid) in out
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False

    for _ in range(20):  # ~2s grace period for a normal shutdown
        if not _still_alive():
            return True
        time.sleep(0.1)

    if sys.platform != "win32":
        try:
            os.kill(pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
        time.sleep(0.3)
    return not _still_alive()


def _acquire_single_instance_lock() -> bool:
    """Refuses to let a second Clicky instance start at all, rather than
    letting it silently fail later trying to bind an already-used port 8000
    (a real bug hit repeatedly during development: a stale/still-running
    instance -- e.g. from a "Quit" that didn't fully terminate everything,
    or a translocated Finder launch that appeared to fail -- left a new
    launch attempt with no visible error, no menu-bar icon, and no
    explanation). Uses an exclusive, non-blocking flock on a file in the
    app's own data directory, held for the process's entire lifetime;
    releasing it (process exit, for any reason, including a crash) is
    handled automatically by the OS closing the fd.

    If the lock is already held, this doesn't just give up -- it reads the
    PID the holder recorded, confirms that PID is genuinely a Clicky
    process (never kill on a bare PID-number match alone), and kills it
    before retrying. This is what makes replacing the installed build with
    a new one "just work": overwriting the app's files doesn't kill
    whatever process already had them open, so without this, every upgrade
    left a stale pre-upgrade instance squatting on both the lock and port
    8000, and the new launch could only ever show "already running" -- a
    real bug hit shipping this exact feature. Since this is a single-user
    personal app, a fresh launch should always supersede an old one, not
    silently refuse to start."""
    global _lock_file
    lock_path = os.path.join(paths.APP_DATA_DIR, "clicky.lock")
    stale_pid = _read_stale_pid(lock_path)
    _lock_file = open(lock_path, "a+")
    locked = _try_flock(_lock_file)
    if not locked and stale_pid and stale_pid != os.getpid() and _process_is_this_app(stale_pid):
        if _kill_stale_instance(stale_pid):
            locked = _try_flock(_lock_file)
    if locked:
        _lock_file.seek(0)
        _lock_file.truncate()
        _lock_file.write(str(os.getpid()))
        _lock_file.flush()
    return locked


def _show_already_running_alert():
    """A launch that can't proceed needs to say so somehow -- there's no
    window, and by definition no unique menu-bar/tray icon to point at
    since another instance already owns that. A native alert dialog is the
    only surface guaranteed visible regardless of what else is running."""
    message = "Check the menu bar for the existing mic icon, or quit it first before relaunching." \
        if sys.platform == "darwin" else \
        "Check the system tray for the existing icon, or quit it first before relaunching."
    try:
        if sys.platform == "darwin":
            subprocess.run(
                ["osascript", "-e",
                 f'display alert "Clicky is already running" message "{message}" as warning'],
                timeout=5, capture_output=True,
            )
        elif sys.platform == "win32":
            import ctypes
            MB_ICONWARNING = 0x30
            ctypes.windll.user32.MessageBoxW(0, message, "Clicky is already running", MB_ICONWARNING)
    except Exception:
        pass  # best-effort -- a failed alert must never prevent the sys.exit(1) that follows it


def _run_server():
    uvicorn.run(app, host="127.0.0.1", port=PORT, reload=False, log_level="info")


if __name__ == "__main__":
    if not _acquire_single_instance_lock():
        _show_already_running_alert()
        sys.exit(1)

    first_run = not settings.is_configured()

    server_thread = threading.Thread(target=_run_server, daemon=True)
    server_thread.start()

    if first_run:
        time.sleep(1.5)  # give uvicorn a moment to bind before opening the tab
        webbrowser.open(f"http://127.0.0.1:{PORT}")

    server_thread.join()
