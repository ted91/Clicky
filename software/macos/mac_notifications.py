"""Reads macOS's own Notification Center history directly from its private,
undocumented SQLite store -- there is no public API for "list recent
notifications" on macOS, so this is the only way to mirror them to the
pager. Requires Full Disk Access granted to whichever process runs this
(Terminal/python during dev, Clicky.app once packaged) -- System Settings >
Privacy & Security > Full Disk Access -- since the store lives under
~/Library/Group Containers, which SIP treats as protected user data.
Degrades to an empty list (never raises past get_recent_notifications) if
that permission hasn't been granted yet.

Each row's `data` blob is itself a binary plist (confirmed live via
plistlib, NOT an NSKeyedArchiver graph as its presence in other Apple
databases might suggest) with the actual notification content nested under
a "req" key -- {"req": {"titl": "...", "body": "...", ...}, "app": "...",
...}. Key names ("titl"/"body") are undocumented and could change across
macOS versions; this reads them defensively (missing/wrong-typed values
just mean that notification is skipped, never an exception).
"""
import os
import plistlib
import sqlite3

DB_PATH = os.path.expanduser("~/Library/Group Containers/group.com.apple.usernoted/db2/db")

# macOS/Cocoa's "reference date" epoch (2001-01-01), used by
# NSDate/CFAbsoluteTime and hence by this database's *_date columns --
# offset from Unix epoch (1970-01-01) in seconds. Confirmed live: dates
# read back ~56 years off (into the 1970s) without this adjustment.
COCOA_EPOCH_OFFSET = 978307200


def _extract_title_body(blob: bytes):
    """Best-effort pull of (title, body) out of one row's plist blob.
    Returns (None, None) if the blob doesn't parse or doesn't have the
    expected shape -- never raises, this is inherently best-effort against
    an undocumented, version-drifting format."""
    try:
        top = plistlib.loads(blob)
    except Exception:
        return None, None
    req = top.get("req") if isinstance(top, dict) else None
    if not isinstance(req, dict):
        return None, None
    title = req.get("titl")
    body = req.get("body")
    return (title if isinstance(title, str) else None,
            body if isinstance(body, str) else None)


def get_recent_notifications(since_epoch: float, max_results: int = 20) -> list:
    """Returns [{"id","app","title","body","date"}] for notifications
    delivered after since_epoch (Unix timestamp), newest first. Never
    raises -- returns [] on any failure (DB missing, Full Disk Access not
    granted, unparseable blob for a given row), since this is a best-effort
    convenience feed, not something that should ever break the poll loop.
    """
    try:
        # mode=ro only -- NOT immutable=1. Confirmed live: immutable=1 tells
        # SQLite the file (and its WAL) will never change, which makes it
        # skip reading the WAL entirely and go straight to the base file --
        # but notificationcenterd runs in WAL mode and the most recent
        # notifications live in db-wal, uncheckpointed, until macOS gets
        # around to it. With immutable=1 this returned zero rows for
        # anything delivered since the last checkpoint; without it, the
        # same read-only connection sees the WAL like any normal reader.
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        return []

    try:
        since_cocoa = since_epoch - COCOA_EPOCH_OFFSET
        # Confirmed live: request_date is NULL on every row on this macOS
        # version (perhaps only populated for notifications still pending
        # delivery/snooze) -- delivered_date is the column that's actually
        # populated once a notification has been shown, which is exactly
        # the "was this shown to the user" moment we want to mirror.
        cur = con.execute(
            """SELECT record.rec_id, app.identifier, record.data, record.delivered_date
               FROM record JOIN app ON record.app_id = app.app_id
               WHERE record.delivered_date > ?
               ORDER BY record.delivered_date DESC LIMIT ?""",
            (since_cocoa, max_results),
        )
        out = []
        for rec_id, app_id, data, delivered_date in cur.fetchall():
            title, body = _extract_title_body(data)
            if not title and not body:
                continue  # nothing displayable extracted -- skip rather than push a blank pager message
            out.append({
                "id": f"macnotif:{rec_id}",
                "app": app_id,
                "title": title or app_id,
                "body": body or "",
                "date": delivered_date + COCOA_EPOCH_OFFSET,
            })
        return out
    except sqlite3.Error:
        return []
    finally:
        con.close()
