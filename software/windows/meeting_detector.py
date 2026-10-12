"""Is a Teams / Zoom / Google Meet call running right now? Adapted from Live Subtitles' detector.py
(github.com/ted91/teams-subtitles). No admin needed.

Windows records which apps currently have the microphone open (the data behind the orange mic dot on
the taskbar) under HKCU\\...\\CapabilityAccessManager\\ConsentStore\\microphone: an app has it open while
LastUsedTimeStart > 0 and LastUsedTimeStop == 0. Desktop call apps (Teams, Zoom, ...) count on their
own; a browser only counts if one of its windows has a meeting-like title (Google Meet, Teams/Zoom web).
Call apps keep the mic open while you're muted, so this still says "in a call" when muted.
"""
import ctypes
import ctypes.wintypes as wt
import os
import sys

try:
    import winreg
except ImportError:
    winreg = None

MIC_KEY = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"
CALL_APPS = ["teams", "zoom", "webex", "slack"]
BROWSERS = ["chrome", "msedge", "firefox", "brave", "opera", "vivaldi", "arc"]
MEETING_TITLE_KEYWORDS = ["meet -", "meet.google.com", "google meet", "microsoft teams", "zoom meeting", "zoom.us"]
OWN_EXE = os.path.basename(sys.executable).lower()   # Clicky opens the mic itself while recording


def apps_using_microphone() -> list:
    """Lowercase app identifiers (exe paths or Store package names) that have the mic open now."""
    if winreg is None:
        return []
    found = []
    for path, nonpackaged in ((MIC_KEY, False), (MIC_KEY + r"\NonPackaged", True)):
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path)
        except OSError:
            continue
        with key:
            i = 0
            while True:
                try:
                    name = winreg.EnumKey(key, i)
                except OSError:
                    break
                i += 1
                if name == "NonPackaged":
                    continue
                try:
                    with winreg.OpenKey(key, name) as sub:
                        start = winreg.QueryValueEx(sub, "LastUsedTimeStart")[0]
                        stop = winreg.QueryValueEx(sub, "LastUsedTimeStop")[0]
                except OSError:
                    continue
                if start and not stop:
                    found.append((name.replace("#", "\\") if nonpackaged else name).lower())
    return found


def _visible_window_titles():
    """[(exe_name, title)] for visible top-level windows."""
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    out = []
    enum_proc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                pid = wt.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                out.append((_exe_name(kernel32, pid.value), buf.value))
        return True

    user32.EnumWindows(enum_proc(cb), 0)
    return out


def _exe_name(kernel32, pid: int) -> str:
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        size = wt.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value).lower()
        return ""
    finally:
        kernel32.CloseHandle(h)


def current_call() -> str:
    """Name of the call app/meeting window if a call is running, else "" (instant, not debounced)."""
    browsers_on_mic = []
    for app in apps_using_microphone():
        base = app.rsplit("\\", 1)[-1]
        if base == OWN_EXE:
            continue
        if any(b in base for b in BROWSERS):
            browsers_on_mic.append(base)
        elif any(a in app for a in CALL_APPS):
            return base
    if browsers_on_mic:
        for exe, title in _visible_window_titles():
            if any(b in exe for b in BROWSERS) and any(k in title.lower() for k in MEETING_TITLE_KEYWORDS):
                return f"{exe}: {title[:50]}"
    return ""


if __name__ == "__main__":
    print("apps using the microphone:", apps_using_microphone() or "none")
    print("call running:", current_call() or "no")
