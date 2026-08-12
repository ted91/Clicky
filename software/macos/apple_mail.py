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
import difflib
import logging
import re
import subprocess
import time
import urllib.parse

log = logging.getLogger("apple_mail")

# How close a sender has to be to the searched name to count as a match.
# 0.78 accepts "Christina" -> "Christine" (0.89) and "Sanchit" ->
# "Sanchit Gupta", while rejecting unrelated words that merely share the
# stem ("Christina" vs "Christmas Shop"). Live-derived: a speaker
# transcribed as "Christina" had real correspondence under the display
# name "Christine James", and exact matching found nothing at all.
_NAME_SIMILARITY_THRESHOLD = 0.78

# How much of a message body to carry back when with_body=True. Enough to
# judge what a thread is actually about (and to quote from), while keeping
# a long newsletter or a deep reply chain from dominating the payload.
MAX_BODY_CHARS = 1500


def _search_stem(name: str) -> str:
    """The substring actually handed to Mail.app's `whose` clause.

    Deliberately a PREFIX of the name, not the whole thing: the whole
    thing is what fails when a transcribed name is one or two letters off
    from the real one. A short stem casts a wide net cheaply inside
    Mail.app, and _name_matches() below does the precise judging in
    Python, where real fuzzy comparison is available. Capped at 6 chars so
    a long surname doesn't re-introduce the exact-match problem, and
    floored at the full name for very short names (nothing to trim)."""
    name = (name or "").strip()
    first = name.split()[0] if name.split() else name
    return first[:6] if len(first) > 6 else first


def _sender_candidates(sender: str) -> list:
    """The comparable identity strings inside a Mail.app `sender` value
    ("Christine James <christine@jetztpat.com>") -- the display name, each
    of its words, and the email's local part. Any of them matching is
    enough, since a person shows up variously as "Christine", "Christine
    James", or just "christine@..."."""
    sender = sender or ""
    out = []
    m = re.match(r"\s*(.*?)\s*<([^>]+)>\s*$", sender)
    display, addr = (m.group(1), m.group(2)) if m else (sender, sender)
    display = display.strip().strip('"')
    if display:
        out.append(display)
        out.extend(display.split())
    if "@" in addr:
        out.append(addr.split("@", 1)[0])
    return [s for s in out if s]


def _name_matches(query: str, sender: str) -> bool:
    """Whether `sender` plausibly IS the person called `query`."""
    q = (query or "").strip().lower()
    if not q:
        return False
    for cand in _sender_candidates(sender):
        c = cand.lower()
        # Substring check needs a length floor -- otherwise a short candidate
        # word like "St" false-positives just by being a letter-sequence
        # inside a longer query ("chri-ST-ina"), with no relation to the
        # name at all. Live-caught: "St Christopher's & Flying Pig Hostels"
        # matched "Christina" purely because "St" is a substring of it.
        if len(c) >= 4 and (q in c or c in q):
            return True
        if difflib.SequenceMatcher(None, q, c).ratio() >= _NAME_SIMILARITY_THRESHOLD:
            return True
        # Compare first names too: "Christina Lopez" vs "Christine James"
        # should match on the first token even though the full strings
        # differ a lot.
        if difflib.SequenceMatcher(None, q.split()[0], c.split()[0] if c.split() else c
                                    ).ratio() >= _NAME_SIMILARITY_THRESHOLD:
            return True
    return False


def message_link(message_id: str) -> str:
    """Builds a message:// deep link that opens this exact email directly
    in Mail.app when clicked -- verified live: `open` on this URL scheme
    exits 0 with Mail.app registered as its handler, no separate app
    needed. message_id is Mail.app's own "message id of m" property (the
    RFC822 Message-ID header, without surrounding angle brackets); the
    scheme wants them present and percent-encoded."""
    return "message://" + urllib.parse.quote(f"<{message_id}>", safe="")


def _osascript(script: str, timeout: int = 90) -> str:
    # 90s, not 20s: searching every mailbox of every account was measured at
    # 4-23s on a real 4-account store (~8000 inbox messages plus archives),
    # so the old 20s limit turned a working search into a timeout -- and a
    # timeout looked identical to "no matching emails" in the UI.
    result = subprocess.run(
        ["osascript", "-e", script], timeout=timeout, check=True, capture_output=True, text=True,
    )
    return result.stdout


def search_messages(query: str, max_results: int = 15, fuzzy: bool = False,
                    with_body: bool = False) -> list:
    """Searches Mail.app for `query` as a case-insensitive substring of
    the sender or subject -- covers the two common watch inputs (an email
    address, or a distinctive word/phrase).

    fuzzy=True is for searching by a PERSON'S NAME that came from a
    transcript, where the spelling is only approximately right: it hands
    Mail.app a short stem (see _search_stem) and then keeps only senders
    that actually resemble the name (_name_matches). Live case that forced
    this: a speaker transcribed as "Christina" whose real correspondence
    is under "Christine James" -- exact matching returned nothing across
    every mailbox of every account. fuzzy=False (the default) keeps the
    literal behaviour the action-item watch wants, where the user typed
    the term themselves and means it exactly. Returns
    [{"id", "from", "subject", "message_id", "link", "timestamp"}], sorted
    newest first by "timestamp" (a Unix epoch float; None if Mail.app
    couldn't report a date for that message).
    "id" is Mail.app's own internal message id (stable within this Mail.app
    database, used as the dedup key for "have we already seen this one");
    "message_id"/"link" are the RFC822 Message-ID and the message:// deep
    link built from it (see message_link) -- what the dashboard actually
    opens. Raises RuntimeError on an AppleScript/Mail.app failure (e.g.
    Automation permission not yet granted) so the caller can log it
    without crashing the poll loop."""
    query = (query or "").strip()
    if not query:
        return []
    # In fuzzy mode Mail.app gets a deliberately broad stem and Python does
    # the real judging; the raw cap is raised because most stem hits get
    # filtered out below and we still want enough survivors.
    term = _search_stem(query) if fuzzy else query
    raw_cap = max_results * 6 if fuzzy else max_results
    # Body text is opt-in because it's the expensive part: `content of m`
    # forces Mail.app to load and decode each message body, which a
    # subject+sender listing never touches. Only the callers that actually
    # judge relevance or feed an LLM ask for it. Truncated inside
    # AppleScript rather than in Python so a 200KB newsletter is never
    # carried across the Apple Event bridge at all.
    body_clause = (
        "try\n"
        "                                    set bodyText to (content of m)\n"
        f"                                    if (length of bodyText) > {MAX_BODY_CHARS} then "
        f"set bodyText to (text 1 thru {MAX_BODY_CHARS} of bodyText)\n"
        "                                end try"
    ) if with_body else ""
    # Searches EVERY mailbox of EVERY account, not just the inbox.
    # Live-confirmed why: this Mac has 4 accounts whose mailboxes include
    # "All Mail", "Archive" and "Sent Mail", and an inbox holding ~8000
    # messages -- an inbox-only search found 0 hits for a person the user
    # had real correspondence with, because it had all been archived.
    # Measured at ~4-6s across the whole store, which is fine for a poll
    # that runs on a multi-second cycle.
    #
    # The per-mailbox `try` matters: some mailboxes (Outbox, sync-failure
    # folders, an account that's briefly unreachable) raise on a whose
    # clause, and without it one bad mailbox aborts the entire search.
    # whose-clause filtering runs inside Mail.app itself (much faster than
    # fetching everything and filtering in Python) -- "contains" is
    # case-insensitive in AppleScript by default.
    # Bulk property access (`id of matchList`) was tried and abandoned:
    # Mail.app's `whose`-filtered result is a list of raw message
    # references, and asking for a property "of" that whole list throws
    # "Can't get id of {message id ... }" -- a known AppleScript/Mail.app
    # quirk, not a mistake in the query. So properties are fetched one
    # message at a time instead. This is fine in practice because the
    # `whose` filter itself (evaluated inside Mail.app, not fetched over
    # the Apple Event bridge) is what's expensive on a huge mailbox, and
    # that cost is identical either way -- confirmed live: capped at
    # {raw_cap} total per-message fetches, a full 4-account/6-mailbox
    # sweep completed in ~5s.
    #
    # ASCII 1/2 as delimiters, not tab/newline -- subjects legitimately
    # contain both, and a subject with a tab in it would otherwise shift
    # every following field by one and corrupt the parse.
    # Date is sent back as a SECONDS-FROM-NOW delta (`theDate - (current
    # date)`, AppleScript's own date-subtraction), not a formatted date
    # string -- date-string formatting is locale-dependent and would need
    # fragile parsing back in Python, whereas a bare number of seconds
    # relative to "now" (whichever "now" this Apple Event ran at) reliably
    # reconstructs into a real timestamp with one addition. Falls back to
    # "date sent" for Sent-mailbox messages, which have no "date received".
    script = f'''
    tell application "Mail"
        set AppleScript's text item delimiters to (ASCII character 1)
        set outStr to ""
        set n to 0
        repeat with acct in accounts
            repeat with mb in (every mailbox of acct)
                if n < {raw_cap} then
                    try
                        set matchList to (messages of mb whose (sender contains "{_escape(term)}" or subject contains "{_escape(term)}"))
                        repeat with m in matchList
                            if n >= {raw_cap} then exit repeat
                            try
                                set secDelta to 0
                                try
                                    set secDelta to ((date received of m) - (current date))
                                on error
                                    try
                                        set secDelta to ((date sent of m) - (current date))
                                    end try
                                end try
                                set bodyText to ""
                                {body_clause}
                                set outStr to outStr & (id of m as string) & (ASCII character 1) & ¬
                                    (sender of m) & (ASCII character 1) & ¬
                                    (subject of m) & (ASCII character 1) & ¬
                                    (message id of m) & (ASCII character 1) & ¬
                                    (secDelta as string) & (ASCII character 1) & ¬
                                    bodyText & (ASCII character 2)
                                set n to n + 1
                            end try
                        end repeat
                    end try
                end if
            end repeat
        end repeat
        return outStr
    end tell
    '''
    try:
        raw = _osascript(script)
        now = time.time()
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Mail.app search failed (Automation permission not granted yet?): {e.stderr or e}")
    except subprocess.TimeoutExpired:
        raise RuntimeError("Mail.app search timed out")

    out = []
    for line in raw.split("\x02"):
        if not line.strip():
            continue
        parts = line.split("\x01")
        if len(parts) < 4:
            continue
        mid = parts[3]
        sender = parts[1]
        # The stem search is intentionally over-broad, so a stem hit is only
        # a real match if the sender genuinely resembles the person.
        if fuzzy and not _name_matches(query, sender):
            continue
        try:
            timestamp = now + float(parts[4]) if len(parts) > 4 else None
        except ValueError:
            timestamp = None
        # Collapse whitespace: mail bodies arrive full of hard-wrapped
        # newlines and quoted-reply indentation, which waste room in the
        # embedding/LLM payload without carrying meaning.
        body = re.sub(r"\s+", " ", parts[5]).strip() if len(parts) > 5 else ""
        out.append({"id": parts[0], "from": sender, "subject": parts[2],
                    "message_id": mid, "link": message_link(mid) if mid else None,
                    "timestamp": timestamp, "body": body})
    # Sorted newest-first BEFORE the max_results cut -- a mailbox is walked
    # in whatever order Mail.app iterates it, not date order, so without
    # this the returned N could easily be the N oldest matches found first
    # rather than the N most recent (what "latest first" actually means).
    # Missing timestamp sorts last, not first -- an unknown date is not
    # evidence of being recent.
    out.sort(key=lambda m: m["timestamp"] if m["timestamp"] is not None else -1, reverse=True)
    return out[:max_results]


def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')
