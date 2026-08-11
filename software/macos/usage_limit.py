"""Meters recorded-audio minutes against a monthly cap, but ONLY while the
app is running on the developer's bundled demo API keys (see config.py's
MISTRAL_API_KEY/DEEPGRAM_API_KEY .env fallback) -- a user who has entered
their own key is uncapped, since they're spending their own quota, not the
developer's.

Deliberately a SEPARATE file from analytics.py, not an extra field on its
"period" bucket: that bucket is destructive (reset_period() wipes it after
every digest email, see poller.check_usage_report_once) and daily, whereas
a usage cap needs a calendar-month bucket that survives independently of
the reporting cadence.

This is an honest local meter, not real enforcement -- it lives in a
plain JSON file the user's own machine can edit or delete. That's an
accepted tradeoff for a trusted-demo-user cap, not a security boundary;
real enforcement would need a server holding the counter, which is a much
bigger build (see this session's planning notes).
"""
import json
import logging
import os
import threading
from datetime import datetime, timezone

import config
import paths
import settings

log = logging.getLogger("usage_limit")

# Maps a provider name to the settings.json field a user's own key would
# live in -- same mapping as app.py's PROVIDERS_NEEDING_KEY, duplicated
# here rather than imported to avoid a poller<->app.py import cycle.
_PROVIDER_KEY_FIELD = {
    "mistral": "mistral_api_key",
    "openai": "openai_api_key",
    "anthropic": "anthropic_api_key",
    "deepgram": "deepgram_api_key",
}


def is_using_bundled_key(provider: str) -> bool:
    """True if `provider` currently resolves to the developer's bundled
    .env key rather than a key the user entered themselves -- the signal
    for whether this recording's processing should count against the
    monthly cap at all. A user-supplied key is never metered."""
    key_field = _PROVIDER_KEY_FIELD.get(provider)
    if key_field is None:
        return False  # "local" (Ollama/faster-whisper) needs no key, never metered
    if settings.get_all().get(key_field):
        return False  # user entered their own key
    return bool(getattr(config, key_field.upper(), ""))

_LIMIT_PATH = os.path.join(paths.APP_DATA_DIR, "usage_limit.json")
_lock = threading.Lock()

MONTHLY_CAP_MINUTES = 100


def _current_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _load() -> dict:
    try:
        with open(_LIMIT_PATH) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    month = _current_month()
    if data.get("month") != month:
        # New calendar month (or first run) -- roll the counter over.
        data = {"month": month, "seconds_used": 0.0}
    return data


def _save(data: dict):
    tmp_path = _LIMIT_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, _LIMIT_PATH)


def record_seconds(seconds: float):
    """Adds to this month's usage. Best-effort -- a broken write here must
    never break the recording it's counting, same contract as
    analytics.track_event()."""
    if seconds <= 0:
        return
    try:
        with _lock:
            data = _load()
            data["seconds_used"] = data.get("seconds_used", 0.0) + seconds
            _save(data)
    except Exception as e:
        log.warning("usage_limit.record_seconds() failed (non-fatal): %s", e)


def minutes_used() -> float:
    with _lock:
        return _load().get("seconds_used", 0.0) / 60.0


def minutes_remaining() -> float:
    return max(0.0, MONTHLY_CAP_MINUTES - minutes_used())


def is_over_limit() -> bool:
    return minutes_used() >= MONTHLY_CAP_MINUTES
