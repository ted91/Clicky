"""AI-pager notification feed: aggregates a few Mac-side sources and pushes
each as a short push to the device (ble_device_client.send_notification /
device_client equivalent), announced there with a click + shown on the
e-paper until BOOT-dismissed (see ble_sync.cpp's NOTIFY command).

Sources: Gmail unread inbox, upcoming calendar events, and macOS's own
Notification Center history (mac_notifications.py -- reads its private
SQLite store directly, no Swift agent involved; needs Full Disk Access,
see that module's docstring). Each gated by its own settings toggle
(notify_gmail_enabled/notify_calendar_enabled/notify_mac_enabled, set from
/settings' Notifications panel) so a disabled source is a cheap no-op.

De-duped by a small on-disk "already sent" id set so a restart doesn't
replay history and a slow poll loop doesn't re-push the same item.

Gmail and Mac notifications additionally pass through an AI importance
filter before reaching the device (see _passes_importance_filter) -- a
VIP allow-list bypasses it entirely, otherwise a sensitivity-tuned LLM
verdict decides. Calendar reminders are exempt (already low-volume and
inherently time-scoped). A skipped item is still marked "seen" so it's
judged once, not every poll cycle.
"""
import json
import logging
import os
import time

import config
import mac_notifications
import paths
import settings

log = logging.getLogger("notifications")

_SEEN_PATH = os.path.join(paths.APP_DATA_DIR, "notifications_seen.json")
_MAX_SEEN = 500  # bounded so this file doesn't grow forever

_last_gmail_error_log = 0
_GMAIL_ERROR_LOG_INTERVAL = 300  # don't spam logs every poll cycle on a 403


def _load_seen() -> set:
    try:
        with open(_SEEN_PATH) as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def _save_seen(seen: set):
    trimmed = list(seen)[-_MAX_SEEN:]
    with open(_SEEN_PATH, "w") as f:
        json.dump(trimmed, f)


def _push(transport, title: str, body: str, source_id: str, seen: set) -> bool:
    if source_id in seen:
        return False
    try:
        transport.send_notification(title, body)
    except Exception as e:
        log.warning("notifications: failed to push '%s': %s", title, e)
        return False
    seen.add(source_id)
    import analytics
    analytics.track_event("notifications_sent")
    return True


def _is_vip(text: str, vip_list: str) -> bool:
    needles = [n.strip().lower() for n in vip_list.splitlines() if n.strip()]
    if not needles:
        return False
    haystack = text.lower()
    return any(n in haystack for n in needles)


def _passes_importance_filter(title: str, body: str) -> bool:
    """Gmail/Mac notification sources only (see _check_gmail/_check_mac) --
    Calendar reminders are already low-volume and time-scoped, so they skip
    this entirely. Anyone on the VIP list always passes, no LLM call spent.
    Otherwise a single one-word LLM verdict (see providers.base's
    build_importance_prompt) decides. Fails OPEN (pushes through) on any
    classification error -- a broken classifier silently eating every
    notification would be a third version of the exact bug this session
    already spent time debugging (Gmail scope, Full Disk Access), not
    something to risk introducing here."""
    saved = settings.get_all()
    if _is_vip(f"{title} {body}", saved.get("notification_vip_list", "")):
        return True
    sensitivity = saved.get("notification_sensitivity", "medium")
    try:
        from providers import get_completer
        from providers.base import build_importance_prompt
        _, complete = get_completer()
        verdict = complete(build_importance_prompt(title, body, sensitivity))
        return verdict.strip().upper() == "IMPORTANT"
    except Exception as e:
        log.warning("notifications: importance check failed, pushing through: %s", e)
        return True


def _check_gmail(transport, seen: set):
    global _last_gmail_error_log
    if not settings.get_all().get("notify_gmail_enabled"):
        return
    import google_client
    if not google_client.is_connected():
        return
    try:
        messages = google_client.list_unread_messages(newer_than="1h", max_results=5)
    except Exception as e:
        now = time.monotonic()
        if now - _last_gmail_error_log > _GMAIL_ERROR_LOG_INTERVAL:
            _last_gmail_error_log = now
            log.warning("notifications: gmail check failed: %s", e)
        return
    for msg in messages:
        source_id = f"gmail:{msg['id']}"
        if source_id in seen:
            continue
        if not _passes_importance_filter(msg["subject"], msg["from"] or ""):
            seen.add(source_id)
            continue
        _push(transport, msg["from"] or "New email", msg["subject"], source_id, seen)


def _check_calendar(transport, seen: set):
    if not settings.get_all().get("notify_calendar_enabled"):
        return
    import google_client
    if not google_client.is_connected():
        return
    try:
        event = google_client.current_or_next_event(window_min=10)
    except Exception as e:
        log.debug("notifications: calendar check failed: %s", e)
        return
    if not event:
        return
    # Same event pushed repeatedly until it starts would be noise -- key by
    # event id, not by poll tick, so it's announced exactly once.
    event_id = event.get("id") or event.get("start")
    if not event_id:
        return
    title = event.get("summary") or "Upcoming event"
    when = event.get("start") or ""
    _push(transport, title, f"Starting soon ({when})", f"cal:{event_id}", seen)


_MAC_LOOKBACK_SECONDS = 600  # generous re-scan window each poll -- cheap, since the seen-id set (not a stored timestamp) is what actually prevents re-pushing


def _check_mac(transport, seen: set):
    if not settings.get_all().get("notify_mac_enabled"):
        return
    notifs = mac_notifications.get_recent_notifications(
        since_epoch=time.time() - _MAC_LOOKBACK_SECONDS, max_results=10)
    # Oldest first so a burst of several notifications arrives on the pager
    # in the order they actually happened, not reverse-chronological.
    for n in reversed(notifs):
        if n["id"] in seen:
            continue
        if not _passes_importance_filter(n["title"], n["body"]):
            seen.add(n["id"])
            continue
        _push(transport, n["title"], n["body"], n["id"], seen)


def check_once(transport):
    """Called once per poll_once() cycle (see poller.py). Cheap no-ops when
    a source is disabled/unconfigured."""
    seen = _load_seen()
    before = len(seen)
    _check_gmail(transport, seen)
    _check_calendar(transport, seen)
    _check_mac(transport, seen)
    if len(seen) != before:
        _save_seen(seen)
