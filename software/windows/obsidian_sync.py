"""Local-vault counterpart to notion_sync.py -- writes recordings, tasks,
people, calendar entries, journal write-ups, and social-post drafts as
markdown files directly into an Obsidian vault folder. No API, no auth --
Obsidian just watches its vault folder on disk, so dropping/patching a .md
file there is the entire integration.

Frontmatter (YAML) stands in for Notion's page PROPERTIES: Obsidian's own
Properties panel renders `approve_x: false` as a real checkbox and
`x_scheduled_at:` as a date picker, so the exact same "poll every cycle,
read a boolean, act, write back" pattern poller.py already uses for Notion
carries over unchanged -- see check_publication_approvals_once() etc. in
poller.py, which read/write this file's read_frontmatter/_update_frontmatter
instead of notion_sync.get_page/update_publication_platform_status.

Vault layout: push_recording() writes to the vault ROOT (unchanged from
before this file grew Tasks/People/Calendar/Journal/Publications support --
backward compatible with an existing vault/links). Every other entity type
gets its own subfolder: Tasks/, People/, Calendar/, Journal/, Publications/.
Notion's relation properties become Obsidian [[wiki-links]] in frontmatter/
body instead.

Deliberate scope reductions vs. the Notion side (kept simple on purpose,
not oversights):
- People notes append a dated "Mentioned in ..." section on every mention
  rather than notion_sync's LLM-merged rolling "Knowledge" paragraph -- an
  LLM call per person per recording is a real cost/complexity jump this
  local-only integration doesn't need to match.
- No ambiguous-name pending-confirmation flow (notion_sync.
  resolve_person_for_relation's ambiguous-candidates UX): a person's note
  is keyed by their slugified name, one file per unique name, so the
  "which of these 3 same-named Notion pages" problem mostly doesn't exist
  here -- a wiki-link to People/{slug}.md is always unambiguous.
"""
import logging
import os
import re

import yaml

import settings

log = logging.getLogger("obsidian_sync")

PLATFORM_LABELS = {"substack": "Substack", "medium": "Medium", "linkedin": "LinkedIn", "x": "X"}


def _slugify(text: str, max_len: int = 60) -> str:
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    text = re.sub(r"[\s_]+", "-", text)
    return text[:max_len] or "recording"


def _vault_path() -> str:
    vault_path = settings.get_all().get("obsidian_vault_path")
    if not vault_path:
        raise RuntimeError("Obsidian isn't configured — set a vault folder path in /integrations")
    if not os.path.isdir(vault_path):
        raise RuntimeError(f"Obsidian vault path does not exist or isn't a folder: {vault_path}")
    return vault_path


def _vault_subfolder(name: str) -> str:
    """Resolves (and creates if needed) a named subfolder under the vault
    root -- Tasks/, People/, Calendar/, Journal/, Publications/."""
    path = os.path.join(_vault_path(), name)
    os.makedirs(path, exist_ok=True)
    return path


def _note_title(record: dict) -> str:
    """The exact "{date} {slug}" string push_recording() uses as its own
    filename stem -- factored out so Tasks/People/Calendar/Publications
    can compute the same string to build a [[wiki-link]] back to a
    recording's main note without needing that note's path threaded
    through every call."""
    date_prefix = (record.get("created_at") or "")[:10]  # YYYY-MM-DD
    slug = _slugify(record.get("summary", {}).get("summary") or record["name"])
    return f"{date_prefix} {slug}" if date_prefix else slug


def _wiki_link(title: str) -> str:
    return f"[[{title}]]"


_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.DOTALL)


def _write_note(dir_path: str, filename: str, frontmatter: dict, body: str) -> str:
    """Creates a new note (or fully overwrites one) with the given
    frontmatter + body. Used by every push_* below for a fresh write --
    see _update_frontmatter for patching just one field on an existing
    note without touching its body."""
    path = os.path.join(dir_path, filename)
    fm_text = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).strip()
    with open(path, "w") as f:
        f.write(f"---\n{fm_text}\n---\n\n{body}")
    return path


def read_frontmatter(path: str) -> dict:
    """Reads just the YAML frontmatter block of a note. Returns {} if the
    file is missing or has no recognizable frontmatter -- fail open, same
    posture callers already take with notion_sync.get_page (a page/file
    that vanished or was hand-edited into something unparseable shouldn't
    crash a poll cycle, just skip that one record until it's fixed)."""
    try:
        with open(path, "r") as f:
            text = f.read()
    except OSError:
        return {}
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}
    try:
        return yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return {}


def _update_frontmatter(path: str, **fields):
    """Patches just the given frontmatter keys on an existing note,
    leaving every other key and the whole body untouched. No-ops (logs a
    warning) if the file doesn't exist or has no frontmatter block to
    patch -- e.g. the user deleted/moved it."""
    try:
        with open(path, "r") as f:
            text = f.read()
    except OSError as e:
        log.warning("failed to patch frontmatter on %s: %s", path, e)
        return
    m = _FRONTMATTER_RE.match(text)
    if not m:
        log.warning("no frontmatter block found in %s -- skipping patch", path)
        return
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        log.warning("unparseable frontmatter in %s -- skipping patch", path)
        return
    fm.update(fields)
    body = m.group(2)
    fm_text = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).strip()
    with open(path, "w") as f:
        f.write(f"---\n{fm_text}\n---\n\n{body}")


def append_body(path: str, text: str):
    """Appends a paragraph to a note's body (after its frontmatter),
    leaving the frontmatter itself untouched -- used for the "Sent"
    confirmation line on a Task note after an approved email goes out
    (mirrors notion_sync.append_blocks's use in
    poller.check_notion_email_approvals_once)."""
    try:
        with open(path, "a") as f:
            f.write(f"\n\n{text}\n")
    except OSError as e:
        log.warning("failed to append to %s: %s", path, e)


def _format_markdown(record: dict) -> str:
    summary = record.get("summary") or {}

    lines = [
        f"# {record.get('name', 'Voice memo')}",
        "",
        "## Summary",
        "",
        summary.get("summary") or "(no summary)",
        "",
    ]

    action_items = summary.get("action_items") or []
    if action_items:
        lines += ["## Action items", ""]
        for item in action_items:
            line = f"- [ ] {item.get('text', '')}"
            if item.get("owner"):
                line += f" — **{item['owner']}**"
            if item.get("due_date"):
                line += f" (due {item['due_date']})"
            lines.append(line)
        lines.append("")

    follow_ups = summary.get("follow_ups") or []
    if follow_ups:
        lines += ["## Follow-ups", ""]
        for fu in follow_ups:
            line = f"- {fu.get('text', '')}"
            if fu.get("owner"):
                line += f" — **{fu['owner']}**"
            lines.append(line)
        lines.append("")

    stakeholders = summary.get("stakeholders") or []
    if stakeholders:
        lines += ["## Stakeholders", ""]
        for s in stakeholders:
            line = f"- **{s.get('name', '')}**"
            if s.get("note"):
                line += f" — {s['note']}"
            lines.append(line)
        lines.append("")

    calendar_events = summary.get("calendar_events") or []
    if calendar_events:
        lines += ["## Calendar events", ""]
        for ev in calendar_events:
            line = f"- {ev.get('title', '')}"
            if ev.get("date"):
                line += f" — {ev['date']}"
            if ev.get("time"):
                line += f" {ev['time']}"
            lines.append(line)
        lines.append("")

    lines += ["## Transcript", ""]
    segments = record.get("segments")
    if segments:
        for seg in segments:
            lines.append(f"**Speaker {seg.get('speaker_id', '?')}:** {seg.get('text', '').strip()}")
            lines.append("")
    else:
        lines.append(record.get("transcript") or "")

    return "\n".join(lines)


def _insight_frontmatter(record: dict) -> dict:
    """Topics/Intents/Deepgram Summary as real frontmatter fields -- see
    macOS obsidian_sync.py for the full rationale."""
    insights = record.get("deepgram_insights") or {}
    fm = {}
    if insights.get("topics"):
        fm["topics"] = insights["topics"][:20]
    if insights.get("intents"):
        fm["intents"] = insights["intents"][:20]
    if insights.get("summary"):
        fm["deepgram_summary"] = insights["summary"]
    return fm


def push_recording(record: dict) -> str:
    """Writes this recording as a markdown file into the vault root.
    Journal-classified recordings skip this entirely (see push_journal) --
    same dedup as notion_sync (a journal entry's only home is the Journal
    folder, not also here). "generate_social_media" frontmatter is the
    trigger poller.check_social_post_generation_triggers_once() polls, same
    property as notion_sync.GENERATE_SOCIAL_PROPERTY. Returns the note's
    path -- caller (poller.distribute_once) persists it via
    storage.set_obsidian_note_path() so Tasks/People/Calendar/Publications
    can wiki-link back to it and so the trigger can be polled."""
    if (record.get("summary") or {}).get("type") == "journal":
        return None
    vault_path = _vault_path()
    title = _note_title(record)
    filename = f"{title}.md"
    file_path = os.path.join(vault_path, filename)

    created_at = record.get("created_at", "")
    frontmatter = {
        "created": created_at,
        "stt_provider": record.get("stt_provider", ""),
        "llm_provider": record.get("llm_provider", ""),
        "source_recording": record.get("name", ""),
        "generate_social_media": False,
    }
    frontmatter.update(_insight_frontmatter(record))
    body = _format_markdown(record)
    _write_note(vault_path, filename, frontmatter, body)
    log.info("wrote %s to Obsidian vault", filename)

    try:
        import rag_index
        rag_index.index_text("obsidian", file_path, body, date=created_at[:10] if created_at else None)
    except Exception as e:
        log.warning("rag_index indexing failed for %s (non-fatal): %s", filename, e)

    return file_path


def push_journal(record: dict) -> str:
    """Journal-specific note -- Reflection/Key Learnings/Action Items/
    Notable Points/Transcript, built from the dedicated LLM call in
    poller.process_once (providers.base.build_journal_writeup_prompt), same
    structure as notion_sync._build_journal_blocks. Falls back to the
    plain Summary/Action items/... layout (_format_markdown) if
    "journal_writeup" is missing (older recording, or that LLM call
    failed) so the note is never empty. Only called for
    summary.type == "journal" (see poller.distribute_once). Returns the
    note's path."""
    dir_path = _vault_subfolder("Journal")
    title = _note_title(record)
    filename = f"{title}.md"

    writeup = (record.get("summary") or {}).get("journal_writeup")
    frontmatter = {
        "created": record.get("created_at", ""),
        "generate_social_media": False,
    }
    frontmatter.update(_insight_frontmatter(record))
    if not writeup:
        body = _format_markdown(record)
    else:
        parts = [
            f"# {writeup.get('title') or record.get('name', 'Journal entry')}",
            "",
            "## Reflection",
            "",
            writeup.get("reflection") or "(no reflection)",
            "",
        ]
        for heading, key in (("Key Learnings", "key_learnings"), ("Action Items", "action_items"),
                              ("Notable Points", "notable_points")):
            items = writeup.get(key) or []
            parts += [f"## {heading}", ""]
            parts += [f"- {it}" for it in items] if items else ["(none)"]
            parts.append("")
        parts += ["## Transcript", "", record.get("transcript") or ""]
        body = "\n".join(parts)

    path = _write_note(dir_path, filename, frontmatter, body)
    log.info("wrote %s to Obsidian Journal", filename)

    try:
        import rag_index
        created_at = record.get("created_at", "")
        rag_index.index_text("obsidian", path, body, date=created_at[:10] if created_at else None)
    except Exception as e:
        log.warning("rag_index indexing failed for %s (non-fatal): %s", filename, e)

    return path


def push_tasks(record: dict, note_path: str = None) -> list:
    """Creates one note per action item in Tasks/, wiki-linked back to the
    recording's main note. Returns a list of {"index", "task_note_path",
    "person_note_path", "draft_id", "recipient_name"} for each
    comm_type == "email" item -- same shape poller.distribute_once persists
    via storage.set_task_email_links() for Notion, with "_note_path" keys
    instead of "_page_id" ones so a link can carry Notion fields, Obsidian
    fields, or both (see storage.py). "index"/"draft_id" line up with
    poller._build_email_drafts's own 1-based action-item index, same as
    notion_sync.push_tasks."""
    action_items = (record.get("summary") or {}).get("action_items") or []
    if not action_items:
        return []
    dir_path = _vault_subfolder("Tasks")
    recording_title = _note_title(record)
    links = []

    for i, item in enumerate(action_items, start=1):
        filename = f"{recording_title} - item{i}.md"
        person_name = item.get("owner") or (item.get("comm_recipient") if item.get("comm_type") == "email" else None)
        person_path = None
        frontmatter = {}
        if item.get("due_date"):
            frontmatter["due_date"] = item["due_date"]
        if item.get("owner"):
            frontmatter["owner"] = item["owner"]
        if note_path:
            frontmatter["related_note"] = _wiki_link(recording_title)
        if person_name:
            frontmatter["related_person"] = _wiki_link(person_name)
            person_path = os.path.join(_vault_subfolder("People"), f"{_slugify(person_name)}.md")

        body_lines = [f"# {item.get('text', '')}", "", f"From recording: {record['name']}"]
        is_email_item = item.get("comm_type") == "email"
        if is_email_item:
            recipient = item.get("comm_recipient")
            subject = item.get("email_subject") or item.get("text", "")
            body = item.get("email_body") or item.get("text", "")
            frontmatter["approve_send"] = False
            frontmatter["send_to"] = ""
            frontmatter["draft_id"] = f"email-item-{i}"
            frontmatter["status"] = "pending"
            # Prefill Send To from the recipient's People note if it has an
            # email already recorded, same as notion_sync's Send To prefill.
            if person_path and os.path.isfile(person_path):
                existing_email = read_frontmatter(person_path).get("email")
                if existing_email:
                    frontmatter["send_to"] = existing_email
                    hint = 'Check "approve_send" above to send it.'
                else:
                    hint = '✍️ Enter the recipient\'s email in "send_to" above, then check "approve_send".'
            else:
                hint = '✍️ Enter the recipient\'s email in "send_to" above, then check "approve_send".'
            body_lines += ["", f"📧 Draft email to {recipient or '(recipient not yet resolved)'}:", "",
                           subject, "", body, "", hint]

        _write_note(dir_path, filename, frontmatter, "\n".join(body_lines))
        task_path = os.path.join(dir_path, filename)

        if is_email_item:
            links.append({
                "index": i,
                "task_note_path": task_path,
                "person_note_path": person_path,
                "draft_id": f"email-item-{i}",
                "recipient_name": recipient or None,
            })

    log.info("pushed %d task(s) from %s to Obsidian Tasks", len(action_items), record["name"])
    return links


def push_people(record: dict, note_path: str = None):
    """Find-or-create a note per mentioned person (stakeholders, which
    already includes the speaker -- see poller._add_speakers_as_stakeholders)
    in People/. Unlike notion_sync.push_people's LLM-merged rolling
    "Knowledge" paragraph, this appends a dated "Mentioned in ..." section
    on every mention -- simpler, still builds a real history, no extra LLM
    call. No-ops if there are no stakeholders or no vault configured."""
    stakeholders = (record.get("summary") or {}).get("stakeholders") or []
    if not stakeholders:
        return
    dir_path = _vault_subfolder("People")
    recording_title = _note_title(record)
    created_at = (record.get("created_at") or "")[:10]

    for s in stakeholders:
        name = (s.get("name") or "").strip()
        if not name:
            continue
        path = os.path.join(dir_path, f"{_slugify(name)}.md")
        mention = f"## Mentioned in {_wiki_link(recording_title)} ({created_at})"
        if s.get("note"):
            mention += f"\n\n{s['note']}"

        if os.path.isfile(path):
            existing_fm = read_frontmatter(path)
            with open(path, "r") as f:
                text = f.read()
            m = _FRONTMATTER_RE.match(text)
            body = m.group(2) if m else text
            fm_text = yaml.safe_dump(existing_fm, sort_keys=False, allow_unicode=True).strip()
            with open(path, "w") as f:
                f.write(f"---\n{fm_text}\n---\n\n{body.rstrip()}\n\n{mention}\n")
        else:
            frontmatter = {"name": name, "email": "", "linkedin": "", "note": s.get("note") or ""}
            _write_note(dir_path, f"{_slugify(name)}.md", frontmatter, f"# {name}\n\n{mention}\n")

    log.info("pushed %d stakeholder(s) from %s to Obsidian People", len(stakeholders), record["name"])


def push_events(record: dict, note_path: str = None):
    """Creates Calendar/ notes from the same three sources as
    notion_sync.push_events: transcript-mentioned events, an entry-date
    marker for the recording/meeting itself, and each due-dated action
    item as its own entry. No-ops if none of the three have anything."""
    summary = record.get("summary") or {}
    calendar_events = summary.get("calendar_events") or []
    due_items = [it for it in (summary.get("action_items") or []) if it.get("due_date")]
    meeting = record.get("meeting")
    if not calendar_events and not due_items and not meeting:
        return

    dir_path = _vault_subfolder("Calendar")
    recording_title = _note_title(record)
    pushed = 0

    def _create_event_note(title: str, date: str, time: str = None, person_name: str = None):
        nonlocal pushed
        frontmatter = {"date": date}
        if time:
            frontmatter["time"] = time
        if note_path:
            frontmatter["related_note"] = _wiki_link(recording_title)
        if person_name:
            frontmatter["related_person"] = _wiki_link(person_name)
        filename = f"{date} {_slugify(title)}.md"
        _write_note(dir_path, filename, frontmatter, f"# {title}")
        pushed += 1

    for ev in calendar_events:
        if not ev.get("date"):
            continue
        _create_event_note(f"📅 {ev.get('title') or 'Untitled event'}", ev["date"], ev.get("time"))

    if meeting or due_items:
        entry_start = (meeting or {}).get("start") or record.get("created_at")
        if entry_start:
            entry_title = (meeting or {}).get("title") or record["name"]
            _create_event_note(f"🎙️ {entry_title}", entry_start[:10], entry_start[11:16] if len(entry_start) > 10 else None)

    for it in due_items:
        person_name = it.get("owner") or (it.get("comm_recipient") if it.get("comm_type") == "email" else None)
        _create_event_note(f"✅ Due: {it.get('text', '')}", it["due_date"], person_name=person_name)

    if pushed:
        log.info("pushed %d calendar entr%s from %s to Obsidian Calendar",
                  pushed, "y" if pushed == 1 else "ies", record["name"])


def push_social_posts(record: dict, note_path: str = None) -> str:
    """Creates ONE Publications/ note for this recording (same shape as
    notion_sync.push_social_posts), sectioned per platform in the body
    with per-platform approval frontmatter (approve_{platform},
    {platform}_scheduled_at, {platform}_status, {platform}_post_url) --
    Obsidian's Properties panel renders these as real checkboxes/date
    pickers. Returns the note's path for the caller to persist via
    storage.set_obsidian_publication_note_path()."""
    posts = record.get("social_posts") or {}
    if not posts:
        return None
    dir_path = _vault_subfolder("Publications")
    recording_title = _note_title(record)
    filename = f"{recording_title}.md"

    frontmatter = {}
    if note_path:
        frontmatter["source_recording"] = _wiki_link(recording_title)
    for platform in posts:
        label = PLATFORM_LABELS.get(platform, platform.capitalize())
        key = platform
        frontmatter[f"approve_{key}"] = False
        frontmatter[f"{key}_scheduled_at"] = ""
        frontmatter[f"{key}_status"] = "draft"
        frontmatter[f"{key}_post_url"] = ""

    body_lines = [f"# {record['name']}", "", "## Transcription Summary", "",
                  (record.get("summary") or {}).get("summary") or "(no summary)", ""]
    for platform, post in posts.items():
        label = PLATFORM_LABELS.get(platform, platform.capitalize())
        body_lines += [f"## {label} Post", "", post.get("body") or "", ""]

    path = _write_note(dir_path, filename, frontmatter, "\n".join(body_lines))
    log.info("pushed %d social post draft(s) from %s to one Obsidian Publications note", len(posts), record["name"])
    return path


def update_publication_platform_status(note_path: str, platform: str, status: str, url: str = None):
    """Patches one platform's {platform}_status (and {platform}_post_url,
    if given) frontmatter on the shared Publications note -- Obsidian
    equivalent of notion_sync.update_publication_platform_status."""
    fields = {f"{platform}_status": status}
    if url:
        fields[f"{platform}_post_url"] = url
    _update_frontmatter(note_path, **fields)


def is_generate_social_triggered(note_path: str) -> bool:
    """Reads whether a recording's main or Journal note has the
    "generate_social_media" frontmatter checkbox currently checked."""
    return bool(read_frontmatter(note_path).get("generate_social_media"))


def backfill_person_email(name: str, email: str):
    """Writes a user-supplied address (typed into a Task note's "send_to"
    frontmatter, see poller.check_obsidian_email_approvals_once) onto that
    person's People/ note frontmatter, if the note exists and doesn't
    already have one -- People/ stays the long-term address book, so every
    future email to this person auto-resolves without retyping. Mirrors
    notion_sync.backfill_person_email."""
    if not (name and email):
        return
    try:
        path = os.path.join(_vault_subfolder("People"), f"{_slugify(name)}.md")
    except RuntimeError:
        return  # vault not configured
    if not os.path.isfile(path):
        return
    if (read_frontmatter(path).get("email") or "").strip():
        return  # already has an address -- don't overwrite
    _update_frontmatter(path, email=email)


def reset_generate_social_trigger(note_path: str):
    """Unchecks "generate_social_media" after generation runs -- same
    momentary "do it now" contract as notion_sync.reset_generate_social_trigger."""
    _update_frontmatter(note_path, generate_social_media=False)
