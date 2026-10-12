"""'Start Clicky with Windows': a shortcut in the user's Startup folder (no admin, no registry)."""
import os
import subprocess
import sys

SHORTCUT_NAME = "Clicky.lnk"


def _startup_dir() -> str:
    return os.path.join(os.environ["APPDATA"], r"Microsoft\Windows\Start Menu\Programs\Startup")


def _shortcut_path() -> str:
    return os.path.join(_startup_dir(), SHORTCUT_NAME)


def is_enabled() -> bool:
    return os.path.exists(_shortcut_path())


def _launch_target():
    """(target, arguments, working_dir) for how this Clicky is running: the packaged exe, or the
    windowless Python interpreter running main_packaged.py from source."""
    if getattr(sys, "frozen", False):
        return sys.executable, "", os.path.dirname(sys.executable)
    here = os.path.dirname(os.path.abspath(__file__))
    exe = sys.executable
    if os.path.basename(exe).lower() == "python.exe":
        exe = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return exe, f'"{os.path.join(here, "main_packaged.py")}"', here


def set_enabled(enabled: bool):
    if not enabled:
        if is_enabled():
            os.remove(_shortcut_path())
        return
    target, args, workdir = _launch_target()
    here = os.path.dirname(os.path.abspath(__file__))
    icon = os.path.join(sys._MEIPASS if getattr(sys, "frozen", False) else here, "clicky_icon.ico")
    ps = ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:CLICKY_LNK); "
          "$s.TargetPath = $env:CLICKY_TARGET; $s.Arguments = $env:CLICKY_ARGS; "
          "$s.WorkingDirectory = $env:CLICKY_WORKDIR; $s.IconLocation = $env:CLICKY_ICON; $s.Save()")
    env = dict(os.environ, CLICKY_LNK=_shortcut_path(), CLICKY_TARGET=target, CLICKY_ARGS=args,
               CLICKY_WORKDIR=workdir, CLICKY_ICON=icon)
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], env=env, check=True,
                   capture_output=True, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
