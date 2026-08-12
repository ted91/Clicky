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


def detect_vault_path() -> str:
    """Best-effort auto-detection of an existing Obsidian vault, so a new
    user isn't asked to type a filesystem path by hand during setup.
    Obsidian itself creates a `.obsidian` marker folder in every vault's
    root -- that's the one cheap, reliable signal a directory is a real
    vault (vs. just any folder). Scans a short list of common locations,
    one level deep, and returns the first match, or "" if none found (the
    field stays a normal, editable text input either way -- this only
    fills in a default for the common case, never overrides a value the
    user already set). Not exhaustive by design: a vault kept somewhere
    unusual, or multiple vaults, still needs manual entry."""
    home = os.path.expanduser("~")
    candidates = [
        os.path.join(home, "Documents"),
        os.path.join(home, "Obsidian"),
        os.path.join(home, "Documents", "Obsidian"),
        os.path.join(home, "Library", "Mobile Documents", "iCloud~md~obsidian", "Documents"),
    ]
    for base in candidates:
        if not os.path.isdir(base):
            continue
        if os.path.isdir(os.path.join(base, ".obsidian")):
            return base
        try:
            for entry in sorted(os.listdir(base)):
                candidate = os.path.join(base, entry)
                if os.path.isdir(candidate) and os.path.isdir(os.path.join(candidate, ".obsidian")):
                    return candidate
        except OSError:
            continue
    return ""


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
    # Same contextual short title the Notion page uses (see
    # notion_sync._recording_title) rather than the whole summary
    # paragraph, so the two destinations agree on what this note is called.
    # The date prefix stays here -- unlike Notion, a filename has no
    # separate Date property to carry it.
    import notion_sync
    slug = _slugify(notion_sync._recording_title(record))
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

    organizations = summary.get("organizations") or []
    if organizations:
        from providers.base import format_organization
        lines += ["## Organizations", ""]
        lines += [f"- {format_organization(o)}" for o in organizations]
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
    """Topics/Intents/Deepgram Summary as real frontmatter fields -- same
    fields notion_sync._insight_properties adds as Notion properties, kept
    consistent across both destinations. Empty dict if Deepgram wasn't the
    STT provider or returned no insights."""
    insights = record.get("deepgram_insights") or {}
    fm = {}
    if insights.get("topics"):
        fm["topics"] = insights["topics"][:20]
    if insights.get("intents"):
        fm["intents"] = insights["intents"][:20]
    if insights.get("summary"):
        fm["deepgram_summary"] = insights["summary"]
    return fm


def push_recording(record: dict, existing_path: str = None) -> str:
    """Writes this recording as a markdown file into the vault root.
    Journal-classified recordings skip this entirely (see push_journal) --
    same dedup as notion_sync (a journal entry's only home is the Journal
    folder, not also here). "generate_social_media" frontmatter is the
    trigger poller.check_social_post_generation_triggers_once() polls, same
    property as notion_sync.GENERATE_SOCIAL_PROPERTY. Returns the note's
    path -- caller (poller.distribute_once) persists it via
    storage.set_obsidian_note_path() so Tasks/People/Calendar/Publications
    can wiki-link back to it and so the trigger can be polled.

    existing_path: when set (a prior push already wrote this recording,
    but the record's obsidian_synced flag wasn't persisted -- e.g. a crash
    right after writing), reuse this exact path/filename instead of
    re-deriving one from the current title. Otherwise a title that
    changed since the first write (e.g. after a speaker rename) would
    produce a second, differently-named file instead of overwriting the
    original."""
    if (record.get("summary") or {}).get("type") == "journal":
        return None
    vault_path = _vault_path()
    if existing_path:
        file_path = existing_path
        filename = os.path.basename(existing_path)
    else:
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


def note_exists(path: str) -> bool:
    """Whether a note this module previously wrote is still in the vault.

    Obsidian's counterpart to notion_sync.find_page_by_recording_id: the
    check that turns a stored path into a verified one. A vault is an
    ordinary folder the user edits and deletes files in, so a recorded
    path is a claim about the past, not a guarantee about the present."""
    return bool(path) and os.path.isfile(path)


def missing_notes(records: list) -> list:
    """Returns [(content_hash, destination)] for every record marked synced
    to Obsidian whose note file is no longer in the vault.

    Sync flags were write-once: nothing re-checked them, so a note deleted
    by hand -- or one whose write failed after the flag was set -- was gone
    for good while the dashboard still reported it synced. Live-confirmed:
    a meeting's Task, Calendar and People notes were all present but its
    main note was absent, obsidian_synced=True, and no code path would
    ever have rewritten it.

    Only reports a record that HAS a recorded path which is now missing --
    a record that never had one (a journal entry lives under Journal/, so
    it has no root note by design) is not missing, it's just shaped
    differently. Caller clears the flags so distribute_once rewrites them;
    this function itself is read-only."""
    if not _vault_path_or_none():
        return []
    missing = []
    for record in records:
        if record.get("status") != "done" or record.get("merged_into"):
            continue
        for dest, key in (("obsidian", "obsidian_note_path"),
                          ("obsidian_journal", "obsidian_journal_note_path")):
            path = record.get(key)
            if record.get(f"{dest}_synced") and path and not note_exists(path):
                missing.append((record["content_hash"], dest))
    return missing


def _vault_path_or_none():
    """_vault_path() raises when Obsidian isn't configured; callers that
    simply want to skip when there's no vault use this instead."""
    try:
        path = _vault_path()
    except Exception:
        return None
    return path if os.path.isdir(path) else None


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


def refresh_task_notes(record: dict) -> int:
    """Rewrites the heading/owner of Task notes already written for this
    recording, so a later correction reaches them -- the Obsidian half of
    notion_sync.refresh_task_titles.

    Re-running push_tasks() is not equivalent: it derives each filename
    from the recording's current title, so once that title changes (which
    a correction or re-summarize does) it writes a NEW file and leaves the
    stale one behind. This updates the existing files in place.

    Paths come from task_email_links where recorded (that's the only place
    an Obsidian task path is persisted today); otherwise it falls back to
    the deterministic current-title path, which is right for a note whose
    title hasn't changed. Missing files are skipped, not created -- this
    refreshes what exists rather than resurrecting deleted notes."""
    items = (record.get("summary") or {}).get("action_items") or []
    if not items:
        return 0
    paths_by_index = {
        l["index"]: l["task_note_path"]
        for l in (record.get("task_email_links") or [])
        if l.get("index") and l.get("task_note_path")
    }
    updated = 0
    for i, item in enumerate(items, start=1):
        path = paths_by_index.get(i) or task_note_path(record, i)
        if not path or not os.path.isfile(path):
            continue
        try:
            frontmatter = read_frontmatter(path)
            if item.get("owner"):
                frontmatter["owner"] = item["owner"]
            if item.get("due_date"):
                frontmatter["due_date"] = item["due_date"]
            if item.get("owner"):
                frontmatter["related_person"] = _wiki_link(item["owner"])
            with open(path, "r", encoding="utf-8") as f:
                existing = f.read()
            # Replace only the "# ..." heading line; everything below it
            # (the email draft, hints, anything the user added) is left
            # untouched -- this is a correction, not a regeneration.
            body = existing.split("---", 2)[-1].lstrip("\n") if existing.startswith("---") else existing
            lines = body.split("\n")
            for n, line in enumerate(lines):
                if line.startswith("# "):
                    lines[n] = f"# {item.get('text', '')}"
                    break
            _write_note(os.path.dirname(path), os.path.basename(path), frontmatter, "\n".join(lines))
            updated += 1
        except Exception as e:
            log.warning("could not refresh Obsidian task note %s (non-fatal): %s", path, e)
    return updated


def task_note_path(record: dict, item_index: int) -> str:
    """Reconstructs the deterministic path push_tasks() writes each action
    item's note to (Tasks/{recording title} - item{1-based index}.md) --
    used by the email-watch feature (poller.check_email_watches_once) to
    mirror watch_query/watch_triggered onto the note's frontmatter without
    needing a separate index->path mapping stored anywhere (push_tasks
    already only persists such a mapping for email-type items -- see
    push_tasks's own "links" return value -- and a watch can be set on
    any action item, not just email ones)."""
    filename = f"{_note_title(record)} - item{item_index + 1}.md"
    return os.path.join(_vault_subfolder("Tasks"), filename)


def set_task_done(record: dict, item_index: int, done: bool):
    """Obsidian counterpart of notion_sync.set_task_done -- mirrors the
    dashboard's action-item checkbox onto the Task note.

    Writes BOTH a "done" boolean and a "status" string: `done` is what
    Obsidian's own checkbox/Dataview queries key off, `status` matches the
    Notion Task's Status property wording so the two destinations read the
    same when compared side by side. item_index is 1-based, matching
    push_tasks()'s enumerate(..., start=1) and Notion's task_status_links.

    Best-effort like the Notion side: the dashboard toggle already
    succeeded locally, so a missing note must not raise."""
    path = _task_note_path_for(record, item_index)
    if not note_exists(path):
        return False
    _update_frontmatter(path, done=done, status="Done" if done else "Not started")
    return True


def read_task_done(record: dict, item_index: int):
    """Reads the Task note's "done" frontmatter back, so ticking the box
    in Obsidian propagates INTO Clicky -- the mirror of
    poller.check_notion_jarvis_done_once's poll-back for Notion. Returns
    None when the note doesn't exist or has no explicit value, which the
    caller must distinguish from a real False."""
    path = _task_note_path_for(record, item_index)
    if not note_exists(path):
        return None
    value = read_frontmatter(path).get("done")
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "1")
    return None


def _task_note_path_for(record: dict, item_index: int) -> str:
    """The Task note path for a 1-based action-item index, preferring a
    path actually recorded at creation time over one recomputed from the
    current title -- the recording's title changes (a rename, a
    re-summarize), and task_note_path() derives from whatever it is NOW,
    which stops matching the file that was written THEN."""
    for link in (record.get("task_email_links") or []):
        if link.get("index") == item_index and link.get("task_note_path"):
            return link["task_note_path"]
    return task_note_path(record, item_index)


def set_task_watch(record: dict, item_index: int, watch_query: str):
    """Mirrors an action item's email watch onto its Obsidian Tasks note
    frontmatter -- no-ops (via _update_frontmatter's own guard) if the
    note doesn't exist, e.g. Obsidian wasn't configured when this
    recording was first processed."""
    path = task_note_path(record, item_index)
    if os.path.isfile(path):
        _update_frontmatter(path, watch_for=watch_query, watch_alert=False)


def set_task_watch_alert(record: dict, item_index: int, matches: list):
    """Flags a Tasks note's watch as triggered -- matches is
    [{"from", "subject"}] from apple_mail.search_messages."""
    path = task_note_path(record, item_index)
    if os.path.isfile(path):
        summary = "; ".join(f"{m.get('from', '')}: {m.get('subject', '')}" for m in matches[:3])
        _update_frontmatter(path, watch_alert=True, watch_alert_summary=summary)


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


def person_note_path(name: str) -> str:
    """The deterministic People/ path for a person's note. Same slug
    push_people() writes to, factored out so contact lookup and dedup
    agree with it rather than each recomputing the rule."""
    if not name or not name.strip():
        return None
    return os.path.join(_vault_subfolder("People"), f"{_slugify(name.strip())}.md")


def get_person_note(email: str, name: str) -> str:
    """Obsidian counterpart of notion_sync.get_person_note -- returns the
    person's "note" (their role/relationship) for meeting-prep enrichment.

    Email match is preferred over name, same precedence as Notion's, since
    an address identifies a human and a name doesn't. Returns "" on any
    miss -- this enriches a prep note and must never break one."""
    try:
        if email and email.strip():
            match = find_person_by_email(email)
            if match:
                return (read_frontmatter(match).get("note") or "").strip()
        path = person_note_path(name)
        if note_exists(path):
            return (read_frontmatter(path).get("note") or "").strip()
    except Exception as e:
        log.debug("Obsidian person-note lookup failed for %r (non-fatal): %s", name or email, e)
    return ""


def find_person_by_email(email: str) -> str:
    """Path of the People/ note whose frontmatter carries this email, or
    None. Mirrors notion_sync._find_person_by_email: the email is the
    identity key, the filename only a slug of whatever name was known
    first."""
    needle = (email or "").strip().lower()
    if not needle:
        return None
    people_dir = _vault_subfolder("People")
    for filename in sorted(os.listdir(people_dir)):
        if not filename.endswith(".md"):
            continue
        path = os.path.join(people_dir, filename)
        try:
            if (read_frontmatter(path).get("email") or "").strip().lower() == needle:
                return path
        except Exception:
            continue
    return None


def set_person_contact_by_name(name: str, email: str = None, linkedin: str = None) -> str:
    """Obsidian counterpart of notion_sync.set_person_contact_by_name --
    writes manually-entered contact details onto a person's People/ note,
    creating a minimal note when none exists yet (same as the Notion side,
    so the dashboard's "+ contact" behaves identically for both).

    Only the fields actually supplied are written; passing None for one
    leaves the existing value alone rather than blanking it."""
    if not name or not name.strip():
        return None
    path = person_note_path(name)
    if not note_exists(path):
        _write_note(_vault_subfolder("People"), os.path.basename(path),
                    {"name": name.strip(), "email": "", "linkedin": "", "note": ""},
                    f"# {name.strip()}\n")
    fields = {}
    if email is not None:
        fields["email"] = email.strip()
    if linkedin is not None:
        fields["linkedin"] = linkedin.strip()
    if fields:
        _update_frontmatter(path, **fields)
    return path


def find_duplicate_people() -> list:
    """Obsidian counterpart of notion_sync.find_duplicate_people: People/
    notes that look like the same human.

    Groups by email, by LinkedIn, AND by normalized name. The name key
    matters as much as the others here for the same reason it does on the
    Notion side -- two notes can describe one person with no contact
    details on either -- with an extra Obsidian-specific wrinkle: the
    filename is a slug of the name, so a same-name duplicate can only
    exist as a *differently slugged* variant ("sanjit.md" vs
    "sanchit.md"), which is exactly the mis-transcribed-name case.

    Returns [{"key": ..., "pages": [{"id": path, "name", "note", "email"}]}]
    -- the same shape the dashboard already renders for Notion, with the
    note's path as its id, so one UI serves both. Read-only: merging is a
    separate, explicit action (see merge_person_notes)."""
    if not _vault_path_or_none():
        return []
    people_dir = _vault_subfolder("People")
    by_email, by_linkedin, by_name = {}, {}, {}
    for filename in sorted(os.listdir(people_dir)):
        if not filename.endswith(".md"):
            continue
        path = os.path.join(people_dir, filename)
        try:
            fm = read_frontmatter(path)
        except Exception:
            continue
        entry = {"id": path, "name": fm.get("name") or filename[:-3],
                 "note": fm.get("note") or "", "email": fm.get("email") or ""}
        email = (fm.get("email") or "").strip().lower()
        linkedin = (fm.get("linkedin") or "").strip().rstrip("/").lower()
        name = (fm.get("name") or filename[:-3]).strip().lower()
        if email:
            by_email.setdefault(email, []).append(entry)
        if linkedin:
            by_linkedin.setdefault(linkedin, []).append(entry)
        if name:
            by_name.setdefault(name, []).append(entry)

    groups, seen = [], set()
    for key, entries in list(by_email.items()) + list(by_linkedin.items()) + list(by_name.items()):
        if len(entries) < 2:
            continue
        ids = tuple(sorted(e["id"] for e in entries))
        if ids in seen:
            continue
        seen.add(ids)
        groups.append({"key": key, "pages": entries})
    return groups


def merge_person_notes(keeper_path: str, loser_path: str):
    """Explicit, user-triggered merge of two People/ notes -- Obsidian's
    counterpart to notion_sync.merge_person_pages, and deliberately never
    automatic for the same reason.

    Appends the loser's body onto the keeper under a labeled heading,
    backfills any contact detail the keeper is missing, then deletes the
    loser. Wiki-links elsewhere in the vault that pointed at the loser are
    rewritten to the keeper, so a merge doesn't leave dangling links --
    the Obsidian-specific half of this job, since Notion relations
    re-point by id and markdown links don't."""
    if not note_exists(keeper_path) or not note_exists(loser_path):
        raise RuntimeError("both notes must exist to merge them")
    if os.path.abspath(keeper_path) == os.path.abspath(loser_path):
        raise RuntimeError("cannot merge a note into itself")

    keeper_fm, loser_fm = read_frontmatter(keeper_path), read_frontmatter(loser_path)
    with open(loser_path, "r", encoding="utf-8") as f:
        loser_text = f.read()
    m = _FRONTMATTER_RE.match(loser_text)
    loser_body = (m.group(2) if m else loser_text).strip()

    # Contact details survive the merge: whichever note had them wins,
    # rather than the merge silently discarding the only copy.
    fields = {}
    for key in ("email", "linkedin", "note"):
        if not (keeper_fm.get(key) or "").strip() and (loser_fm.get(key) or "").strip():
            fields[key] = loser_fm[key]
    if fields:
        _update_frontmatter(keeper_path, **fields)

    loser_name = loser_fm.get("name") or os.path.basename(loser_path)[:-3]
    if loser_body:
        append_body(keeper_path, f"\n## Merged from duplicate note ({loser_name})\n\n{loser_body}\n")

    keeper_title = os.path.basename(keeper_path)[:-3]
    loser_title = os.path.basename(loser_path)[:-3]
    _repoint_wiki_links(loser_title, keeper_title)
    os.remove(loser_path)
    log.info("merged Obsidian People note %s into %s", loser_title, keeper_title)


def _repoint_wiki_links(old_title: str, new_title: str):
    """Rewrites [[old]] wiki-links across the vault to [[new]]. Without
    this a merged-away note leaves broken links in every recording note
    that mentioned that person."""
    vault = _vault_path_or_none()
    if not vault or old_title == new_title:
        return
    for root, _dirs, files in os.walk(vault):
        for filename in files:
            if not filename.endswith(".md"):
                continue
            path = os.path.join(root, filename)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    text = f.read()
                if f"[[{old_title}]]" not in text:
                    continue
                with open(path, "w", encoding="utf-8") as f:
                    f.write(text.replace(f"[[{old_title}]]", f"[[{new_title}]]"))
            except Exception as e:
                log.debug("could not re-point wiki links in %s (non-fatal): %s", path, e)


NEW_ACTION_ITEM_FIELD = "new_action_item"
ADD_ACTION_ITEM_FIELD = "add_action_item"


def read_action_item_trigger(note_path: str) -> str:
    """Obsidian counterpart of notion_sync.read_action_item_trigger:
    returns the text typed into the note's "new_action_item" frontmatter
    when "add_action_item" is set true, else "".

    Two fields rather than one so an unfinished sentence doesn't fire the
    moment it's typed -- the boolean is the deliberate "do it now", exactly
    like Notion's checkbox beside its text property."""
    if not note_exists(note_path):
        return ""
    try:
        fm = read_frontmatter(note_path)
    except Exception:
        return ""
    trigger = fm.get(ADD_ACTION_ITEM_FIELD)
    if isinstance(trigger, str):
        trigger = trigger.strip().lower() in ("true", "yes", "1")
    if not trigger:
        return ""
    return (fm.get(NEW_ACTION_ITEM_FIELD) or "").strip()


def reset_action_item_trigger(note_path: str):
    """Clears both trigger fields once the task has been created, so the
    next poll doesn't create it again -- the reset half of the same
    ensure/poll/reset shape notion_sync uses."""
    if note_exists(note_path):
        _update_frontmatter(note_path, **{NEW_ACTION_ITEM_FIELD: "", ADD_ACTION_ITEM_FIELD: False})


def ensure_action_item_trigger_fields(note_path: str):
    """Adds the two trigger fields to a note that predates them, so the
    user has something to fill in. Obsidian has no schema to migrate (the
    Notion equivalent patches the database), but a field that isn't
    present in the frontmatter is a field nobody knows exists."""
    if not note_exists(note_path):
        return
    try:
        fm = read_frontmatter(note_path)
    except Exception:
        return
    missing = {}
    if NEW_ACTION_ITEM_FIELD not in fm:
        missing[NEW_ACTION_ITEM_FIELD] = ""
    if ADD_ACTION_ITEM_FIELD not in fm:
        missing[ADD_ACTION_ITEM_FIELD] = False
    if missing:
        _update_frontmatter(note_path, **missing)


def push_single_task(text: str, record: dict, item_index: int) -> str:
    """Writes one new Task note for an action item added from inside the
    vault -- Obsidian's counterpart to notion_sync.push_single_task.
    Returns the note's path so the caller can record it."""
    dir_path = _vault_subfolder("Tasks")
    filename = f"{_note_title(record)} - item{item_index}.md"
    frontmatter = {"related_note": _wiki_link(_note_title(record)),
                   "done": False, "status": "Not started", "source": "added in Obsidian"}
    _write_note(dir_path, filename, frontmatter,
                f"# {text}\n\nFrom recording: {record.get('name', '')}\n")
    path = os.path.join(dir_path, filename)
    log.info("created Obsidian Task note from vault-added action item: %s", filename)
    return path


def push_command(record: dict, jarvis_result: dict) -> str:
    """Writes one Jarvis voice command into Jarvis/ -- the Obsidian
    counterpart of notion_sync.push_command, which had no Obsidian path at
    all: commands reached Notion and the dashboard but never the vault.

    Its own folder rather than the vault root for the same reason Notion
    gives it its own database: a command isn't a recording in the Notes
    sense (no speakers, summary or action items) and shouldn't be mixed in
    with them. Best-effort, like every other destination push."""
    action_type = jarvis_result.get("action_type") or "unknown"
    transcript = (jarvis_result.get("transcript") or "").strip()
    spoken = (jarvis_result.get("spoken") or "").strip()
    title = f"{action_type} — {transcript[:100]}" if transcript else action_type

    dir_path = _vault_subfolder("Jarvis")
    created_at = record.get("created_at") or ""
    filename = f"{created_at[:10]} {_slugify(title)}.md"
    frontmatter = {
        "created": created_at,
        "action_type": action_type,
        "ok": bool(jarvis_result.get("ok")),
        "done": jarvis_result.get("user_status") == "done",
        "source_recording": record.get("name", ""),
    }
    body = "\n".join([f"# {title}", "", "## Heard", "", transcript or "(nothing transcribed)",
                      "", "## Replied", "", spoken or "(no reply)"])
    _write_note(dir_path, filename, frontmatter, body)
    path = os.path.join(dir_path, filename)
    log.info("pushed Jarvis command %s to Obsidian", record["name"])
    return path


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


def write_agent_note(title: str, body: str, record: dict) -> bool:
    """Files an approved agent deliverable (a research briefing or a
    drafted document -- see poller.approve_agent_result) into Agent/.

    Its own folder for the same reason Jarvis/ has one: this isn't a
    recording, it's something the agent produced *about* a recording, and
    mixing it into Notes/ would make the vault's recording list stop
    meaning "things I recorded". Links back to the source recording's note
    so the provenance is one click away. Returns False rather than raising
    if the vault isn't configured -- the caller treats destinations as
    individually optional."""
    try:
        dir_path = _vault_subfolder("Agent")
    except RuntimeError:
        return False
    created_at = record.get("created_at") or ""
    filename = f"{created_at[:10]} {_slugify(title)}.md"
    frontmatter = {
        "created": created_at,
        "type": "agent-output",
        "approved": True,
        "source_recording": record.get("name", ""),
    }
    source_link = f"[[{_note_title(record)}]]"
    full_body = "\n".join([
        f"# {title}", "",
        body, "",
        "---", "",
        f"Produced by the agent from {source_link}, reviewed and approved by you.",
    ])
    _write_note(dir_path, filename, frontmatter, full_body)
    log.info("wrote agent note %r to Obsidian", title)
    return True
