"""Screenshot of the running meeting's window (Teams / Zoom / Webex / Slack / a browser with Google Meet),
for live recordings: what was on screen at a given moment, saved next to the transcript.

The window is captured with PrintWindow(PW_RENDERFULLCONTENT), which renders the window's own contents
even if something (e.g. Clicky's subtitle bar) is on top of it. If that comes back blank (some GPU-drawn
windows), it falls back to a plain screen grab of the window's area.
"""
import ctypes
import ctypes.wintypes as wt
import os

import meeting_detector

_TEAMS_MAIN_SECTIONS = ("chat |", "activity |", "teams |", "calendar |", "calls |", "onedrive |", "apps |",
                        "copilot |", "community |", "files |", "people |", "planner |")


def _windows():
    """[(hwnd, exe, title, (left, top, right, bottom))] for visible, non-minimized top-level windows."""
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    out = []
    proc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                pid = wt.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                r = wt.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(r))
                if r.right - r.left > 200 and r.bottom - r.top > 150:
                    out.append((hwnd, meeting_detector._exe_name(kernel32, pid.value), buf.value,
                                (r.left, r.top, r.right, r.bottom)))
        return True

    user32.EnumWindows(proc(cb), 0)
    return out


def find_meeting_window(call: str):
    """Best window for the detected call ("" -> None): the meeting window itself, not e.g. Teams' chat."""
    c = call.lower()
    if ":" in c and any(b in c.split(":")[0] for b in meeting_detector.BROWSERS):
        exe_match = lambda exe: exe == c.split(":")[0].strip()
        preferred = lambda title: any(k in title.lower() for k in meeting_detector.MEETING_TITLE_KEYWORDS)
    else:
        key = next((k for k in ("teams", "zoom", "webex", "slack") if k in c), c)
        exe_match = lambda exe: key in exe or (key == "webex" and "ciscocollab" in exe)
        preferred = lambda title: not title.lower().startswith(_TEAMS_MAIN_SECTIONS)
    cands = [w for w in _windows() if exe_match(w[1])]
    if not cands:
        return None
    area = lambda w: (w[3][2] - w[3][0]) * (w[3][3] - w[3][1])
    best = [w for w in cands if preferred(w[2])] or cands
    return max(best, key=area)


def _print_window(hwnd, w, h):
    """PrintWindow(PW_RENDERFULLCONTENT) -> PIL image, or None if it fails / comes back all black."""
    from PIL import Image
    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    hdc = user32.GetWindowDC(hwnd)
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    old = gdi32.SelectObject(mem, bmp)
    try:
        if not user32.PrintWindow(hwnd, mem, 2):
            return None

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                        ("biBitCount", wt.WORD), ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                        ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                        ("biClrImportant", wt.DWORD)]
        bih = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)  # top-down BGRA
        buf = ctypes.create_string_buffer(w * h * 4)
        if not gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bih), 0):
            return None
        img = Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1)
        return img if img.getbbox() else None      # all black -> the window didn't render for us
    finally:
        gdi32.SelectObject(mem, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(hwnd, hdc)


def capture_meeting():
    """-> (PIL image, window title). Raises RuntimeError with a user-facing message."""
    call = meeting_detector.current_call()
    if not call:
        raise RuntimeError("No Teams / Zoom / Meet call is running on this computer.")
    win = find_meeting_window(call)
    if not win:
        raise RuntimeError("Couldn't find the meeting window - is it minimized?")
    hwnd, _exe, title, (l, t, r, b) = win
    img = _print_window(hwnd, r - l, b - t)
    if img is None:
        from PIL import ImageGrab
        img = ImageGrab.grab(bbox=(l, t, r, b), all_screens=True)
    return img, title


def save(img, folder: str, filename: str) -> str:
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, filename)
    img.save(path, "PNG", optimize=True)
    return path
