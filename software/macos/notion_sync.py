"""Pushes a processed recording into a linked set of Notion databases —
mirroring how Notion is normally used (small databases joined by Relations)
rather than one database wearing every hat:

  Notes (existing "recordings" database) -- one page per voice memo, holds
    the summary/transcript. Source of truth; everything else relates to it.
  Tasks -- one page per action item. Has its own Status/Due Date, and a
    real `people`-type Assignee -- reserved for actual Notion workspace
    members, since assigning that property emails them.
  People -- one page per person mentioned (stakeholders, and the speaker
    themself, per poller.py's _add_speakers_as_stakeholders). NOT the
    `people` property type -- these are often not Notion accounts at all,
    so this is a plain lightweight directory linked by Relation, which
    costs nothing and never notifies anyone.
  Calendar -- one page per calendar event mentioned in the recording.

All four are joined by two-way Relation properties (created once, in
notion_sync_setup work) so a Notes page shows its related Tasks/People/
Calendar entries automatically -- no manual back-linking needed here,
Notion mirrors dual-property relations for us.

Uses Notion's REST API directly via `requests` (already a core dependency)
rather than the official notion-client SDK, to avoid another install for
what's a handful of endpoint calls.

Setup (documented in full in pipeline/README.md):
1. Create an integration at https://www.notion.so/my-integrations, copy its
   "Internal Integration Token".
2. Create (or pick) a database in Notion, share it with that integration
   (••• menu -> Connections -> your integration).
3. Paste the token and database IDs into /integrations.
"""
import logging

import requests

import settings
import storage
from providers.base import merge_consecutive_segments

log = logging.getLogger("notion_sync")

API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2026-03-11"
MAX_TEXT_LEN = 1900  # Notion rich_text blocks cap at 2000 chars; leave margin

# The Notes database has "Speaker 1".."Speaker 6" rich_text properties
# (added once, out-of-band -- see pipeline/README.md) so a speaker can be
# renamed directly in Notion, not just the dashboard. 6 covers the
# overwhelming majority of voice-memo/small-meeting recordings; a
# recording with more distinct speakers than this just doesn't get slots
# past #6 editable in Notion (still editable on the dashboard, no cap
# there since it's keyed by the actual speaker_id, not a fixed slot).
SPEAKER_SLOT_COUNT = 6


def speaker_slot_index(speaker_id: str):
    """Maps a diarization speaker_id ("speaker_1", "speaker_2", ...) to its
    Notion "Speaker N" property slot. Returns None if the id has no
    trailing number (defensive -- observed diarization output always has
    this shape, but nothing enforces it) or falls outside the slot range."""
    import re
    m = re.search(r"(\d+)$", speaker_id or "")
    if not m:
        return None
    idx = int(m.group(1))
    return idx if 1 <= idx <= SPEAKER_SLOT_COUNT else None


def _headers():
    token = settings.get_all().get("notion_token")
    if not token:
        raise RuntimeError("Notion isn't configured — set a token in /integrations")
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def get_page(page_id: str) -> dict:
    """Fetches a page's current properties -- used by
    poller.check_notion_email_approvals_once() to read a Task page's
    "Approve & Send" checkbox each poll cycle."""
    resp = requests.get(f"{API_BASE}/pages/{page_id}", headers=_headers(), timeout=15)
    if not resp.ok:
        raise RuntimeError(f"Notion API error {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def append_blocks(page_id: str, blocks: list):
    """Appends children blocks to an existing page without touching what's
    already there -- same PATCH .../blocks/{id}/children pattern as
    update_transcript_blocks(), generalized for any page. Used to log a
    "Sent ..." confirmation onto a Task page and a copy of the sent email
    onto the recipient's People page (see
    poller.check_notion_email_approvals_once())."""
    resp = requests.patch(
        f"{API_BASE}/blocks/{page_id}/children", headers=_headers(),
        json={"children": blocks}, timeout=15,
    )
    if not resp.ok:
        raise RuntimeError(f"Notion API error {resp.status_code}: {resp.text[:300]}")


def _text_block(kind: str, text: str):
    """Notion caps rich_text segments at 2000 chars -- chunk long text
    across multiple blocks rather than truncating it."""
    blocks = []
    for i in range(0, len(text), MAX_TEXT_LEN) or [0]:
        chunk = text[i:i + MAX_TEXT_LEN] if text else ""
        blocks.append({
            "object": "block",
            "type": kind,
            kind: {"rich_text": [{"type": "text", "text": {"content": chunk}}]},
        })
    return blocks


def _heading(text: str):
    return {
        "object": "block",
        "type": "heading_2",
        "heading_2": {"rich_text": [{"type": "text", "text": {"content": text}}]},
    }


def _bulleted(items: list):
    return [
        {
            "object": "block",
            "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": [{"type": "text", "text": {"content": item[:MAX_TEXT_LEN]}}]},
        }
        for item in items
    ]


def _build_blocks(record: dict) -> list:
    """Every recording's Notes page uses the same fixed set of section
    headings, always -- Summary/Action items/Follow-ups/Stakeholders/
    Calendar events/(Audio Intelligence)/Transcript -- rather than only
    showing whichever ones happen to have content. This keeps the page
    structure predictable and scannable across all your recordings; an
    empty section just shows "(none)" instead of vanishing, so you're
    never wondering whether the summarizer skipped a category or the LLM
    genuinely found nothing there."""
    summary = record.get("summary") or {}
    blocks = [_heading("Summary")]
    blocks += _text_block("paragraph", summary.get("summary") or "(no summary)")

    action_items = summary.get("action_items") or []
    blocks.append(_heading("Action items"))
    if action_items:
        lines = []
        for item in action_items:
            line = item.get("text", "")
            if item.get("owner"):
                line += f" — {item['owner']}"
            if item.get("due_date"):
                line += f" (due {item['due_date']})"
            lines.append(line)
        blocks += _bulleted(lines)
    else:
        blocks += _bulleted(["(none)"])

    follow_ups = summary.get("follow_ups") or []
    blocks.append(_heading("Follow-ups"))
    if follow_ups:
        lines = [f"{fu.get('text', '')}" + (f" — {fu['owner']}" if fu.get("owner") else "") for fu in follow_ups]
        blocks += _bulleted(lines)
    else:
        blocks += _bulleted(["(none)"])

    stakeholders = summary.get("stakeholders") or []
    blocks.append(_heading("Stakeholders"))
    if stakeholders:
        lines = [s.get("name", "") + (f" — {s['note']}" if s.get("note") else "") for s in stakeholders]
        blocks += _bulleted(lines)
    else:
        blocks += _bulleted(["(none)"])

    calendar_events = summary.get("calendar_events") or []
    blocks.append(_heading("Calendar events"))
    if calendar_events:
        lines = []
        for ev in calendar_events:
            line = ev.get("title", "")
            if ev.get("date"):
                line += f" — {ev['date']}"
            if ev.get("time"):
                line += f" {ev['time']}"
            lines.append(line)
        blocks += _bulleted(lines)
    else:
        blocks += _bulleted(["(none)"])

    # Deepgram Audio Intelligence insights block (only present when Deepgram was
    # the STT provider and the response included intelligence features).
    insights = record.get("deepgram_insights") or {}
    insight_lines = []
    if insights.get("summary"):
        insight_lines.append(f"Deepgram summary: {insights['summary']}")
    if insights.get("topics"):
        insight_lines.append(f"Topics: {', '.join(insights['topics'][:8])}")
    if insights.get("intents"):
        insight_lines.append(f"Intents: {', '.join(insights['intents'][:8])}")
    if insights.get("entities"):
        persons = [e["value"] for e in insights["entities"] if e.get("label") in ("PER", "PERSON")]
        orgs = [e["value"] for e in insights["entities"] if e.get("label") in ("ORG", "ORGANIZATION")]
        if persons:
            insight_lines.append(f"People detected: {', '.join(persons[:8])}")
        if orgs:
            insight_lines.append(f"Organizations detected: {', '.join(orgs[:6])}")
    if insight_lines:
        blocks.append(_heading("Audio Intelligence"))
        blocks += _bulleted(insight_lines)

    blocks.append(_heading("Transcript"))
    blocks += _text_block("paragraph", _transcript_text(record))

    return _cap_blocks(blocks)


def _transcript_text(record: dict) -> str:
    """Shared by _build_blocks and _build_journal_blocks -- renders the
    recording's transcript with speaker labels when diarized segments
    exist, else falls back to the plain transcript string."""
    segments = record.get("segments")
    if not segments:
        return record.get("transcript") or ""
    speaker_names = record.get("speaker_names") or {}

    def _label(seg):
        sid = seg.get("speaker_id", "?")
        return speaker_names.get(sid) or f"Speaker {sid}"

    # Consecutive same-speaker segments merged first (see
    # merge_consecutive_segments) -- diarization splits even one
    # uninterrupted turn into many short segments, which otherwise
    # reads as a dozen choppy one-liners instead of one coherent block.
    return "\n".join(
        f"{_label(seg)}: {seg.get('text', '').strip()}" for seg in merge_consecutive_segments(segments)
    )


def _cap_blocks(blocks: list) -> list:
    """Notion caps page-creation children at 100 blocks -- summary/action
    items/etc. are always small, but a long transcript could chunk into
    many paragraph blocks. Truncate rather than fail the whole push."""
    MAX_BLOCKS = 100
    if len(blocks) > MAX_BLOCKS:
        blocks = blocks[:MAX_BLOCKS - 1] + [_text_block("paragraph", "[transcript truncated — see the pipeline dashboard for the full text]")[0]]
    return blocks


def _build_journal_blocks(record: dict) -> list:
    """Journal-specific page layout -- Reflection/Key Learnings/Action
    Items/Notable Points/Transcript, built from the dedicated LLM call in
    poller.process_once (providers.base.build_journal_writeup_prompt),
    instead of reusing _build_blocks' meeting-oriented Summary/Action
    items/Follow-ups/Stakeholders/Calendar events layout -- which reads as
    empty meeting boilerplate on a self-reflective entry (a journal has no
    stakeholders or calendar logistics). Falls back to _build_blocks for a
    recording processed before this existed (no "journal_writeup" key yet)
    or if that LLM call failed, so older/degraded entries still get a
    usable page rather than an empty one."""
    writeup = (record.get("summary") or {}).get("journal_writeup")
    if not writeup:
        return _build_blocks(record)

    blocks = [_heading("Reflection")]
    blocks += _text_block("paragraph", writeup.get("reflection") or "(no reflection)")

    blocks.append(_heading("Key Learnings"))
    blocks += _bulleted(writeup.get("key_learnings") or ["(none)"])

    blocks.append(_heading("Action Items"))
    blocks += _bulleted(writeup.get("action_items") or ["(none)"])

    blocks.append(_heading("Notable Points"))
    blocks += _bulleted(writeup.get("notable_points") or ["(none)"])

    blocks.append(_heading("Transcript"))
    blocks += _text_block("paragraph", _transcript_text(record))

    return _cap_blocks(blocks)


def push_recording(record: dict) -> str:
    """Creates a new Notion page in the Notes database for this recording.
    Returns the created page's id (used to relate Tasks/People/Calendar
    entries back to it). Raises on failure -- caller (poller.py) is
    responsible for retry bookkeeping.

    Deliberately does NOT try to guess Assignee/Person on this page anymore
    -- those are legacy `people`-type properties left over from before the
    Tasks/People split, and matching them from transcript text risked
    emailing the wrong real Notion account. Assignee now only ever gets set
    on Tasks (push_tasks, exact-match only); "who's mentioned" now lives in
    the People database (push_people), which can't notify anyone."""
    database_id = settings.get_all().get("notion_database_id")
    if not database_id:
        raise RuntimeError("Notion isn't configured — set a database ID in /integrations")

    ensure_insight_properties(database_id)

    title = record.get("summary", {}).get("summary") or record["name"]
    title = title[:200]  # Notion title property practical limit

    properties = {
        # Assumes the database's title property exists under some name;
        # Notion requires referencing it by its actual property name,
        # which we don't know in advance -- "Name" is Notion's default
        # for a fresh database's title column.
        "Name": {"title": [{"type": "text", "text": {"content": title}}]},
    }
    if record.get("created_at"):
        properties["Date"] = {"date": {"start": record["created_at"][:10]}}
    properties.update(_insight_properties(record))

    for speaker_id, speaker_name in (record.get("speaker_names") or {}).items():
        idx = speaker_slot_index(speaker_id)
        if idx:
            properties[f"Speaker {idx}"] = {"rich_text": [{"type": "text", "text": {"content": speaker_name[:MAX_TEXT_LEN]}}]}

    payload = {
        "parent": {"database_id": database_id, "type": "database_id"},
        "properties": properties,
        "children": _build_blocks(record),
    }

    resp = requests.post(f"{API_BASE}/pages", headers=_headers(), json=payload, timeout=15)
    if not resp.ok:
        raise RuntimeError(f"Notion API error {resp.status_code}: {resp.text[:300]}")
    page_id = resp.json()["id"]
    log.info("pushed %s to Notion", record["name"])

    try:
        import rag_index
        index_text = f"{title}\n\n{_page_plain_text(page_id)}"
        rag_index.index_text("notion", page_id, index_text, date=(record.get("created_at") or "")[:10] or None)
    except Exception as e:
        log.warning("rag_index indexing failed for Notion page %s (non-fatal): %s", page_id, e)

    return page_id


def push_command(record: dict, jarvis_result: dict) -> str:
    """Creates a new page in the dedicated Jarvis database (see
    notion_setup.create_jarvis_database) for one voice command -- Jarvis
    commands previously had no Notion presence at all, only an inline card
    on the main dashboard. Mirrors push_recording()'s shape (title + Date
    property + a simple body) but against its own database, since a
    command isn't a recording in the Notes sense (no speakers/summary/
    action items) and doesn't belong mixed into that database.
    Best-effort: caller (poller.py) should treat a failure here as
    non-fatal, same as every other optional sync destination."""
    database_id = settings.get_all().get("notion_jarvis_database_id")
    if not database_id:
        raise RuntimeError("Jarvis Notion database isn't configured — set it up in Settings -> Jarvis")

    action_type = jarvis_result.get("action_type") or "unknown"
    transcript = jarvis_result.get("transcript") or ""
    spoken = jarvis_result.get("spoken") or ""
    title = f"{action_type} — {transcript[:150]}" if transcript else action_type

    properties = {
        "Name": {"title": [{"type": "text", "text": {"content": title[:200]}}]},
        "Action Type": {"select": {"name": action_type}},
        "Heard": {"rich_text": [{"type": "text", "text": {"content": transcript[:MAX_TEXT_LEN]}}]},
        "Replied": {"rich_text": [{"type": "text", "text": {"content": spoken[:MAX_TEXT_LEN]}}]},
        "OK": {"checkbox": bool(jarvis_result.get("ok"))},
        "Done": {"checkbox": jarvis_result.get("user_status") == "done"},
    }
    if record.get("created_at"):
        properties["Date"] = {"date": {"start": record["created_at"][:10]}}

    payload = {
        "parent": {"database_id": database_id, "type": "database_id"},
        "properties": properties,
    }
    resp = requests.post(f"{API_BASE}/pages", headers=_headers(), json=payload, timeout=15)
    if not resp.ok:
        raise RuntimeError(f"Notion API error {resp.status_code}: {resp.text[:300]}")
    log.info("pushed Jarvis command %s to Notion", record["name"])
    return resp.json()["id"]


def get_speaker_slot_values(note_page_id: str) -> dict:
    """Reads the Notes page's "Speaker 1".."Speaker 6" properties. Returns
    {slot_index: text} for whichever slots are non-empty -- used by
    poller.py to detect a rename made directly in Notion."""
    resp = requests.get(f"{API_BASE}/pages/{note_page_id}", headers=_headers(), timeout=15)
    if not resp.ok:
        raise RuntimeError(f"Notion API error {resp.status_code}: {resp.text[:300]}")
    props = resp.json()["properties"]
    values = {}
    for i in range(1, SPEAKER_SLOT_COUNT + 1):
        prop = props.get(f"Speaker {i}")
        if prop and prop.get("type") == "rich_text":
            text = "".join(t.get("plain_text", "") for t in prop["rich_text"]).strip()
            if text:
                values[i] = text
    return values


def set_speaker_slot_values(note_page_id: str, slot_values: dict):
    """slot_values: {slot_index: text}. Used to push a name the dashboard
    already knows (auto-guessed, or renamed there) into Notion, so the
    two stay in sync regardless of which side the user edits."""
    properties = {
        f"Speaker {idx}": {"rich_text": [{"type": "text", "text": {"content": text[:MAX_TEXT_LEN]}}]}
        for idx, text in slot_values.items()
    }
    resp = requests.patch(f"{API_BASE}/pages/{note_page_id}", headers=_headers(), json={"properties": properties}, timeout=15)
    if not resp.ok:
        raise RuntimeError(f"Notion API error {resp.status_code}: {resp.text[:300]}")


def _list_block_children(block_id: str) -> list:
    children = []
    cursor = None
    while True:
        params = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        resp = requests.get(f"{API_BASE}/blocks/{block_id}/children", headers=_headers(), params=params, timeout=15)
        if not resp.ok:
            raise RuntimeError(f"Notion API error {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        children.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return children


def _block_plain_text(block: dict) -> str:
    rich_text = block.get(block.get("type", ""), {}).get("rich_text", [])
    return "".join(t.get("plain_text", "") for t in rich_text)


def update_transcript_blocks(note_page_id: str, record: dict):
    """Called when a speaker gets renamed (manually via the dashboard, or
    the auto-guess corrects itself on reprocessing) for a recording that's
    already been pushed to Notion -- push_recording() only writes the
    transcript once at creation time, so without this, a rename in the
    dashboard would never show up on an already-existing Notion page.
    Finds the existing "Transcript" heading_2 block and everything after
    it (the old transcript paragraphs), archives them, and re-appends a
    freshly rendered transcript using the current speaker_names. No-op if
    the page has no "Transcript" heading (shouldn't happen for anything
    push_recording created, but defensive against a hand-edited page)."""
    children = _list_block_children(note_page_id)
    transcript_heading_idx = next(
        (i for i, b in enumerate(children)
         if b.get("type") == "heading_2" and _block_plain_text(b).strip() == "Transcript"),
        None,
    )
    if transcript_heading_idx is None:
        return

    for block in children[transcript_heading_idx:]:
        resp = requests.delete(f"{API_BASE}/blocks/{block['id']}", headers=_headers(), timeout=15)
        if not resp.ok:
            raise RuntimeError(f"Notion API error {resp.status_code}: {resp.text[:300]}")

    segments = record.get("segments")
    if segments:
        speaker_names = record.get("speaker_names") or {}

        def _label(seg):
            sid = seg.get("speaker_id", "?")
            return speaker_names.get(sid) or f"Speaker {sid}"

        transcript_text = "\n".join(
            f"{_label(seg)}: {seg.get('text', '').strip()}" for seg in merge_consecutive_segments(segments)
        )
    else:
        transcript_text = record.get("transcript") or ""

    new_blocks = [_heading("Transcript")] + _text_block("paragraph", transcript_text)
    resp = requests.patch(
        f"{API_BASE}/blocks/{note_page_id}/children", headers=_headers(),
        json={"children": new_blocks}, timeout=15,
    )
    if not resp.ok:
        raise RuntimeError(f"Notion API error {resp.status_code}: {resp.text[:300]}")
    log.info("updated transcript speaker names on Notion page %s", note_page_id)


def update_all_blocks(note_page_id: str, record: dict):
    """Like update_transcript_blocks(), but replaces the entire page body
    (Summary, Action items, Stakeholders, everything -- see _build_blocks)
    instead of just the Transcript section. Used after a speaker rename
    triggers re-summarization (poller.resync_after_rename) -- the LLM's own
    prose in Summary/Stakeholders was frozen at whatever label existed at
    original processing time, so only refreshing the Transcript block
    would leave those still referencing the old name. Archives every
    existing child block, not just from a heading onward, since a fresh
    summarize() pass can change the presence/order of sections entirely
    (e.g. action_items going from empty to non-empty)."""
    children = _list_block_children(note_page_id)
    for block in children:
        resp = requests.delete(f"{API_BASE}/blocks/{block['id']}", headers=_headers(), timeout=15)
        if not resp.ok:
            raise RuntimeError(f"Notion API error {resp.status_code}: {resp.text[:300]}")

    new_blocks = _build_blocks(record)
    # Same 100-block append cap as push_recording's initial creation --
    # _build_blocks() already handles truncation internally, but the
    # append endpoint itself is also capped at 100 per call.
    for i in range(0, len(new_blocks), 100):
        resp = requests.patch(
            f"{API_BASE}/blocks/{note_page_id}/children", headers=_headers(),
            json={"children": new_blocks[i:i + 100]}, timeout=15,
        )
        if not resp.ok:
            raise RuntimeError(f"Notion API error {resp.status_code}: {resp.text[:300]}")
    log.info("refreshed full page content on Notion page %s", note_page_id)


def list_workspace_people() -> list:
    """GET /v1/users, paginated. Requires the integration to have "Read
    user information" capability enabled (notion.so/my-integrations ->
    your integration -> Capabilities) -- without it this 403s. Returns
    [{"id": ..., "name": ...}, ...] for type=="person" users only (skips
    bot/integration accounts, which can't be assigned tasks)."""
    people = []
    cursor = None
    while True:
        params = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        resp = requests.get(f"{API_BASE}/users", headers=_headers(), params=params, timeout=15)
        if not resp.ok:
            raise RuntimeError(f"Notion API error {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        for u in data.get("results", []):
            if u.get("type") == "person" and u.get("name"):
                people.append({"id": u["id"], "name": u["name"]})
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return people


_people_lookup_broken = False  # set once list_workspace_people() 403s, so we stop retrying it every push


def _safe_list_workspace_people() -> list:
    """Wraps list_workspace_people() so a token without the "Read user
    information" capability degrades to "no Assignee matches" instead of
    failing the whole push. Logs the actionable fix once, not on every
    poll cycle."""
    global _people_lookup_broken
    if _people_lookup_broken:
        return []
    try:
        return list_workspace_people()
    except Exception as e:
        _people_lookup_broken = True
        log.warning(
            "Notion people lookup failed (%s) -- Task Assignee will be left blank until this is fixed. "
            "Enable \"Read user information\" for your integration at "
            "https://www.notion.so/my-integrations -> your integration -> Capabilities, then restart the pipeline.",
            e,
        )
        return []


def _match_person_exact(name: str, people: list):
    """Exact, case-insensitive match only -- deliberately NOT a substring
    match. Assignee is a real `people`-type property that emails whoever
    it's set to; a loose match (e.g. "Jeremy" fuzzy-matching a coworker
    named "Jeremy Adams" who has nothing to do with this recording) would
    notify the wrong person. Returns None rather than guess."""
    if not name:
        return None
    needle = name.strip().lower()
    for p in people:
        if p["name"].strip().lower() == needle:
            return p
    return None


_data_source_cache = {}  # database_id -> data_source_id, resolved once per process


def _data_source_id(database_id: str) -> str:
    """Notion's current API models each database as a container of one or
    more "data sources" -- querying/patching properties happens against the
    data source, not the database, even though page creation still targets
    the database_id directly. Cached since it won't change at runtime."""
    if database_id not in _data_source_cache:
        resp = requests.get(f"{API_BASE}/databases/{database_id}", headers=_headers(), timeout=15)
        if not resp.ok:
            raise RuntimeError(f"Notion API error {resp.status_code}: {resp.text[:300]}")
        _data_source_cache[database_id] = resp.json()["data_sources"][0]["id"]
    return _data_source_cache[database_id]


_schema_cache = {}  # database_id -> {prop_name: prop_type}, resolved once per process


def _schema_properties(database_id: str) -> dict:
    """{property_name: property_type} for a database's data source --
    used by push_journal() to adapt to whatever schema the user's own
    Notion Journal template happens to have, since (unlike Tasks/People/
    Calendar) we didn't create that database ourselves and can't assume
    property names."""
    if database_id not in _schema_cache:
        ds_id = _data_source_id(database_id)
        resp = requests.get(f"{API_BASE}/data_sources/{ds_id}", headers=_headers(), timeout=15)
        if not resp.ok:
            raise RuntimeError(f"Notion API error {resp.status_code}: {resp.text[:300]}")
        _schema_cache[database_id] = {name: p["type"] for name, p in resp.json()["properties"].items()}
    return _schema_cache[database_id]


def _find_person_page(name: str, database_id: str):
    """Case-insensitive title match against the People database. Notion's
    query filter is exact-match only (no case-insensitive op), so this
    fetches all pages and compares client-side -- fine at the scale of a
    personal voice-memo tool's contact list."""
    ds_id = _data_source_id(database_id)
    cursor = None
    needle = name.strip().lower()
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        resp = requests.post(f"{API_BASE}/data_sources/{ds_id}/query", headers=_headers(), json=body, timeout=15)
        if not resp.ok:
            raise RuntimeError(f"Notion API error {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        for page in data.get("results", []):
            title_prop = page["properties"].get("Name", {}).get("title", [])
            page_title = "".join(t.get("plain_text", "") for t in title_prop).strip()
            if page_title.lower() == needle:
                return page
        if not data.get("has_more"):
            return None
        cursor = data.get("next_cursor")


def _relation_ids(page: dict, prop_name: str) -> list:
    return [r["id"] for r in page.get("properties", {}).get(prop_name, {}).get("relation", [])]


def _find_person_by_email(email: str, database_id: str):
    """Same pagination as _find_person_page but matches the Email property
    -- preferred over name matching when we have a calendar attendee's
    address, since names alone drift across recordings ("Sanchit" vs
    "Sanchit Gupta") while an email address doesn't."""
    ds_id = _data_source_id(database_id)
    cursor = None
    needle = email.strip().lower()
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        resp = requests.post(f"{API_BASE}/data_sources/{ds_id}/query", headers=_headers(), json=body, timeout=15)
        if not resp.ok:
            raise RuntimeError(f"Notion API error {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        for page in data.get("results", []):
            page_email = (page["properties"].get("Email", {}).get("email") or "").strip().lower()
            if page_email and page_email == needle:
                return page
        if not data.get("has_more"):
            return None
        cursor = data.get("next_cursor")


def _page_plain_text(page_id: str) -> str:
    """Concatenates a page's top-level block content into plain text --
    used by query_pages_mentioning below since Notes/Journal pages have no
    participant/person relation property to filter on (see push_recording's
    docstring), only free text in the page body. One level deep only (no
    recursion into nested blocks/toggles) -- Notes/Journal pages built by
    this project are flat (headings + paragraphs), not nested."""
    parts = []
    cursor = None
    while True:
        params = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        resp = requests.get(f"{API_BASE}/blocks/{page_id}/children", headers=_headers(), params=params, timeout=15)
        if not resp.ok:
            return ""
        data = resp.json()
        for block in data.get("results", []):
            block_type = block.get("type")
            rich_text = (block.get(block_type) or {}).get("rich_text", [])
            parts.append("".join(t.get("plain_text", "") for t in rich_text))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return "\n".join(parts)


def query_pages_mentioning(person_name: str, start: str = None, end: str = None) -> list:
    """New cross-page retrieval for jarvis.py's find_person_context -- NOT
    a rename of an existing lookup. Notes/Journal pages have no participant/
    person relation property (see push_recording's docstring: that was a
    deliberate removal), so this can't filter server-side by "who's
    mentioned" the way a People-database relation could. Instead: queries
    each configured database (Notes via notion_database_id, Journal via
    notion_journal_database_id) with a server-side Date-range filter when
    given, then does a client-side case-insensitive substring match of
    person_name against each candidate page's title + Speaker N properties
    + full block-content text (see _page_plain_text). Returns
    [{"source": "notion", "date": "YYYY-MM-DD", "text": "..."}, ...]."""
    saved = settings.get_all()
    needle = person_name.strip().lower()
    results = []

    for db_key in ("notion_database_id", "notion_journal_database_id"):
        database_id = saved.get(db_key)
        if not database_id:
            continue
        try:
            ds_id = _data_source_id(database_id)
        except Exception as e:
            log.debug("query_pages_mentioning: could not resolve data source for %s: %s", db_key, e)
            continue

        date_filter = None
        if start or end:
            conditions = []
            if start:
                conditions.append({"property": "Date", "date": {"on_or_after": start}})
            if end:
                conditions.append({"property": "Date", "date": {"on_or_before": end}})
            date_filter = {"and": conditions} if len(conditions) > 1 else conditions[0]

        cursor = None
        while True:
            body = {"page_size": 100}
            if date_filter:
                body["filter"] = date_filter
            if cursor:
                body["start_cursor"] = cursor
            try:
                resp = requests.post(f"{API_BASE}/data_sources/{ds_id}/query", headers=_headers(), json=body, timeout=15)
            except requests.RequestException as e:
                log.debug("query_pages_mentioning: query failed for %s: %s", db_key, e)
                break
            if not resp.ok:
                # A database without a "Date" property (e.g. a user's own
                # Journal template, see push_journal's schema-adaptive
                # handling) would 400 on the filter above -- fall back to
                # an unfiltered query rather than silently returning nothing.
                if date_filter:
                    body.pop("filter", None)
                    resp = requests.post(f"{API_BASE}/data_sources/{ds_id}/query", headers=_headers(), json=body, timeout=15)
                if not resp.ok:
                    break
            data = resp.json()
            for page in data.get("results", []):
                props = page.get("properties", {})
                title_prop = props.get("Name", {}).get("title", [])
                title = "".join(t.get("plain_text", "") for t in title_prop).strip()
                speaker_values = [
                    "".join(t.get("plain_text", "") for t in v.get("rich_text", []))
                    for k, v in props.items() if k.startswith("Speaker ")
                ]
                date = (props.get("Date", {}).get("date") or {}).get("start") or ""
                haystack = (title + " " + " ".join(speaker_values)).lower()
                text = ""
                matched = needle in haystack
                if not matched:
                    text = _page_plain_text(page["id"])
                    matched = needle in text.lower()
                if matched:
                    results.append({"source": "notion", "date": date[:10] if date else None, "text": text or title})
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")

    return results


def _find_all_person_pages_by_name(name: str, database_id: str) -> list:
    """Like _find_person_page, but returns every matching page instead of
    just the first -- needed to tell "exactly one existing person shares
    this name" apart from "several people share this name". Both cases
    (per explicit product decision) require user confirmation before
    linking a Task/Calendar entry to any of them -- a same name is never
    enough on its own, only a real email match is confident enough to
    skip that (two different people can easily share a first name; an
    address can't)."""
    ds_id = _data_source_id(database_id)
    cursor = None
    needle = name.strip().lower()
    matches = []
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        resp = requests.post(f"{API_BASE}/data_sources/{ds_id}/query", headers=_headers(), json=body, timeout=15)
        if not resp.ok:
            raise RuntimeError(f"Notion API error {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        for page in data.get("results", []):
            title_prop = page["properties"].get("Name", {}).get("title", [])
            page_title = "".join(t.get("plain_text", "") for t in title_prop).strip()
            if page_title.lower() == needle:
                matches.append(page)
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return matches


def _page_summary(page: dict) -> dict:
    """{"id","name","note","email"} for a People page -- the shape shown
    to the user on the dashboard's pending-confirmation queue, since
    "Vijay" alone isn't enough to tell two candidates apart; their Note
    (role/context) and Email (if any) usually are."""
    title_prop = page["properties"].get("Name", {}).get("title", [])
    note_prop = page["properties"].get("Note", {}).get("rich_text", [])
    return {
        "id": page["id"],
        "name": "".join(t.get("plain_text", "") for t in title_prop).strip(),
        "note": "".join(t.get("plain_text", "") for t in note_prop).strip(),
        "email": page["properties"].get("Email", {}).get("email") or "",
    }


def _create_minimal_person_page(name: str, email: str, database_id: str, note_page_id: str = None) -> dict:
    properties = {"Name": {"title": [{"type": "text", "text": {"content": name[:200]}}]}}
    if email:
        properties["Email"] = {"email": email}
    if note_page_id:
        properties["Related Note"] = {"relation": [{"id": note_page_id}]}
    resp = requests.post(
        f"{API_BASE}/pages", headers=_headers(),
        json={"parent": {"database_id": database_id, "type": "database_id"}, "properties": properties},
        timeout=15,
    )
    if not resp.ok and email:
        # Older workspace without the Email property -- retry without it.
        del properties["Email"]
        resp = requests.post(
            f"{API_BASE}/pages", headers=_headers(),
            json={"parent": {"database_id": database_id, "type": "database_id"}, "properties": properties},
            timeout=15,
        )
    if not resp.ok:
        raise RuntimeError(f"Notion API error {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def resolve_person_for_relation(name: str, email: str, people_database_id: str, note_page_id: str = None,
                                 linkedin: str = None):
    """Resolves a name (optionally with a known email, e.g. a calendar
    attendee, or a manually-entered LinkedIn URL) to a People page id for a
    Task/Calendar entry's "Related Person" relation.

    Returns (person_page_id_or_None, candidates_or_None):
    - Confident: email or LinkedIn known (found or newly created), or
      genuinely no existing page shares this name at all -> (page_id,
      None), nothing to confirm. Contact info wins even over a name
      mismatch -- it's a stronger identity signal than spelling (see
      notion_setup.py's LinkedIn property comment).
    - Ambiguous: no email/LinkedIn, and 1+ existing pages already share
      this name -> (None, [_page_summary(p), ...]). Caller must register a
      pending confirmation (storage.add_pending_person_link) instead of
      setting the relation now -- even a single same-name match isn't
      auto-linked by itself, since it could genuinely be a different
      person.
    """
    if not name:
        return None, None
    if linkedin:
        page = _find_person_by_linkedin(linkedin, people_database_id)
        if page:
            return page["id"], None
    if email:
        page = _find_person_by_email(email, people_database_id)
        if page is None:
            page = _create_minimal_person_page(name, email, people_database_id, note_page_id)
        return page["id"], None

    candidates = _find_all_person_pages_by_name(name, people_database_id)
    if not candidates:
        page = _create_minimal_person_page(name, None, people_database_id, note_page_id)
        return page["id"], None
    return None, [_page_summary(p) for p in candidates]


def get_person_note(email: str, name: str, database_id: str) -> str:
    """Fetches a person's "Note" field from the People database (their role
    or relationship, e.g. "PM on the Atlas project") -- used by
    poller._past_context_for_attendees to enrich pre-meeting prep notes
    beyond just a bare name. Email match preferred (see
    _find_person_by_email), falls back to name. Returns "" on any failure
    (missing database, person not found, API hiccup) -- this is a nice-to-
    have enrichment, never something that should break the prep note."""
    if not database_id:
        return ""
    try:
        page = _find_person_by_email(email, database_id) if email else None
        if page is None and name:
            page = _find_person_page(name, database_id)
        if page is None:
            return ""
        note_prop = page["properties"].get("Note", {}).get("rich_text", [])
        return "".join(t.get("plain_text", "") for t in note_prop).strip()
    except RuntimeError:
        return ""


def _attendee_email_for_name(name: str, meeting: dict) -> str:
    """Matches a stakeholder name against the calendar attendee list
    (case-insensitive, either full display name or the local part before
    '@') -- returns '' if no meeting or no match."""
    if not meeting or not name:
        return ""
    needle = name.strip().lower()
    for attendee in meeting.get("attendees") or []:
        attendee_name = (attendee.get("name") or "").strip().lower()
        if attendee_name and (attendee_name == needle or needle in attendee_name or attendee_name in needle):
            return attendee.get("email", "")
    return ""


def _person_new_context(record: dict, stakeholder: dict) -> str:
    """Assembles this recording's facts about one person for the
    knowledge-merge prompt (see providers/base.py's
    build_person_knowledge_prompt): their stakeholder note, the recording
    summary, any action items naming them, and the recording date."""
    name = (stakeholder.get("name") or "").strip()
    summary = record.get("summary") or {}
    lines = [f"Recording: {record['name']} on {record.get('created_at', 'unknown date')}"]
    if stakeholder.get("note"):
        lines.append(f"Their role in this conversation: {stakeholder['note']}")
    if summary.get("summary"):
        lines.append(f"Conversation summary: {summary['summary']}")
    involving = [
        it.get("text", "") for it in (summary.get("action_items") or [])
        if name and name.lower() in ((it.get("owner") or "") + " " + (it.get("comm_recipient") or "")).lower()
    ]
    if involving:
        lines.append("Action items involving them: " + "; ".join(involving))
    return "\n".join(lines)


def _update_person_knowledge(page_id: str, person_name: str, record: dict, stakeholder: dict, note_page_id: str):
    """Grows a person's Notion page into a knowledge base on every mention:

    - "Knowledge" section: one LLM-maintained paragraph, updated in place
      by MERGING this recording's new facts into the existing text (see
      PERSON_KNOWLEDGE_INSTRUCTIONS -- prior facts are preserved, not
      replaced), using the same surgical section-replace pattern as
      update_transcript_blocks.
    - "History" section: append-only dated log, one bullet per mention
      with an inline link to that conversation's Notes page -- multiple
      conversations with the same person across days/topics each get
      their own entry, never modifying earlier ones.
    """
    from providers import get_completer
    from providers.base import build_person_knowledge_prompt

    children = _list_block_children(page_id)
    knowledge_heading = None
    history_heading = None
    knowledge_paras = []
    for block in children:
        if block.get("type") == "heading_2":
            text = _block_plain_text(block).strip().lower()
            if text == "knowledge":
                knowledge_heading = block
            elif text == "history":
                history_heading = block
        elif knowledge_heading and not history_heading and block.get("type") == "paragraph":
            knowledge_paras.append(block)
        elif block.get("type") == "bulleted_list_item" and f"— {record['name']}:" in _block_plain_text(block):
            # This recording already has a History entry here -- push_people
            # is being retried (e.g. a later stakeholder failed last cycle).
            # Skip entirely rather than double-merging the same facts into
            # Knowledge and double-logging the mention.
            return

    existing_text = "\n".join(_block_plain_text(b) for b in knowledge_paras).strip()
    _, complete = get_completer()
    new_knowledge = complete(build_person_knowledge_prompt(
        person_name, existing_text, _person_new_context(record, stakeholder)))

    if knowledge_heading:
        for block in knowledge_paras:
            resp = requests.delete(f"{API_BASE}/blocks/{block['id']}", headers=_headers(), timeout=15)
            if not resp.ok:
                raise RuntimeError(f"Notion API error {resp.status_code}: {resp.text[:300]}")
        resp = requests.patch(
            f"{API_BASE}/blocks/{page_id}/children", headers=_headers(),
            json={"children": _text_block("paragraph", new_knowledge), "after": knowledge_heading["id"]},
            timeout=15,
        )
        if not resp.ok:
            raise RuntimeError(f"Notion API error {resp.status_code}: {resp.text[:300]}")
    else:
        append_blocks(page_id, [_heading("Knowledge")] + _text_block("paragraph", new_knowledge))

    if not history_heading:
        append_blocks(page_id, [_heading("History")])

    # One dated bullet per mention, with an inline mention-link to the
    # conversation's Notes page. Appended at the page end, which is always
    # inside the History section since History is the last section.
    when = (record.get("created_at") or "")[:16].replace("T", " ")
    entry_text = f"🗓 {when} — {record['name']}: {(stakeholder.get('note') or (record.get('summary') or {}).get('summary') or '')[:300]} — "
    append_blocks(page_id, [{
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": [
            {"type": "text", "text": {"content": entry_text}},
            {"type": "mention", "mention": {"type": "page", "page": {"id": note_page_id}}},
        ]},
    }])


def push_people(record: dict, note_page_id: str):
    """Find-or-create a Notion page per mentioned person (stakeholders,
    which already includes the speaker themself -- see poller.py's
    _add_speakers_as_stakeholders) in the People database, and relate each
    to this recording's Notes page. Appends to each person's existing
    relations rather than overwriting, so someone mentioned across
    multiple recordings ends up linked to all of them. No-ops if there are
    no stakeholders or the People database isn't configured (so this is an
    optional layer, not a hard requirement).

    When this is a meeting recording (record["meeting"] set, see
    google_client.current_or_next_event), each stakeholder's email is
    resolved from the calendar attendee list and used as the primary
    find-or-create key -- falls back to name matching when there's no
    meeting, or the name doesn't match any attendee. This is what makes
    "every recording with alice@co.com" a real Notion filter rather than
    depending on her name being spelled identically every time."""
    stakeholders = (record.get("summary") or {}).get("stakeholders") or []
    if not stakeholders:
        return
    database_id = settings.get_all().get("notion_people_database_id")
    if not database_id:
        return
    meeting = record.get("meeting")

    for s in stakeholders:
        name = (s.get("name") or "").strip()
        if not name:
            continue
        email = _attendee_email_for_name(name, meeting)

        existing = None
        if email:
            try:
                existing = _find_person_by_email(email, database_id)
            except RuntimeError:
                pass  # Email property may not exist on an older workspace -- fall through to name match
        if existing is None:
            existing = _find_person_page(name, database_id)

        if existing:
            page_id = existing["id"]
            related = set(_relation_ids(existing, "Related Note"))
            properties = {}
            if note_page_id not in related:
                related.add(note_page_id)
                properties["Related Note"] = {"relation": [{"id": pid} for pid in related]}
            # Backfill Email on a page that was originally name-matched
            # before we had calendar context for this person.
            existing_email = (existing["properties"].get("Email", {}).get("email") or "")
            if email and not existing_email:
                properties["Email"] = {"email": email}
            if properties:
                resp = requests.patch(f"{API_BASE}/pages/{page_id}", headers=_headers(), json={"properties": properties}, timeout=15)
                if not resp.ok and "Email" in properties:
                    # Older workspace without the Email property -- retry without it
                    # rather than failing the whole push over a cosmetic field.
                    del properties["Email"]
                    resp = requests.patch(f"{API_BASE}/pages/{page_id}", headers=_headers(), json={"properties": properties}, timeout=15) if properties else resp
                if not resp.ok:
                    raise RuntimeError(f"Notion API error {resp.status_code}: {resp.text[:300]}")
        else:
            properties = {
                "Name": {"title": [{"type": "text", "text": {"content": name[:200]}}]},
                "Related Note": {"relation": [{"id": note_page_id}]},
            }
            if s.get("note"):
                properties["Note"] = {"rich_text": [{"type": "text", "text": {"content": s["note"][:MAX_TEXT_LEN]}}]}
            if email:
                properties["Email"] = {"email": email}
            resp = requests.post(
                f"{API_BASE}/pages", headers=_headers(),
                json={"parent": {"database_id": database_id, "type": "database_id"}, "properties": properties},
                timeout=15,
            )
            if not resp.ok and email:
                # Older workspace without the Email property -- retry without it.
                del properties["Email"]
                resp = requests.post(
                    f"{API_BASE}/pages", headers=_headers(),
                    json={"parent": {"database_id": database_id, "type": "database_id"}, "properties": properties},
                    timeout=15,
                )
            if not resp.ok:
                raise RuntimeError(f"Notion API error {resp.status_code}: {resp.text[:300]}")
            page_id = resp.json()["id"]

        # Grow this person's page body (Knowledge merge + History log) on
        # every mention. Best-effort: an LLM/Notion hiccup here must not
        # fail the whole push_people (whose success gates
        # mark_distributed("notion_people") in poller.distribute_once) --
        # the properties/relations above are the load-bearing part.
        try:
            _update_person_knowledge(page_id, name, record, s, note_page_id)
        except Exception as e:
            log.warning("failed to update knowledge for %r on People page %s: %s", name, page_id, e)

    log.info("pushed %d stakeholder(s) from %s to Notion People", len(stakeholders), record["name"])


def backfill_person_email(name: str, email: str):
    """Writes a user-supplied address (typed into a Task page's "Send To"
    property, see poller.check_notion_email_approvals_once) onto that
    person's People page Email property, if the page exists and doesn't
    already have one -- People stays the long-term address book, so every
    future email to this person auto-resolves without retyping."""
    people_database_id = settings.get_all().get("notion_people_database_id")
    if not (name and email and people_database_id):
        return
    page = _find_person_page(name, people_database_id)
    if not page:
        return
    if (page["properties"].get("Email", {}).get("email") or "").strip():
        return  # already has an address -- don't overwrite
    resp = requests.patch(
        f"{API_BASE}/pages/{page['id']}", headers=_headers(),
        json={"properties": {"Email": {"email": email}}}, timeout=15,
    )
    if not resp.ok:
        log.warning("failed to backfill email for %r: %s %s", name, resp.status_code, resp.text[:300])


_people_linkedin_ensured = set()


def ensure_people_linkedin_property(people_database_id: str):
    """Idempotently adds the "LinkedIn" (url) property to an existing
    People database that predates this feature -- same gate-and-PATCH
    shape as ensure_email_draft_properties(). New workspaces get it from
    notion_setup.create_workspace() directly; this covers upgrades."""
    if not people_database_id or people_database_id in _people_linkedin_ensured:
        return
    ds_id = _data_source_id(people_database_id)
    resp = requests.patch(
        f"{API_BASE}/data_sources/{ds_id}", headers=_headers(),
        json={"properties": {"LinkedIn": {"url": {}}}},
        timeout=15,
    )
    if not resp.ok:
        log.warning("failed to add LinkedIn property to People database %s: %s %s",
                    people_database_id, resp.status_code, resp.text[:300])
        return
    _people_linkedin_ensured.add(people_database_id)


def _find_person_by_linkedin(linkedin_url: str, database_id: str):
    """Same pagination/matching shape as _find_person_by_email -- a
    LinkedIn URL is as strong an identity signal as an email address (a
    real profile can't belong to two different people), so it gets the
    same priority treatment in resolve_person_for_relation."""
    if not linkedin_url:
        return None
    ds_id = _data_source_id(database_id)
    cursor = None
    needle = linkedin_url.strip().rstrip("/").lower()
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        resp = requests.post(f"{API_BASE}/data_sources/{ds_id}/query", headers=_headers(), json=body, timeout=15)
        if not resp.ok:
            raise RuntimeError(f"Notion API error {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        for page in data.get("results", []):
            page_linkedin = (page["properties"].get("LinkedIn", {}).get("url") or "").strip().rstrip("/").lower()
            if page_linkedin and page_linkedin == needle:
                return page
        if not data.get("has_more"):
            return None
        cursor = data.get("next_cursor")


def update_person_contact(page_id: str, email: str = None, linkedin: str = None):
    """Manual entry from the dashboard (see app.py's POST /people/{page_id}/
    contact) for a speaker/stakeholder whose email/LinkedIn Google Meet
    didn't supply -- e.g. a transcript-only name with no calendar event.
    Only writes the fields actually provided; leaves the other untouched."""
    properties = {}
    if email is not None:
        properties["Email"] = {"email": email.strip() or None}
    if linkedin is not None:
        properties["LinkedIn"] = {"url": linkedin.strip() or None}
    if not properties:
        return
    resp = requests.patch(f"{API_BASE}/pages/{page_id}", headers=_headers(), json={"properties": properties}, timeout=15)
    if not resp.ok:
        raise RuntimeError(f"Notion API error {resp.status_code}: {resp.text[:300]}")


def set_person_contact_by_name(name: str, people_database_id: str, email: str = None, linkedin: str = None):
    """Dashboard entry point for manually adding a stakeholder's contact
    info (see app.py's POST /people/contact) -- looks the person up by
    name (creating a minimal page if none exists yet, same as
    resolve_person_for_relation's no-candidates path) and writes whichever
    of email/linkedin was actually supplied. Name-based lookup here is
    fine even though it's not the identity-linking mechanism itself --
    this is the point where a human confirms which named person these
    contact details belong to, not an automated guess."""
    if not name or not people_database_id:
        return
    ensure_people_linkedin_property(people_database_id)
    page = _find_person_page(name, people_database_id)
    if page is None:
        page = _create_minimal_person_page(name, email, people_database_id)
        if linkedin:
            update_person_contact(page["id"], linkedin=linkedin)
        return
    update_person_contact(page["id"], email=email, linkedin=linkedin)


def find_duplicate_people(people_database_id: str) -> list:
    """Scans the whole People database for pages that share a non-empty
    Email or LinkedIn value despite having different names -- the
    situation manual contact entry (update_person_contact) or a later
    calendar match can surface (e.g. "Vijay" and "Vijay Kumar" turn out to
    be the same address). Returns [{"key": email_or_linkedin, "pages":
    [_page_summary, ...]}] for the dashboard to show as a "these look like
    the same person" suggestion -- merging itself is a separate, explicit
    user action (see merge_person_pages) since moving page content/
    relations unattended is too destructive to do silently."""
    ds_id = _data_source_id(people_database_id)
    cursor = None
    by_email, by_linkedin = {}, {}
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        resp = requests.post(f"{API_BASE}/data_sources/{ds_id}/query", headers=_headers(), json=body, timeout=15)
        if not resp.ok:
            raise RuntimeError(f"Notion API error {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        for page in data.get("results", []):
            email = (page["properties"].get("Email", {}).get("email") or "").strip().lower()
            linkedin = (page["properties"].get("LinkedIn", {}).get("url") or "").strip().rstrip("/").lower()
            if email:
                by_email.setdefault(email, []).append(page)
            if linkedin:
                by_linkedin.setdefault(linkedin, []).append(page)
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")

    groups = []
    seen_page_id_sets = set()
    for key, pages in list(by_email.items()) + list(by_linkedin.items()):
        if len(pages) < 2:
            continue
        ids = tuple(sorted(p["id"] for p in pages))
        if ids in seen_page_id_sets:
            continue
        seen_page_id_sets.add(ids)
        groups.append({"key": key, "pages": [_page_summary(p) for p in pages]})
    return groups


def merge_person_pages(keeper_id: str, loser_id: str):
    """Explicit, user-triggered merge (dashboard "Merge" button, see
    find_duplicate_people) -- never automatic. Appends the loser's Note
    content onto the keeper as a labeled block, re-points any Task/
    Calendar "Related Person" relation from loser to keeper, then archives
    the loser page. Notion's relation properties don't support a server-
    side "find everything relating to X" query across databases directly,
    so this re-points via each configured Tasks/Calendar database's own
    query filtered on Related Person = loser_id."""
    keeper = get_page(keeper_id)
    loser = get_page(loser_id)
    loser_note = "".join(
        t.get("plain_text", "") for t in loser["properties"].get("Note", {}).get("rich_text", [])
    ).strip()
    if loser_note:
        append_blocks(keeper_id, _text_block("paragraph", f"(merged from a duplicate page) {loser_note}"))

    saved = settings.get_all()
    for database_id in (saved.get("notion_tasks_database_id"), saved.get("notion_events_database_id")):
        if not database_id:
            continue
        ds_id = _data_source_id(database_id)
        resp = requests.post(
            f"{API_BASE}/data_sources/{ds_id}/query", headers=_headers(),
            json={"filter": {"property": "Related Person", "relation": {"contains": loser_id}}},
            timeout=15,
        )
        if not resp.ok:
            log.warning("merge: failed to query %s for loser relations: %s %s", database_id, resp.status_code, resp.text[:300])
            continue
        for page in resp.json().get("results", []):
            existing = [r["id"] for r in page["properties"].get("Related Person", {}).get("relation", []) if r["id"] != loser_id]
            if keeper_id not in existing:
                existing.append(keeper_id)
            requests.patch(
                f"{API_BASE}/pages/{page['id']}", headers=_headers(),
                json={"properties": {"Related Person": {"relation": [{"id": pid} for pid in existing]}}},
                timeout=15,
            )

    archive_resp = requests.patch(f"{API_BASE}/pages/{loser_id}", headers=_headers(), json={"archived": True}, timeout=15)
    if not archive_resp.ok:
        raise RuntimeError(f"Merged relations but failed to archive duplicate page: {archive_resp.status_code} {archive_resp.text[:300]}")


_related_person_ensured = set()


def ensure_related_person_property(database_id: str, people_database_id: str):
    """Idempotently adds the "Related Person" dual_property relation
    (Tasks/Calendar -> People) to a database that predates this feature --
    see notion_setup.py's create_workspace, which adds it for brand-new
    workspaces at setup time, and push_tasks/push_events' own per-write
    retry-without-Related-Person fallback, which was silently masking the
    fact that some workspaces never got this property at all. Without it,
    every PATCH the app later tries against "Related Person" (both
    resolve_pending_person_link's confirm step, and any direct set at
    creation time) fails validation forever, since the property genuinely
    doesn't exist in the database's schema -- not just unset on one page.
    Safe to call repeatedly -- gated by a per-process, per-database cache."""
    if database_id in _related_person_ensured or not people_database_id:
        return
    ds_id = _data_source_id(database_id)
    people_ds_id = _data_source_id(people_database_id)
    resp = requests.patch(
        f"{API_BASE}/data_sources/{ds_id}", headers=_headers(),
        json={"properties": {"Related Person": {
            "relation": {"data_source_id": people_ds_id, "type": "dual_property", "dual_property": {}}
        }}},
        timeout=15,
    )
    if not resp.ok:
        log.warning("failed to add Related Person property to database %s: %s %s",
                    database_id, resp.status_code, resp.text[:300])
        return
    _related_person_ensured.add(database_id)


_email_schema_ensured = set()


def ensure_email_draft_properties(database_id: str):
    """Idempotently extends a Tasks database with the properties
    push_tasks() needs to show a real email draft (not just a "check the
    dashboard" pointer) and let approval happen right there in Notion:
    "Email Draft Subject" (rich_text -- short enough to show in table
    view) and "Approve & Send" (checkbox, read every poll cycle by
    poller.check_notion_email_approvals_once()). Safe to call repeatedly --
    PATCHing a property that already exists with the same type is a no-op
    on Notion's side -- but gated by a per-process, per-database cache so
    it's not refetched/repatched on every single task write."""
    if database_id in _email_schema_ensured:
        return
    ds_id = _data_source_id(database_id)
    resp = requests.patch(
        f"{API_BASE}/data_sources/{ds_id}", headers=_headers(),
        json={"properties": {
            "Email Draft Subject": {"rich_text": {}},
            "Approve & Send": {"checkbox": {}},
            # Editable recipient address, right on the draft's Task page --
            # prefilled from the People page when known, typed in by the
            # user otherwise (see poller.check_notion_email_approvals_once,
            # which reads it at send time and backfills People).
            "Send To": {"email": {}},
        }},
        timeout=15,
    )
    if not resp.ok:
        # Best-effort -- push_tasks()'s own per-write retry-without-these-
        # properties fallback still keeps task creation working even if
        # this never succeeds (e.g. integration lacks schema-edit rights).
        log.warning("failed to add email draft properties to Tasks database %s: %s %s",
                    database_id, resp.status_code, resp.text[:300])
        return
    _email_schema_ensured.add(database_id)


def push_tasks(record: dict, note_page_id: str = None) -> list:
    """Creates one Notion page per action item in the Tasks database,
    related back to this recording's Notes page. Assignee is only ever set
    on an exact (case-insensitive) name match against real workspace
    members -- see _match_person_exact's docstring for why this isn't a
    fuzzy match. No-ops if there are no action items.

    Returns a list of {"index", "task_page_id", "person_page_id",
    "draft_id"} for each comm_type == "email" action item -- the caller
    (poller.distribute_once) persists this via
    storage.set_task_email_links() so poller.check_notion_email_approvals_once()
    can later find each Task page again to check its "Approve & Send" box,
    and so a successful send can log itself onto the recipient's People
    page (person_page_id). "index"/"draft_id" (f"email-item-{{index}}")
    line up with poller._build_email_drafts's own 1-based action-item
    index -- both iterate the same summary["action_items"] list in the
    same order, so the Nth Task page created here always corresponds to
    the Nth action item's draft."""
    action_items = (record.get("summary") or {}).get("action_items") or []
    if not action_items:
        return []

    database_id = settings.get_all().get("notion_tasks_database_id")
    if not database_id:
        raise RuntimeError("Notion Tasks isn't configured — set a Tasks database ID in /integrations")

    if any(item.get("comm_type") == "email" for item in action_items):
        try:
            ensure_email_draft_properties(database_id)
        except Exception as e:
            log.warning("failed to ensure email draft schema on Tasks database %s: %s", database_id, e)

    people = _safe_list_workspace_people()
    people_database_id = settings.get_all().get("notion_people_database_id")
    if people_database_id:
        try:
            ensure_related_person_property(database_id, people_database_id)
        except Exception as e:
            log.warning("failed to ensure Related Person schema on Tasks database %s: %s", database_id, e)
    meeting = record.get("meeting")
    email_links = []

    for i, item in enumerate(action_items, start=1):
        title = (item.get("text") or record["name"])[:200]
        properties = {
            "Name": {"title": [{"type": "text", "text": {"content": title}}]},
        }
        if item.get("due_date"):
            properties["Due Date"] = {"date": {"start": item["due_date"]}}
        if note_page_id:
            properties["Related Note"] = {"relation": [{"id": note_page_id}]}

        matched = _match_person_exact(item.get("owner"), people)
        if matched:
            properties["Assignee"] = {"people": [{"object": "user", "id": matched["id"]}]}
        elif item.get("owner"):
            # No exact workspace-member match -- don't silently drop the
            # name, note it in the title instead of guessing an assignee.
            properties["Name"] = {"title": [{"type": "text", "text": {"content": f"{title} ({item['owner']})"}}]}

        # Related Person (People database) -- distinct from Assignee above,
        # which only ever points at real Notion workspace members. This is
        # who the task is actually *about* (owner, or the recipient of an
        # email-type action item), so their People page can show every
        # task involving them. Confident (email known, or genuinely no
        # same-name page exists yet) sets the relation immediately;
        # ambiguous (name matches 1+ existing pages, no email) instead
        # registers a pending confirmation for the dashboard rather than
        # guessing which "Vijay" this is.
        person_name = item.get("owner") or (item.get("comm_recipient") if item.get("comm_type") == "email" else None)
        pending_candidates = None
        if person_name and people_database_id:
            email = _attendee_email_for_name(person_name, meeting)
            try:
                person_id, pending_candidates = resolve_person_for_relation(person_name, email, people_database_id, note_page_id)
                if person_id:
                    properties["Related Person"] = {"relation": [{"id": person_id}]}
            except RuntimeError as e:
                log.warning("person relation resolution failed for %r: %s", person_name, e)

        children = _text_block("paragraph", f"From recording: {record['name']}")
        is_email_item = item.get("comm_type") == "email"
        if is_email_item:
            # Approving here (checking the box below) is now the actual
            # send trigger -- see poller.check_notion_email_approvals_once()
            # -- not just a pointer back to the Clicky dashboard. Subject
            # goes in its own property so it's visible in table view; the
            # full body as page content since it can run long. "Send To"
            # holds the recipient's address, prefilled from the People
            # page when known and user-editable right here otherwise --
            # the poller reads it at send time, so typing it in and
            # checking the box is the whole flow, no People-page detour.
            recipient = item.get("comm_recipient")
            subject = item.get("email_subject") or item.get("text", "")
            body = item.get("email_body") or item.get("text", "")
            properties["Email Draft Subject"] = {"rich_text": [{"type": "text", "text": {"content": subject[:2000]}}]}
            properties["Approve & Send"] = {"checkbox": False}
            recipient_email = ""
            if recipient and people_database_id:
                try:
                    person_page = _find_person_page(recipient, people_database_id)
                    if person_page:
                        recipient_email = (person_page["properties"].get("Email", {}).get("email") or "").strip()
                except Exception as e:
                    log.debug("Send To prefill lookup for %r failed: %s", recipient, e)
            if recipient_email:
                properties["Send To"] = {"email": recipient_email}
                hint = "Check \"Approve & Send\" above to send it."
            else:
                hint = "✍️ Enter the recipient's email in \"Send To\" above, then check \"Approve & Send\"."
            children += _text_block(
                "paragraph",
                f"📧 Draft email to {recipient or '(recipient not yet resolved)'}:\n\n{body}\n\n{hint}")

        payload = {
            "parent": {"database_id": database_id, "type": "database_id"},
            "properties": properties,
            "children": children,
        }
        # Progressively strip optional properties a workspace might not
        # have yet (schema-extension may itself have failed above, or an
        # older workspace never got "Related Person") rather than losing
        # the whole task over one missing property.
        optional_props = ["Related Person", "Email Draft Subject", "Approve & Send", "Send To"]
        resp = requests.post(f"{API_BASE}/pages", headers=_headers(), json=payload, timeout=15)
        while not resp.ok and any(p in properties for p in optional_props):
            for p in optional_props:
                if p in properties:
                    del properties[p]
                    break
            resp = requests.post(f"{API_BASE}/pages", headers=_headers(), json=payload, timeout=15)
        if not resp.ok:
            raise RuntimeError(f"Notion API error {resp.status_code}: {resp.text[:300]}")

        task_page = resp.json()
        if pending_candidates:
            storage.add_pending_person_link(
                person_name, task_page["id"], "tasks", pending_candidates, record["name"])

        if is_email_item:
            email_links.append({
                "index": i,
                "task_page_id": task_page["id"],
                "person_page_id": properties.get("Related Person", {}).get("relation", [{}])[0].get("id"),
                "draft_id": f"email-item-{i}",
                "recipient_name": recipient or None,
            })

    log.info("pushed %d task(s) from %s to Notion Tasks", len(action_items), record["name"])
    return email_links


_publication_schema_ensured = set()


# One Publications page per recording (not per platform) -- each platform
# gets its own prefixed property set (f"Approve {label}" etc.) so approval/
# scheduling/status/publish-URL stay independently trackable per platform on
# the shared page. See push_social_posts()/poller.check_publication_approvals_once().
PLATFORM_LABELS = {"substack": "Substack", "medium": "Medium", "linkedin": "LinkedIn", "x": "X"}


def ensure_publication_properties(database_id: str):
    """Idempotently ensures the Publications database has the per-platform
    properties push_social_posts()/poller.check_publication_approvals_once()
    need -- only relevant for a database created by an older version of
    notion_setup.create_publications_database, or hand-created by a user.
    Same gate-and-PATCH pattern as ensure_email_draft_properties(). Additive
    only -- a database that still has the old single Platform/Status/
    Approve & Publish/Scheduled At/Post URL columns from before this page
    was consolidated to one-per-recording keeps them, just unused; Notion's
    API has no clean way to remove a data source property from code."""
    if database_id in _publication_schema_ensured:
        return
    properties = {}
    for label in PLATFORM_LABELS.values():
        properties[f"Approve {label}"] = {"checkbox": {}}
        properties[f"{label} Scheduled At"] = {"date": {}}
        properties[f"{label} Status"] = {"select": {"options": [
            {"name": "Draft"}, {"name": "Scheduled"}, {"name": "Published"}, {"name": "Failed"},
        ]}}
        properties[f"{label} Post URL"] = {"url": {}}
    ds_id = _data_source_id(database_id)
    resp = requests.patch(
        f"{API_BASE}/data_sources/{ds_id}", headers=_headers(),
        json={"properties": properties},
        timeout=15,
    )
    if not resp.ok:
        log.warning("failed to add publication properties to database %s: %s %s",
                    database_id, resp.status_code, resp.text[:300])
        return
    _publication_schema_ensured.add(database_id)


GENERATE_SOCIAL_PROPERTY = "Generate Social Media"
_generate_social_trigger_ensured = set()


def ensure_generate_social_trigger_property(database_id: str):
    """Idempotently adds the "Generate Social Media" checkbox to a Notes
    or Journal database -- checking it on a specific recording's page is
    what triggers poller.check_social_post_generation_triggers_once() to
    generate that recording's social posts (any recording type, on-demand
    only -- there's no more automatic journal-only generation). Safe to
    call on a user's own pre-existing Journal database (e.g. installed
    from Notion's template gallery): Notion allows adding a new property
    to any database you can edit, same assumption push_journal()'s
    schema-adaptive design already relies on. Same gate-and-PATCH pattern
    as ensure_publication_properties()."""
    if not database_id or database_id in _generate_social_trigger_ensured:
        return
    ds_id = _data_source_id(database_id)
    resp = requests.patch(
        f"{API_BASE}/data_sources/{ds_id}", headers=_headers(),
        json={"properties": {GENERATE_SOCIAL_PROPERTY: {"checkbox": {}}}},
        timeout=15,
    )
    if not resp.ok:
        log.warning("failed to add %r property to database %s: %s %s",
                    GENERATE_SOCIAL_PROPERTY, database_id, resp.status_code, resp.text[:300])
        return
    _generate_social_trigger_ensured.add(database_id)


_insight_properties_ensured = set()


def ensure_insight_properties(database_id: str):
    """Idempotently adds Topics/Intents/Deepgram Summary properties to a
    Notes or Journal database -- these used to only ever be baked into the
    LLM prompt (providers.base.build_summary_prompt) and the page body
    text (see _build_blocks), never queryable/filterable as real database
    columns. Same gate-and-PATCH pattern as ensure_publication_properties.
    Only meaningful when Deepgram is the STT provider (see
    deepgram_provider.py) -- harmless no-op columns otherwise."""
    if not database_id or database_id in _insight_properties_ensured:
        return
    ds_id = _data_source_id(database_id)
    resp = requests.patch(
        f"{API_BASE}/data_sources/{ds_id}", headers=_headers(),
        json={"properties": {
            "Topics": {"multi_select": {}},
            "Intents": {"multi_select": {}},
            "Deepgram Summary": {"rich_text": {}},
        }},
        timeout=15,
    )
    if not resp.ok:
        log.warning("failed to add insight properties to database %s: %s %s",
                    database_id, resp.status_code, resp.text[:300])
        return
    _insight_properties_ensured.add(database_id)


def _insight_properties(record: dict) -> dict:
    """Shared by push_recording/push_journal -- Topics/Intents/Deepgram
    Summary properties built from record["deepgram_insights"], if any."""
    insights = record.get("deepgram_insights") or {}
    properties = {}
    # Notion's multi_select option names can't contain a comma -- Deepgram's
    # topic/intent phrases occasionally do (e.g. "project planning, budget").
    topics = insights.get("topics") or []
    if topics:
        properties["Topics"] = {"multi_select": [{"name": t.replace(",", "/")[:100]} for t in topics[:20]]}
    intents = insights.get("intents") or []
    if intents:
        properties["Intents"] = {"multi_select": [{"name": i.replace(",", "/")[:100]} for i in intents[:20]]}
    if insights.get("summary"):
        properties["Deepgram Summary"] = {"rich_text": [{"type": "text", "text": {"content": insights["summary"][:MAX_TEXT_LEN]}}]}
    return properties


def is_generate_social_triggered(page_id: str) -> bool:
    """Reads whether a recording's Notes or Journal page has the
    "Generate Social Media" checkbox currently checked."""
    page = get_page(page_id)
    return bool((page.get("properties") or {}).get(GENERATE_SOCIAL_PROPERTY, {}).get("checkbox"))


def reset_generate_social_trigger(page_id: str):
    """Unchecks the "Generate Social Media" checkbox after generation runs
    -- makes it a momentary "do it now" action rather than a persistent
    state, so checking it again later regenerates a fresh batch of posts
    (new Publications pages) instead of needing separate schema to
    distinguish "already handled this check" from "please redo it"."""
    resp = requests.patch(
        f"{API_BASE}/pages/{page_id}", headers=_headers(),
        json={"properties": {GENERATE_SOCIAL_PROPERTY: {"checkbox": False}}},
        timeout=15,
    )
    if not resp.ok:
        log.warning("failed to reset %r on page %s: %s %s",
                    GENERATE_SOCIAL_PROPERTY, page_id, resp.status_code, resp.text[:300])


def push_social_posts(record: dict, note_page_id: str = None) -> dict:
    """Creates ONE Publications-database page for this recording (not one
    per platform, as before) -- see poller.check_social_post_generation_triggers_once,
    which calls providers.base.build_social_post_prompt. The page body is
    sectioned per-platform ("Transcription Summary" first, then a heading +
    the draft body for each platform present) so a user can read and edit
    every version of the post in one place, while approval/scheduling/status
    stay independently trackable per platform via prefixed properties
    (f"Approve {label}" etc., see ensure_publication_properties). Returns
    {"notion_page_id": ...} (record-level now, not per-platform) for the
    caller to persist via storage.set_notion_publication_page_id().

    "posts" is {platform: {"title": str, "body": str}} -- Medium has no
    platform-specific client (see software's Medium-is-manual design) but
    still gets a section + property set, with its Status left at "Draft"
    forever (no auto-publish path exists for it).
    """
    database_id = settings.get_all().get("notion_publications_database_id")
    if not database_id:
        raise RuntimeError("Notion Publications isn't configured — set it up in /integrations")
    posts = (record.get("social_posts") or {})
    if not posts:
        return {}

    try:
        ensure_publication_properties(database_id)
    except Exception as e:
        log.warning("failed to ensure publication schema on database %s: %s", database_id, e)

    properties = {
        "Name": {"title": [{"type": "text", "text": {"content": record["name"][:200]}}]},
    }
    if note_page_id:
        properties["Source Recording"] = {"relation": [{"id": note_page_id}]}
    for platform in posts:
        label = PLATFORM_LABELS.get(platform, platform.capitalize())
        properties[f"Approve {label}"] = {"checkbox": False}
        properties[f"{label} Status"] = {"select": {"name": "Draft"}}

    children = [_heading("Transcription Summary")]
    children += _text_block("paragraph", (record.get("summary") or {}).get("summary") or "(no summary)")
    for platform, post in posts.items():
        label = PLATFORM_LABELS.get(platform, platform.capitalize())
        children.append(_heading(f"{label} Post"))
        children += _text_block("paragraph", post.get("body") or "")

    payload = {
        "parent": {"database_id": database_id, "type": "database_id"},
        "properties": properties,
        "children": children,
    }
    resp = requests.post(f"{API_BASE}/pages", headers=_headers(), json=payload, timeout=15)
    if not resp.ok:
        log.warning("failed to create Publications page for %s: %s %s",
                    record["name"], resp.status_code, resp.text[:300])
        return {}

    page_id = resp.json()["id"]
    log.info("pushed %d social post draft(s) from %s to one Notion Publications page", len(posts), record["name"])
    return {"notion_page_id": page_id}


def update_publication_platform_status(page_id: str, platform: str, status: str, url: str = None):
    """Writes one platform's f"{label} Status" select (and f"{label} Post
    URL", if given) back onto the shared Publications page -- called from
    poller.py's check_publication_approvals_once() (Draft -> Scheduled, once
    that platform's checkbox+date are seen; Draft -> Published for Medium's
    manual-URL path) and check_social_publish_once() (Scheduled ->
    Published/Failed once an automated publish attempt resolves). Without
    this, the local dashboard's own status tracking (storage.social_posts)
    was silently diverging from what the user actually sees in Notion.
    `status` must be one of the select options already created by
    ensure_publication_properties/notion_setup (Draft/Scheduled/Published/Failed)."""
    label = PLATFORM_LABELS.get(platform, platform.capitalize())
    properties = {f"{label} Status": {"select": {"name": status}}}
    if url:
        properties[f"{label} Post URL"] = {"url": url}
    resp = requests.patch(f"{API_BASE}/pages/{page_id}", headers=_headers(), json={"properties": properties}, timeout=15)
    if not resp.ok:
        log.warning("failed to update Publications page %s %s status to %r: %s %s",
                    page_id, label, status, resp.status_code, resp.text[:300])


def push_events(record: dict, note_page_id: str = None):
    """Creates Notion Calendar pages from three independent sources, all
    related back to this recording's Notes page:

    1. Events explicitly mentioned in the transcript ("calendar_events" --
       a future meeting/appointment the speaker talked about).
    2. An "entry date" marker -- when this recording/meeting actually
       happened. Real meeting start time when this is a detected meeting
       (record["meeting"]["start"]), else the recording's own created_at
       for a standalone memo. Only pushed when there's a real meeting OR
       at least one due-dated action item below, so a throwaway memo with
       nothing due doesn't clutter the calendar with a bare marker.
    3. Each action item's own due date, as its OWN separate entry -- a
       single meeting or memo can produce several tasks each due on a
       different day, so these must land on their own dates rather than
       all clustering on the meeting/entry date. This is what makes a due
       date something you actually see on your calendar instead of only
       buried in the Tasks database's "Due Date" column.

    No-ops entirely if the Calendar database isn't configured, or none of
    the three sources above have anything to contribute.
    """
    database_id = settings.get_all().get("notion_events_database_id")
    if not database_id:
        return

    summary = record.get("summary") or {}
    calendar_events = summary.get("calendar_events") or []
    due_items = [it for it in (summary.get("action_items") or []) if it.get("due_date")]
    meeting = record.get("meeting")

    if not calendar_events and not due_items and not meeting:
        return

    people_database_id = settings.get_all().get("notion_people_database_id")
    if people_database_id:
        try:
            ensure_related_person_property(database_id, people_database_id)
        except Exception as e:
            log.warning("failed to ensure Related Person schema on Calendar database %s: %s", database_id, e)

    def _create_event_page(title: str, date_value: dict, person_ids: list = None, pending: tuple = None):
        properties = {
            "Name": {"title": [{"type": "text", "text": {"content": title[:200]}}]},
            "Date": {"date": date_value},
        }
        if note_page_id:
            properties["Related Note"] = {"relation": [{"id": note_page_id}]}
        if person_ids:
            properties["Related Person"] = {"relation": [{"id": pid} for pid in person_ids]}
        payload = {"parent": {"database_id": database_id, "type": "database_id"}, "properties": properties}
        resp = requests.post(f"{API_BASE}/pages", headers=_headers(), json=payload, timeout=15)
        if not resp.ok and "Related Person" in properties:
            del properties["Related Person"]  # older workspace without this relation yet
            resp = requests.post(f"{API_BASE}/pages", headers=_headers(), json=payload, timeout=15)
        if not resp.ok:
            raise RuntimeError(f"Notion API error {resp.status_code}: {resp.text[:300]}")
        if pending:
            person_name, candidates = pending
            storage.add_pending_person_link(person_name, resp.json()["id"], "events", candidates, record["name"])

    pushed = 0

    for ev in calendar_events:
        if not ev.get("date"):
            continue  # need at least a date to place this on the calendar at all
        date_value = {"start": ev["date"]}
        if ev.get("time"):
            date_value["start"] = f"{ev['date']}T{ev['time']}:00"
        _create_event_page(f"📅 {ev.get('title') or 'Untitled event'}", date_value)
        pushed += 1

    if meeting or due_items:
        entry_start = (meeting or {}).get("start") or record.get("created_at")
        if entry_start:
            entry_title = (meeting or {}).get("title") or record["name"]
            # Meeting attendees each have a real email address already (the
            # calendar invite), so this is always a confident resolution --
            # never registers a pending confirmation, unlike the name-only
            # cases below.
            person_ids = []
            if people_database_id:
                for a in (meeting or {}).get("attendees") or []:
                    if not a.get("name"):
                        continue
                    try:
                        pid, _ = resolve_person_for_relation(a["name"], a.get("email", ""), people_database_id, note_page_id)
                        if pid:
                            person_ids.append(pid)
                    except RuntimeError as e:
                        log.warning("person relation resolution failed for attendee %r: %s", a.get("name"), e)
            _create_event_page(f"🎙️ {entry_title}", {"start": entry_start}, person_ids=person_ids or None)
            pushed += 1

    for it in due_items:
        pending = None
        person_ids = None
        person_name = it.get("owner") or (it.get("comm_recipient") if it.get("comm_type") == "email" else None)
        if person_name and people_database_id:
            email = _attendee_email_for_name(person_name, meeting)
            try:
                pid, candidates = resolve_person_for_relation(person_name, email, people_database_id, note_page_id)
                if pid:
                    person_ids = [pid]
                elif candidates:
                    pending = (person_name, candidates)
            except RuntimeError as e:
                log.warning("person relation resolution failed for %r: %s", person_name, e)
        _create_event_page(f"✅ Due: {it.get('text', '')}", {"start": it["due_date"]}, person_ids=person_ids, pending=pending)
        pushed += 1

    if pushed:
        log.info("pushed %d calendar entr%s from %s to Notion Calendar",
                  pushed, "y" if pushed == 1 else "ies", record["name"])


def resolve_pending_person_link(notion_page_ids: list, person_page_id: str = None,
                                 new_person_name: str = None, people_database_id: str = None) -> tuple:
    """Applies a user's dashboard/Notion confirmation by patching every
    already-created Task/Calendar page's "Related Person" relation to the
    SAME person -- either an existing People page the user picked, or one
    brand-new page created once and reused for every target (never one
    new person page per target -- see storage.add_pending_person_link's
    dedup, which is what puts more than one page id in notion_page_ids in
    the first place: the same ambiguous name showing up in both a Task
    and a Calendar entry for one recording used to register two separate
    confirmations and, if each created its own "new person" page, two
    duplicate People pages for what's actually the same person).

    Returns (person_page_id, failed_page_ids) -- failed_page_ids is the
    subset that couldn't be patched right now due to a transient error
    (caller should re-register just those for retry). A target page
    that's genuinely gone (404) is logged and skipped, never retried,
    since retrying a deleted page can never succeed."""
    if person_page_id is None:
        if not new_person_name or not people_database_id:
            raise RuntimeError("need either an existing person_page_id or a new_person_name + people_database_id")
        page = _create_minimal_person_page(new_person_name, None, people_database_id)
        person_page_id = page["id"]

    failed = []
    for page_id in notion_page_ids:
        resp = requests.patch(
            f"{API_BASE}/pages/{page_id}", headers=_headers(),
            json={"properties": {"Related Person": {"relation": [{"id": person_page_id}]}}},
            timeout=15,
        )
        if resp.status_code == 404:
            log.warning("Related Person target page %s no longer exists -- skipping", page_id)
            continue
        if not resp.ok:
            log.warning("failed to patch Related Person on %s: %s %s", page_id, resp.status_code, resp.text[:300])
            failed.append(page_id)
    return person_page_id, failed


def push_journal(record: dict, note_page_id: str = None):
    """Pushes recordings the summarizer classified as "journal" (self-
    reflective, no tasks/other people involved -- see providers/base.py's
    "type" field) into a Journal database. This is now the ONLY Notion
    destination for a journal-classified recording (poller.distribute_once
    skips the main Notes push for these, so a journal entry doesn't show
    up duplicated in both places) -- note_page_id is therefore usually
    None here; the "Related Note" relation below is set only when it
    happens to be provided and the property exists. Unlike Tasks/People/
    Calendar, this database is expected to be
    the user's own pre-existing Notion Journal (installed from Notion's
    template gallery) or similar -- we don't control its schema, so this
    adapts: finds whatever property is type=title (Notion lets a database
    creator name it anything), sets a date property only if one exists
    named "Date", and only sets a "Related Note" relation if that property
    happens to exist. No-ops if the Journal database isn't configured, or
    if this recording wasn't classified as a journal entry."""
    if (record.get("summary") or {}).get("type") != "journal":
        return
    database_id = settings.get_all().get("notion_journal_database_id")
    if not database_id:
        return

    schema = _schema_properties(database_id)
    title_prop = next((name for name, ptype in schema.items() if ptype == "title"), None)
    if not title_prop:
        raise RuntimeError(f"Notion Journal database {database_id} has no title property")

    writeup = (record.get("summary") or {}).get("journal_writeup") or {}
    title = (writeup.get("title") or record.get("summary", {}).get("summary") or record["name"])[:200]
    properties = {title_prop: {"title": [{"type": "text", "text": {"content": title}}]}}

    if schema.get("Date") == "date" and record.get("created_at"):
        properties["Date"] = {"date": {"start": record["created_at"][:10]}}
    if schema.get("Related Note") == "relation" and note_page_id:
        properties["Related Note"] = {"relation": [{"id": note_page_id}]}
    # Same conservative, adapt-don't-force approach as Date/Related Note
    # above -- only set these if the user's own Journal database already
    # happens to have them, rather than force-adding new columns to a
    # database we don't own the schema of (unlike the app's own Notes
    # database, see ensure_insight_properties/push_recording).
    insight_props = _insight_properties(record)
    for name, value in insight_props.items():
        prop_type = "multi_select" if name in ("Topics", "Intents") else "rich_text"
        if schema.get(name) == prop_type:
            properties[name] = value

    payload = {
        "parent": {"database_id": database_id, "type": "database_id"},
        "properties": properties,
        "children": _build_journal_blocks(record),
    }
    resp = requests.post(f"{API_BASE}/pages", headers=_headers(), json=payload, timeout=15)
    if not resp.ok:
        raise RuntimeError(f"Notion API error {resp.status_code}: {resp.text[:300]}")
    page_id = resp.json()["id"]
    log.info("pushed %s to Notion Journal", record["name"])

    try:
        import rag_index
        index_text = f"{title}\n\n{_page_plain_text(page_id)}"
        rag_index.index_text("notion", page_id, index_text, date=(record.get("created_at") or "")[:10] or None)
    except Exception as e:
        log.warning("rag_index indexing failed for Notion Journal page %s (non-fatal): %s", page_id, e)

    return page_id
