"""Lightweight usage tracking -- purely for the developer's own visibility
into their own device (see poller.check_usage_report_once, which emails a
periodic digest to the developer's own address). This is not third-party
telemetry: there's no vendor on the other end, no opt-out flow, and nothing
leaves the machine except the digest email the user themself configured.

Persistence: a dedicated JSON file under paths.APP_DATA_DIR, same
atomic-write shape as storage.py's tombstone file (_load_tombstones/
_save_tombstones) -- settings.json is for user-editable config, not a
write-heavy counters dict.
"""
import json
import logging
import os
import threading
from datetime import datetime, timezone

import paths

log = logging.getLogger("analytics")

_STATS_PATH = os.path.join(paths.APP_DATA_DIR, "usage_stats.json")
_lock = threading.Lock()

_DEFAULT_PERIOD = {
    "period_start": None,
    "recordings_count": 0,
    "recordings_journal_count": 0,
    "recordings_actionable_count": 0,
    "total_recording_seconds": 0.0,
    "stt_provider_counts": {},
    "llm_provider_counts": {},
    "notion_pushes": 0,
    "obsidian_pushes": 0,
    "social_posts_generated": {},
    "social_posts_published": {},
    "notifications_sent": 0,
    "drafts_approved": 0,
    "feedback_submitted_count": 0,
}


def _load() -> dict:
    try:
        with open(_STATS_PATH) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    data.setdefault("period", dict(_DEFAULT_PERIOD))
    data.setdefault("all_time", dict(_DEFAULT_PERIOD))
    data.setdefault("last_report_date", None)
    if not data["period"].get("period_start"):
        data["period"]["period_start"] = datetime.now(timezone.utc).isoformat()
    return data


def _save(data: dict):
    tmp_path = _STATS_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, _STATS_PATH)


def track_event(name: str, count: int = 1, seconds: float = 0.0, key: str = None):
    """Increments a named counter (and its all-time twin). `key` is for the
    per-provider/per-platform dict counters (e.g.
    track_event("stt_provider_counts", key="mistral")); `seconds` adds to a
    running total (e.g. track_event("total_recording_seconds", seconds=42.3)).
    Best-effort -- a broken counter write must never break the feature
    being counted, so any exception here is logged and swallowed, never
    raised back to the caller."""
    try:
        with _lock:
            data = _load()
            for bucket in (data["period"], data["all_time"]):
                if key is not None:
                    bucket.setdefault(name, {})
                    bucket[name][key] = bucket[name].get(key, 0) + count
                elif name == "total_recording_seconds":
                    bucket[name] = bucket.get(name, 0.0) + seconds
                else:
                    bucket[name] = bucket.get(name, 0) + count
            _save(data)
    except Exception as e:
        log.warning("analytics.track_event(%r) failed (non-fatal): %s", name, e)


def get_period_summary() -> dict:
    with _lock:
        return _load()["period"]


def last_report_date() -> str:
    """ISO date string (YYYY-MM-DD) of the last successfully-sent digest,
    or None if one has never been sent -- used by
    poller.check_usage_report_once()'s wall-clock due-check."""
    with _lock:
        return _load().get("last_report_date")


def reset_period(report_date: str):
    """Called after a successful digest send -- rolls the period bucket
    back to empty (all_time is untouched, it's a separate running total)
    and records the date so the next report isn't due until tomorrow."""
    with _lock:
        data = _load()
        data["period"] = dict(_DEFAULT_PERIOD)
        data["period"]["period_start"] = datetime.now(timezone.utc).isoformat()
        data["last_report_date"] = report_date
        _save(data)
