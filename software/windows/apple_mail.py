"""Mac Mail.app (Mail) search via AppleScript -- backs the action-item
email watch feature (poller.check_email_watches_once). Deliberately not
Gmail: whatever account(s) are configured in Mail.app (iCloud, Gmail,
Exchange, IMAP, anything), no separate OAuth/API setup, consistent with
jarvis.py's existing Mail.app draft/send AppleScript automation (same
one-time Automation permission prompt, no new TCC category).

No date-range query here -- AppleScript date literals are locale-dependent
and fragile to build reliably from a stored ISO string, so instead this
just returns the most recent N matching messages; the caller (storage's
watch_seen_ids) tracks which message ids have already been seen/alerted
on, so a newly-arrived match is detected by "not in the seen set" rather
than by date comparison. Works the same regardless of which mail account(s)
Mail.app has configured.
"""
import logging
import subprocess

log = logging.getLogger("apple_mail")


def _osascript(script: str, timeout: int = 20) -> str:
    result = subprocess.run(
        ["osascript", "-e", script], timeout=timeout, check=True, capture_output=True, text=True,
    )
    return result.stdout


def search_messages(query: str, max_results: int = 15) -> list:
    """Searches Mail.app's inbox for `query` as a case-insensitive
    substring of the sender or subject -- covers the two common watch
    inputs (an email address, or a distinctive word/phrase). Returns
    [{"id", "from", "subject"}], most recent first. Raises RuntimeError on
    an AppleScript/Mail.app failure (e.g. Automation permission not yet
    granted) so the caller can log it without crashing the poll loop."""
    query = (query or "").strip()
    if not query:
        return []
    # whose-clause filtering runs inside Mail.app itself (much faster than
    # fetching everything and filtering in Python) -- "contains" is
    # case-insensitive in AppleScript by default.
    script = f'''
    tell application "Mail"
        set matchList to (messages of inbox whose (sender contains "{_escape(query)}" or subject contains "{_escape(query)}"))
        set outStr to ""
        set n to 0
        repeat with m in matchList
            if n >= {max_results} then exit repeat
            set outStr to outStr & (id of m as string) & "\\t" & (sender of m) & "\\t" & (subject of m) & "\\n"
            set n to n + 1
        end repeat
        return outStr
    end tell
    '''
    try:
        raw = _osascript(script)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Mail.app search failed (Automation permission not granted yet?): {e.stderr or e}")
    except subprocess.TimeoutExpired:
        raise RuntimeError("Mail.app search timed out")

    out = []
    for line in raw.strip("\n").split("\n"):
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        out.append({"id": parts[0], "from": parts[1], "subject": parts[2]})
    return out


def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')
