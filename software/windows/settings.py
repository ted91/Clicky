"""User-editable settings, written by the /setup and /pair webpages —
distinct from config.py's env-file defaults. Stored as JSON so the webapp
can persist changes without touching a hand-edited .env. Anything saved
here overrides the matching config.py default at runtime; see
config.reload_settings().
"""
import hashlib
import json
import os
import secrets
import threading

import paths

_PATH = paths.SETTINGS_PATH
_lock = threading.Lock()

PBKDF2_ITERATIONS = 200_000


def _load() -> dict:
    if not os.path.exists(_PATH):
        return {}
    with open(_PATH, "r") as f:
        return json.load(f)


def _save(data: dict):
    tmp_path = _PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, _PATH)


def get_all() -> dict:
    with _lock:
        return _load()


def update(**kwargs):
    """Merges kwargs into the stored settings (None values are dropped, not
    stored, so callers can pass every field from a form without clobbering
    unrelated ones left blank). To actually clear/remove a key (e.g.
    disconnecting an integration), use delete() instead -- passing None
    here is a silent no-op by design, not a way to unset something."""
    with _lock:
        data = _load()
        data.update({k: v for k, v in kwargs.items() if v is not None})
        _save(data)


def delete(*keys):
    """Removes the given key(s) entirely from stored settings, if present.
    Unlike update(), this is how a value actually gets unset -- see
    google_client.disconnect(), which needs google_token gone, not merely
    left as None (update(google_token=None) would silently drop that kwarg
    and leave the old token in place)."""
    with _lock:
        data = _load()
        changed = False
        for key in keys:
            if key in data:
                del data[key]
                changed = True
        if changed:
            _save(data)


def session_secret() -> str:
    """Generated once, persisted, used to sign session cookies — stable
    across restarts so logins survive an app restart."""
    with _lock:
        data = _load()
        if "session_secret" not in data:
            data["session_secret"] = secrets.token_hex(32)
            _save(data)
        return data["session_secret"]


def get_or_create_device_id() -> str:
    """Generated once, persisted -- a stable short identifier for this
    install, distinct from session_secret (which is a signing key, not
    meant to be human-visible). Shown on the Account settings panel and
    included in the daily usage-digest email (poller._format_usage_digest)
    so a user running Clicky on more than one machine can tell which one a
    given digest is reporting on."""
    with _lock:
        data = _load()
        if "device_id" not in data:
            data["device_id"] = secrets.token_hex(4)
            _save(data)
        return data["device_id"]


def get_or_create_jarvis_device_api_key() -> str:
    """Generated once, persisted -- a shared secret the device (ESP32) must
    send when forwarding an already-decided Jarvis action to this app's
    /jarvis/execute-decision route (see app.py). See macOS settings.py for
    the full rationale."""
    with _lock:
        data = _load()
        if "jarvis_device_api_key" not in data:
            data["jarvis_device_api_key"] = secrets.token_hex(16)
            _save(data)
        return data["jarvis_device_api_key"]


def hash_password(password: str, salt: bytes = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"{salt.hex()}:{digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, _digest_hex = stored.split(":", 1)
    except (ValueError, AttributeError):
        return False
    salt = bytes.fromhex(salt_hex)
    return secrets.compare_digest(hash_password(password, salt), stored)


def is_configured() -> bool:
    """True once first-run setup (password + provider choice) has been
    completed. The /setup form itself is responsible for only saving a
    provider once its required API key field was actually filled in."""
    data = get_all()
    return bool(data.get("password_hash")) and bool(data.get("stt_provider")) and bool(data.get("llm_provider"))
