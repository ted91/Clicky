"""Entry point for the packaged .app build (see clicky.spec) -- not used
by the normal `python3 app.py` dev workflow, which keeps using app.py's
own __main__ block directly from a terminal.

A packaged app has no terminal for the user to read "Uvicorn running on
..." from, so this starts the server in a background thread. Binds to
127.0.0.1 (not app.py's dev-mode 0.0.0.0) since this is meant for one
person's own Mac, not something to expose on the LAN by default.

On Windows there is no meetingcap menu-bar agent, so this file owns the
system-tray icon itself (pystray): it's the visible "Clicky is running"
signal, with Open dashboard / Quit. The dashboard also opens in the browser
on every launch so a new user can see the app started.
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


def _preload_current_msvcp140():
    """bleak's WinRT backend (the `winrt` package) ships an old MSVCP140.dll (14.29). If it's the first
    copy loaded, every later native library in the process gets it too, and libraries built with a
    newer MSVC (onnxruntime for Parakeet, ctranslate2 for faster-whisper) crash inside it with
    0xc0000005. Loading the system's copy first (when it's >= 14.40) makes everyone share that one;
    MSVCP140 is backward compatible, so the WinRT code is fine with it."""
    if sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes
    path = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "msvcp140.dll")
    try:
        ver = ctypes.WinDLL("version")
        size = ver.GetFileVersionInfoSizeW(path, None)
        if not size:
            return
        buf = ctypes.create_string_buffer(size)
        ver.GetFileVersionInfoW(path, 0, size, buf)
        ptr, length = ctypes.c_void_p(), wintypes.UINT()
        ver.VerQueryValueW(buf, "\\", ctypes.byref(ptr), ctypes.byref(length))
        ms = ctypes.cast(ptr, ctypes.POINTER(wintypes.DWORD * 4)).contents[2]   # VS_FIXEDFILEINFO.dwFileVersionMS
        if (ms >> 16, ms & 0xFFFF) >= (14, 40):
            ctypes.WinDLL(path)
    except Exception:
        pass


_preload_current_msvcp140()

import paths

# With no console (pythonw.exe, or the PyInstaller console=False exe) sys.stdout/stderr are None, and
# uvicorn's log setup crashes the server thread on them -- the tray icon still appears but nothing
# answers on port 8000. Send output to a log file instead, which also keeps tracebacks visible.
if sys.stdout is None or sys.stderr is None:
    os.makedirs(paths.APP_DATA_DIR, exist_ok=True)
    _log_stream = open(os.path.join(paths.APP_DATA_DIR, "clicky.log"), "a", buffering=1, encoding="utf-8")
    sys.stdout = sys.stdout or _log_stream
    sys.stderr = sys.stderr or _log_stream

import uvicorn

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
        "Look for the Clicky icon in the system tray (it may be under the ^ arrow) and right-click it to open the dashboard or quit."
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


def _dashboard_url() -> str:
    return f"http://127.0.0.1:{PORT}"


def _open_dashboard(*_):
    webbrowser.open(_dashboard_url())


def _wait_for_server(timeout: float = 20.0) -> bool:
    import socket
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", PORT), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def _open_dashboard_when_ready():
    if _wait_for_server():
        _open_dashboard()


def _tray_icon_image():
    from PIL import Image
    here = sys._MEIPASS if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
    return Image.open(os.path.join(here, "clicky_icon.ico"))


def _recording_icon_image(base):
    """The normal icon with a red dot in the corner, shown while a live recording is running."""
    from PIL import ImageDraw
    img = base.convert("RGBA").resize((64, 64))
    draw = ImageDraw.Draw(img)
    draw.ellipse((34, 34, 62, 62), fill=(220, 30, 30, 255), outline=(255, 255, 255, 255), width=3)
    return img


def _beep(kind: str):
    try:
        import winsound
        winsound.MessageBeep(winsound.MB_OK if kind == "start" else winsound.MB_ICONASTERISK)
    except Exception:
        pass


IDLE_TOOLTIP = "Clicky is running - click to open the dashboard. Double-tap Caps Lock to record."
RECORDING_TOOLTIP = "Clicky is RECORDING - double-tap Caps Lock (or right-click) to stop"


def _run_tray() -> bool:
    """Shows the Clicky system-tray icon (left-click / double-click opens the
    dashboard; right-click offers Open / Start-Stop recording / Quit) and a
    one-time "Clicky is running" notification, so a user can tell the app is
    up even though it has no window of its own. Also owns live recording
    (live_recorder.py): the tray menu item and a Caps Lock double tap both toggle
    it, with a red-dot icon, a sound and a notification on every start/stop.
    Blocks until Quit is chosen. Returns False if the tray couldn't be
    created (missing pystray/Pillow, no shell), so the caller can fall back
    to running headless rather than dying."""
    try:
        import pystray
        image = _tray_icon_image()
        recording_image = _recording_icon_image(image)
    except Exception:
        return False

    import live_recorder
    import live_transcriber
    import live_translator
    import subtitle_bar
    controller = live_recorder.controller
    icon = None

    bar = subtitle_bar.SubtitleBar(status_fn=controller.status, on_stop=controller.stop,
                                   on_open_dashboard=_open_dashboard)
    bar.start()
    live_transcriber.add_update_listener(bar.on_update)

    def _notify(message, title):
        try:
            icon.notify(message, title)
        except Exception:
            pass

    def _on_recording_event(event, info):
        if event == "started":
            icon.icon, icon.title = recording_image, RECORDING_TOOLTIP
            bar.show()
            _beep("start")
            _notify(f"Recording {info}. Double-tap Caps Lock again to stop. "
                    "Let everyone in the conversation know you're recording.", "Clicky is recording")
        elif event == "stopping":
            icon.icon, icon.title = image, "Clicky - saving the recording..."
            _beep("stop")
        elif event == "saved":
            icon.title = IDLE_TOOLTIP
            bar.hide(after_s=subtitle_bar.HIDE_AFTER_STOP_S)
            _notify(f"{info} is on the dashboard (audio + transcript).", "Recording saved")
        elif event in ("discarded", "failed"):
            icon.icon, icon.title = image, IDLE_TOOLTIP
            bar.hide(after_s=subtitle_bar.HIDE_AFTER_STOP_S)
            _notify(info, "Recording discarded" if event == "discarded" else "Recording problem")
        elif event == "warning":
            _notify(info, "Unmute your microphone")
        elif event == "info":
            _notify(info, "Microphone is live")
        icon.update_menu()

    controller.add_listener(_on_recording_event)

    def _quit(icon, _item):
        if controller.recording:
            controller.stop()
            time.sleep(1.5)   # let the save thread write the recording before the process exits
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("Open Clicky dashboard", _open_dashboard, default=True),
        pystray.MenuItem(lambda item: "Stop recording" if controller.recording else "Start recording (or double-tap Caps Lock)",
                         lambda *_: threading.Thread(target=controller.toggle, daemon=True).start()),
        pystray.MenuItem("Show subtitle bar while recording",
                         lambda *_: (settings.update(subtitle_bar=not settings.get_all().get("subtitle_bar", True)),
                                     bar.show() if controller.recording and settings.get_all().get("subtitle_bar", True)
                                     else bar.hide()),
                         checked=lambda item: settings.get_all().get("subtitle_bar", True)),
        pystray.MenuItem(f"Running at {_dashboard_url()}", None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit Clicky", _quit),
    )
    icon = pystray.Icon("Clicky", image, IDLE_TOOLTIP, menu)

    # Always running; it checks the "capslock_record_hotkey" setting itself, so Settings can switch it live.
    watcher = live_recorder.CapsLockDoubleTapWatcher(controller.toggle)

    def _on_ready(icon):
        icon.visible = True
        if watcher:
            watcher.start()
        live_recorder.CallWatcher(controller).start()   # does nothing unless "auto_record_calls" is on
        live_transcriber.preload_in_background()
        live_translator.preload_in_background()
        _notify("Click the Clicky icon in the system tray to open the dashboard. "
                "Double-tap Caps Lock to start or stop recording a conversation.", "Clicky is running")

    try:
        icon.run(_on_ready)
    except Exception:
        return False
    return True


if __name__ == "__main__":
    if not _acquire_single_instance_lock():
        _open_dashboard()
        _show_already_running_alert()
        sys.exit(1)

    server_thread = threading.Thread(target=_run_server, daemon=True)
    server_thread.start()

    # Every launch (not just the first) opens the dashboard so it's obvious
    # the app started -- see the module docstring for why it's the tray icon
    # that carries the day-to-day "it's running" signal.
    threading.Thread(target=_open_dashboard_when_ready, daemon=True).start()

    if _run_tray():
        os._exit(0)  # tray "Quit" -- hard exit so no background poller thread keeps the process alive
    server_thread.join()
