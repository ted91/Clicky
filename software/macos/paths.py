"""Resolves where the pipeline stores its persistent data (settings.json,
recordings.json, audio files) -- separate from the pipeline's own source
directory. This matters once the app is packaged (PyInstaller .app): the
bundle's contents are read-only-ish and get replaced wholesale on every
reinstall/update, so writing recordings.json next to app.py (the old
default -- os.path.dirname(__file__)) would silently wipe a user's data
on every upgrade.

Defaults to a dotfolder in the user's home directory, overridable via
CLICKY_DATA_DIR for anyone who wants it elsewhere. One-time migrates data
found in the old in-repo location, so upgrading from "python3 app.py in a
git checkout" to the packaged app doesn't lose existing recordings/settings.
"""
import os
import shutil

APP_DATA_DIR = os.environ.get("CLICKY_DATA_DIR") or os.path.join(os.path.expanduser("~"), ".clicky-pipeline")
_LEGACY_DIR = os.path.dirname(__file__)  # pipeline/ source directory -- the pre-packaging default

os.makedirs(APP_DATA_DIR, exist_ok=True)


def _migrate_legacy_file(name: str) -> str:
    new_path = os.path.join(APP_DATA_DIR, name)
    old_path = os.path.join(_LEGACY_DIR, name)
    if not os.path.exists(new_path) and os.path.exists(old_path):
        shutil.copy2(old_path, new_path)
    return new_path


def _migrate_legacy_dir(name: str) -> str:
    new_path = os.path.join(APP_DATA_DIR, name)
    old_path = os.path.join(_LEGACY_DIR, name)
    if not os.path.exists(new_path) and os.path.isdir(old_path):
        shutil.copytree(old_path, new_path)
    os.makedirs(new_path, exist_ok=True)
    return new_path


SETTINGS_PATH = _migrate_legacy_file("settings.json")
STORAGE_PATH = _migrate_legacy_file("recordings.json")
AUDIO_DIR = _migrate_legacy_dir("audio")
VOICEPRINTS_PATH = _migrate_legacy_file("voiceprints.json")  # see voice_id.py
