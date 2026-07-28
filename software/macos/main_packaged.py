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
    handled automatically by the OS closing the fd."""
    global _lock_file
    lock_path = os.path.join(paths.APP_DATA_DIR, "clicky.lock")
    _lock_file = open(lock_path, "w")
    try:
        if sys.platform == "win32":
            # msvcrt.locking() has no separate "shared vs exclusive" mode
            # like flock -- LK_NBLCK is already exclusive & non-blocking,
            # matching fcntl.LOCK_EX | fcntl.LOCK_NB below. Locks exactly 1
            # byte starting at the current position (0, since the file was
            # just opened) -- arbitrary, this file has no real content,
            # just needs *a* byte for Windows' byte-range locking to work.
            msvcrt.locking(_lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(_lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


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
