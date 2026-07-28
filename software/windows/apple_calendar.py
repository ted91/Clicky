"""Apple Calendar (macOS Calendar.app / iCloud / Exchange / any account
configured in Calendar) as a second, optional calendar source alongside
Google Calendar -- for people who use Apple's own Calendar rather than
Google Calendar. No OAuth, no developer setup: this is a native macOS
EventKit query running inside the meetingcap Swift agent (already a
persistent process with its own permission model, see meeting_recorder.py
and meetingcap/main.swift), gated by the standard macOS "Calendars"
privacy permission -- the same TCC prompt flow as Screen Recording/
Microphone, not a Cloud Console integration.

This module is a thin wrapper so poller.py can treat this and
google_client.current_or_next_event() interchangeably -- same return
shape: {title, start, end, meeting_url, attendees:[{name,email}]} or None.
"""
import logging

import meeting_recorder

log = logging.getLogger("apple_calendar")


def is_available() -> bool:
    """True if the menu-bar agent (which owns the actual EventKit query)
    is running -- calendar permission itself is checked lazily on the
    first real query, not here, since that's a one-time TCC prompt best
    triggered by an actual lookup rather than a separate up-front check."""
    return meeting_recorder.state().get("available", False)


def current_or_next_event(window_min: int = 15):
    """Returns the calendar event currently in progress, or the next one
    starting within `window_min` minutes, matching
    google_client.current_or_next_event()'s shape -- or None if nothing
    qualifies, EventKit access hasn't been granted, or the agent isn't
    reachable. Failures here are meant to be non-fatal to the caller, same
    as the Google path."""
    try:
        return meeting_recorder.get_apple_calendar_event(window_min)
    except Exception as e:
        log.warning("Apple Calendar lookup failed: %s", e)
        return None
