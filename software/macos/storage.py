"""Local JSON-file store of recordings — no external DB needed for a
personal voice-memo device. Two-phase by design: a recording becomes
visible on the dashboard as soon as its audio is downloaded from the
device (status="pending"), independent of whether transcription/
summarization has happened yet or ever succeeds (status="done"/"failed").
This matters because BLE fetch and LLM processing are two very different
failure domains — if the LLM step fails (bad API key, network hiccup),
the audio is already safely on disk and won't be re-fetched from the
device on every poll cycle; only processing gets retried.

Keyed by (name, content_hash) since the PSRAM fallback recording is always
named "ram_recording.wav" and would otherwise look like a duplicate of
itself every time it's overwritten with new audio.
"""
import json
import os
import threading
from datetime import datetime, timedelta, timezone

import config
import paths

_lock = threading.Lock()
AUDIO_DIR = paths.AUDIO_DIR

# Deliberately never-deleted-from-device names, kept independent of the
# main records list so the tombstone survives even though the record
# itself is gone (see delete_recording()'s docstring for the bug this
# fixes: without this, a deleted SD-sourced recording -- which the device
# keeps forever by design -- looked "unknown" again on the very next poll
# and silently re-downloaded itself).
#
# Stored as {name: size}, not just a bare name, because the device's
# boot-time index can reuse a deleted name for genuinely new, unrelated
# audio (see delete_recording_from_device()'s comment) -- a name-only
# tombstone would then permanently block that new content from ever
# syncing just because it landed on a previously-deleted name. Keying by
# (name, size) together means only the exact deleted file is suppressed;
# a same-name recording of a different size is recognized as new.
_TOMBSTONE_PATH = os.path.join(paths.APP_DATA_DIR, "deleted_device_names.json")


def _load_tombstones() -> dict:
    try:
        with open(_TOMBSTONE_PATH) as f:
            raw = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    if isinstance(raw, list):
        # Pre-existing file from before tombstones tracked size -- size
        # unknown, so fall back to matching on name alone for these until
        # they're naturally superseded by a real (name, size) tombstone.
        return {name: None for name in raw}
    return raw


def _save_tombstones(tombstones: dict):
    with open(_TOMBSTONE_PATH, "w") as f:
        json.dump(tombstones, f, indent=2, sort_keys=True)


def _add_tombstone(name: str, size: int):
    tombstones = _load_tombstones()
    tombstones[name] = size
    _save_tombstones(tombstones)


def _remove_tombstone(name: str):
    tombstones = _load_tombstones()
    if name in tombstones:
        del tombstones[name]
        _save_tombstones(tombstones)


def is_tombstoned(name: str, size: int) -> bool:
    """True if the user explicitly deleted this exact (name, size)
    recording from the dashboard -- poller.py's sync_once() must never
    re-download it again, even though the device itself may still be
    offering it (SD recordings are a permanent on-device archive by
    design; only an explicit delete-from-device action, see
    delete_recording_from_device(), removes it there too). A same-named
    recording of a different size is new content, not this tombstone."""
    tombstones = _load_tombstones()
    if name not in tombstones:
        return False
    tombstoned_size = tombstones[name]
    return tombstoned_size is None or tombstoned_size == size


def _load():
    if not os.path.exists(config.STORAGE_PATH):
        return []
    with open(config.STORAGE_PATH, "r") as f:
        records = json.load(f)
    for r in records:
        r.setdefault("speaker_names", {})  # backfill for records written before this field existed
        r.setdefault("meeting", None)      # calendar metadata for meeting recordings (meeting_recorder.py)
        r.setdefault("drafts", None)       # post-meeting follow-up drafts pending user approval (poller.py)
        r.setdefault("task_email_links", [])  # notion_sync.push_tasks()'s email-item Task/People page ids (poller.check_notion_email_approvals_once)
        r.setdefault("official_transcript_status", None)  # None | "applied" | "gave_up" -- see poller.check_official_meeting_transcripts_once
        r.setdefault("official_transcript_check_until", None)  # ISO timestamp bound on how long to keep polling for one
        r.setdefault("social_posts", {})  # {platform: {status, body, title, notion_page_id, scheduled_at, published_at, url, error}}
        r.setdefault("notion_journal_page_id", None)
        r.setdefault("notion_publication_page_id", None)
        r.setdefault("obsidian_note_path", None)
        r.setdefault("obsidian_tasks_synced", False)
        r.setdefault("obsidian_people_synced", False)
        r.setdefault("obsidian_events_synced", False)
        r.setdefault("obsidian_journal_synced", False)
        r.setdefault("obsidian_journal_note_path", None)
        r.setdefault("obsidian_publications_synced", False)
        r.setdefault("obsidian_publication_note_path", None)
    return records


def _save(records):
    tmp_path = config.STORAGE_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(records, f, indent=2)
    os.replace(tmp_path, config.STORAGE_PATH)


def _find(records, content_hash):
    for r in records:
        if r["content_hash"] == content_hash:
            return r
    return None


def is_known_by_size(name: str, size: int) -> bool:
    """Cheap pre-download check using the (name, size) the device reports
    in its /list — lets poller.py skip re-downloading a recording it's
    already synced without needing to fetch bytes just to hash them. Not
    cryptographically precise (same name+size could theoretically differ in
    content), but for this device's semantics — SD files are written once
    and never modified, and the RAM fallback recording's size changes with
    any new recording of a different length — a same-size "collision" would
    only mean skipping a re-record of literally identical duration, which
    isn't a real-world concern here.

    Also true for an explicitly deleted-from-dashboard (name, size) pair
    (see is_tombstoned()) -- once the user deletes a recording, that exact
    file must never come back just because its active record is gone. The
    device's boot-time index can still reuse the name for unrelated new
    audio of a different size, though, so the tombstone check is scoped to
    the deleted file's own size rather than the name alone.
    """
    if is_tombstoned(name, size):
        return True
    with _lock:
        records = _load()
    return any(r["name"] == name and r.get("size") == size for r in records)


def is_known_content_hash(content_hash: str) -> bool:
    """Post-download dedup by actual content, not just (name, size) --
    needed for the RAM fallback recording specifically: its is_known_by_size
    check is skipped (see poller.py's sync_once) so a genuinely new
    recording that happens to match a past one's byte size isn't silently
    stranded, but that means a re-download of truly identical bytes (e.g.
    a retry after the device's confirm-delete previously failed, so it's
    still offering the same content) needs this content-level check instead
    to avoid creating a duplicate record."""
    with _lock:
        records = _load()
    return any(r.get("content_hash") == content_hash for r in records)


def add_pending(name: str, size: int, content_hash: str, wav_bytes: bytes) -> dict:
    """Call as soon as audio is downloaded from the device — makes it show
    up on the dashboard immediately, before any transcription is attempted.

    Denoises (see noise_reduction.denoise_wav -- RNNoise-based background
    noise suppression) then peak-normalizes (see audio_utils.normalize_wav)
    the audio before writing it to disk, in that order so normalization
    doesn't amplify a noise floor the denoiser is about to remove -- both
    for STT accuracy and for playback in the dashboard. content_hash is
    computed by the caller from the *original* bytes (used purely as a
    stable dedup/identifier key, not a content-integrity check of the
    stored file), so it's left as-is even though the file written here may
    differ from it.
    """
    import audio_utils
    import noise_reduction
    wav_bytes = noise_reduction.denoise_wav(wav_bytes)
    wav_bytes = audio_utils.normalize_wav(wav_bytes)

    os.makedirs(AUDIO_DIR, exist_ok=True)
    wav_path = os.path.join(AUDIO_DIR, f"{content_hash}.wav")
    with open(wav_path, "wb") as f:
        f.write(wav_bytes)

    record = {
        "id": f"{name}-{content_hash[:8]}",
        "name": name,
        "size": size,
        "content_hash": content_hash,
        "wav_path": wav_path,
        # "command" for a Jarvis voice command (cmd_*.wav, see recorder.cpp);
        # routed to jarvis.process_command instead of the memo summarize()/
        # Notion/Obsidian pipeline (see poller.process_once).
        "kind": "command" if name.startswith("cmd_") else "memo",
        "jarvis_result": None,  # set by mark_jarvis_processed() for kind=="command" records
        "status": "pending",  # -> "done" or "failed" once processing runs
        "transcript": None,
        "segments": None,  # speaker-diarized [{speaker_id,text,start,end}], if the STT provider supports it
        "speaker_names": {},  # user-assigned {speaker_id: display_name}, overrides "Speaker X" everywhere
        "summary": None,
        "deepgram_insights": None,  # populated when STT_PROVIDER=deepgram; see deepgram_provider._parse_insights
        "stt_provider": None,
        "llm_provider": None,
        "error": None,
        "notion_synced": False,
        "notion_page_id": None,  # set once push_recording() succeeds; Tasks/People/Calendar relate back to this
        "notion_tasks_synced": False,  # separate: one page per action item in a Tasks database, see notion_sync.py
        "notion_people_synced": False,  # separate: one page per mentioned person in a People database
        "notion_events_synced": False,  # separate: one page per calendar event in a Calendar database
        "notion_journal_synced": False,  # separate: only for summary.type == "journal", see notion_sync.push_journal
        "notion_journal_page_id": None,  # set once push_journal() succeeds -- needed to poll that page's own "Generate Social Media" checkbox
        "obsidian_synced": False,
        "obsidian_note_path": None,  # set once obsidian_sync.push_recording() succeeds; Tasks/People/Calendar/Publications wiki-link back to this
        "obsidian_tasks_synced": False,
        "obsidian_people_synced": False,
        "obsidian_events_synced": False,
        "obsidian_journal_synced": False,
        "obsidian_journal_note_path": None,  # set once obsidian_sync.push_journal() succeeds -- needed to poll that note's own "generate_social_media" frontmatter checkbox
        "obsidian_publications_synced": False,
        "obsidian_publication_note_path": None,  # set once obsidian_sync.push_social_posts() succeeds -- the ONE Publications note for this recording (sectioned per platform)
        "social_posts": {},  # {platform: {...}} -- see set_social_posts/update_social_post
        "notion_publication_page_id": None,  # set once push_social_posts() succeeds -- the ONE Publications page for this recording (sectioned per platform), see notion_sync.push_social_posts
        "created_at": datetime.now(timezone.utc).isoformat(),
        "merged_from": [],  # content_hashes absorbed into this record, see merge_recordings()
        "merged_into": None,  # set on a record absorbed into another -- excluded from get_undistributed()/dashboard
        "merged_wav_paths": [],  # extra wav_paths absorbed in (this record's own wav_path stays first), for playback continuity
        "pre_merge_transcript": None,  # this record's transcript exactly as it was before the most recent merge, so unmerge_recording() can restore it exactly
        "merge_checked": False,  # set True once poller.merge_continuations_once() has made its one decision for this record -- same one-shot-then-flag idempotency as the *_synced fields, so a "not a continuation" verdict isn't re-asked (and re-billed) every poll cycle
    }
    with _lock:
        records = _load()
        records.append(record)
        _save(records)
    return record


def get_unprocessed():
    """Records still needing transcription/summarization — either never
    attempted (pending) or a previous attempt failed (failed), so a poll
    cycle can retry without touching the device at all.

    Jarvis voice commands (kind=="command") are sorted first -- a spoken
    command is a live, waited-on interaction (the user is standing there
    expecting a spoken reply), unlike a memo/meeting recording that gets
    processed in the background with no one watching a clock. Without this,
    a command queued behind a large, slow-to-transcribe memo recording (or
    several) sits waiting its turn with no reason to, which is exactly the
    "most time goes in sync/transcribing before acting" latency reported --
    this fixes the queuing order, not the per-file transcription time
    itself (see poller.py's kind=="command" branch for that side)."""
    with _lock:
        records = _load()
    unprocessed = [r for r in records if r["status"] in ("pending", "failed")]
    unprocessed.sort(key=lambda r: r.get("kind") != "command")
    return unprocessed


def mark_processed(content_hash: str, transcript: str, segments,
                    summary: dict, stt_provider: str, llm_provider: str,
                    deepgram_insights: dict = None):
    with _lock:
        records = _load()
        record = _find(records, content_hash)
        if record is None:
            return
        record["status"] = "done"
        record["transcript"] = transcript
        record["segments"] = segments
        record["summary"] = summary
        record["deepgram_insights"] = deepgram_insights
        record["stt_provider"] = stt_provider
        record["llm_provider"] = llm_provider
        record["error"] = None
        _save(records)


def merge_recordings(keeper_hash: str, loser_hash: str, merged_transcript: str,
                      merged_summary: dict, gap_seconds: int):
    """Absorbs `loser_hash` into `keeper_hash` -- see
    poller.merge_continuations_once/conversation_merge.py, which decides
    WHETHER two recordings should merge (gap + type + LLM continuity
    check); this function only does the mechanical merge once that
    decision is made. Mirrors notion_sync.merge_person_pages's keeper/
    loser shape: the loser's row stays in storage (audit trail, and so
    unmerge_recording() has something to restore) but is excluded from
    every destination's push queue and the dashboard via merged_into.

    `merged_transcript`/`merged_summary` are supplied by the caller
    (poller.py re-runs summarize() on the combined transcript, the same
    call process_once() already makes) rather than computed here, since
    storage.py has no business calling an LLM. The keeper's pre-merge
    transcript is saved so a later unmerge_recording() can restore it
    exactly rather than trying to reconstruct it by splitting the merged
    text back apart."""
    with _lock:
        records = _load()
        keeper = _find(records, keeper_hash)
        loser = _find(records, loser_hash)
        if keeper is None or loser is None:
            return False
        keeper["pre_merge_transcript"] = keeper["transcript"]
        keeper["transcript"] = merged_transcript
        keeper["summary"] = merged_summary
        keeper["merged_from"].append(loser_hash)
        keeper["merged_wav_paths"].append(loser["wav_path"])
        keeper["merged_wav_paths"].extend(loser.get("merged_wav_paths") or [])
        loser["merged_into"] = keeper_hash
        _save(records)
        return True


def mark_merge_checked(content_hash: str):
    """Flags a record as having had its one merge-continuation decision
    made (merged or not) -- see merge_checked's field docstring on
    add_pending. Called on BOTH outcomes so a "not a continuation" verdict
    isn't re-asked every poll cycle."""
    with _lock:
        records = _load()
        record = _find(records, content_hash)
        if record is None:
            return
        record["merge_checked"] = True
        _save(records)


def unmerge_recording(keeper_hash: str, loser_hash: str) -> bool:
    """Reverses merge_recordings(): restores the keeper's exact pre-merge
    transcript (see merge_recordings' docstring on why this is stored
    verbatim rather than reconstructed), and clears the loser's
    merged_into plus its *_synced flags so distribute_once() pushes it as
    its own document again next poll cycle. Does NOT re-run summarize()
    here -- the caller (app.py's unmerge route) is responsible for that,
    same division of responsibility as merge_recordings() above."""
    with _lock:
        records = _load()
        keeper = _find(records, keeper_hash)
        loser = _find(records, loser_hash)
        if keeper is None or loser is None or loser.get("merged_into") != keeper_hash:
            return False
        if keeper["pre_merge_transcript"] is not None:
            keeper["transcript"] = keeper["pre_merge_transcript"]
            keeper["pre_merge_transcript"] = None
        keeper["merged_from"] = [h for h in keeper["merged_from"] if h != loser_hash]
        keeper["merged_wav_paths"] = [p for p in keeper["merged_wav_paths"] if p != loser["wav_path"]]
        loser["merged_into"] = None
        for field in ("notion_synced", "obsidian_synced"):
            loser[field] = False
        _save(records)
        return True


def mark_jarvis_processed(content_hash: str, transcript: str, jarvis_result: dict, stt_provider: str):
    """Jarvis command recordings (kind=="command") skip the memo
    summarize()/Notion/Obsidian pipeline entirely -- see poller.process_once
    -- so they need their own terminal state instead of mark_processed's
    summary/llm_provider fields. jarvis_result is jarvis.process_command's
    return dict ({transcript, action_type, ok, spoken}), shown on the
    dashboard in place of a summary."""
    with _lock:
        records = _load()
        record = _find(records, content_hash)
        if record is None:
            return
        record["status"] = "done"
        record["transcript"] = transcript
        record["jarvis_result"] = jarvis_result
        record["stt_provider"] = stt_provider
        record["error"] = None
        _save(records)


def set_jarvis_user_status(content_hash: str, status: str) -> bool:
    """Sets the user's disposition on a processed Jarvis command --
    "pending"/"done"/"discarded" -- distinct from jarvis_result["ok"]
    (whether the action itself executed successfully). Mirrors
    update_draft()'s shape but writes into jarvis_result directly since a
    command has one result, not a list of drafts. Returns False if the
    recording doesn't exist or hasn't been processed by Jarvis yet."""
    with _lock:
        records = _load()
        record = _find(records, content_hash)
        if record is None or record.get("jarvis_result") is None:
            return False
        record["jarvis_result"]["user_status"] = status
        _save(records)
        return True


def apply_speaker_name_guesses(content_hash: str, guesses: dict):
    """Auto-fills speaker_names from the summarizer's self-identification
    guesses (see providers/base.py's "speaker_names" field) -- only for
    speaker_ids with no name yet, so it never overwrites a user's manual
    rename via set_speaker_name(). No-op if there's nothing to apply."""
    if not guesses:
        return
    with _lock:
        records = _load()
        record = _find(records, content_hash)
        if record is None:
            return
        names = record.setdefault("speaker_names", {})
        for speaker_id, name in guesses.items():
            if name and speaker_id not in names:
                names[speaker_id] = name
        _save(records)


def set_speaker_name(content_hash: str, speaker_id: str, name: str) -> bool:
    """Assigns a display name to a diarized speaker_id for one recording.
    An empty `name` clears the override, falling back to "Speaker X" again.
    Returns False if no such recording/speaker_id exists."""
    with _lock:
        records = _load()
        record = _find(records, content_hash)
        if record is None:
            return False
        if not any((s.get("speaker_id") == speaker_id) for s in (record.get("segments") or [])):
            return False
        names = record.setdefault("speaker_names", {})
        if name:
            names[speaker_id] = name
        else:
            names.pop(speaker_id, None)
        _save(records)
    return True


def mark_distributed(content_hash: str, destination: str):
    """destination: "notion", "notion_tasks", "notion_people",
    "notion_events", or "obsidian" -- marks that recording as already
    pushed there, so a later poll cycle doesn't duplicate it."""
    field = f"{destination}_synced"
    with _lock:
        records = _load()
        record = _find(records, content_hash)
        if record is None:
            return
        record[field] = True
        _save(records)


def update_summary(content_hash: str, summary: dict):
    """Replaces just the summary (used after a speaker rename triggers
    re-summarization -- see poller.resync_after_rename) without touching
    transcript/segments/providers/sync bookkeeping."""
    with _lock:
        records = _load()
        record = _find(records, content_hash)
        if record is None:
            return
        record["summary"] = summary
        _save(records)


def update_transcript(content_hash: str, transcript: str):
    """Replaces just the transcript text -- used when an official Google
    Meet transcript becomes available (see
    poller.check_official_meeting_transcripts_once) to upgrade from the
    locally-diarized one. Deliberately leaves `segments` (our own
    diarization) untouched -- _enforce_journal_rule's multi-speaker check
    still needs it, and it's still a reasonable record of the local
    recording even once the transcript text itself has been superseded."""
    with _lock:
        records = _load()
        record = _find(records, content_hash)
        if record is None:
            return
        record["transcript"] = transcript
        _save(records)


def reset_distribution_flags(content_hash: str, destinations: list):
    """Clears specific "{destination}_synced" flags (e.g. "notion_tasks",
    "notion_journal") so distribute_once() re-evaluates and re-pushes to
    just those destinations, without touching the Notes page itself
    (notion_page_id/notion_synced are left alone -- that page gets updated
    in place, not recreated). Used when re-summarization changes the
    journal/actionable classification or the shape of action_items/
    calendar_events, so downstream routing gets a chance to catch up
    instead of staying stuck on a stale decision made before the rename."""
    with _lock:
        records = _load()
        record = _find(records, content_hash)
        if record is None:
            return
        for dest in destinations:
            record[f"{dest}_synced"] = False
        _save(records)


def reset_notion_sync(content_hash: str):
    """Clears all Notion sync bookkeeping for a recording, so the next poll
    cycle re-pushes it as a brand-new page. Used when the previously-pushed
    page turns out to be gone (archived/deleted in Notion, e.g. by the
    user) -- without this, every subsequent poll cycle would keep hitting
    the same "page is archived" error forever trying to update a page that
    no longer usefully exists."""
    with _lock:
        records = _load()
        record = _find(records, content_hash)
        if record is None:
            return
        record["notion_page_id"] = None
        record["notion_synced"] = False
        record["notion_tasks_synced"] = False
        record["notion_people_synced"] = False
        record["notion_events_synced"] = False
        record["notion_journal_synced"] = False
        _save(records)


def set_notion_page_id(content_hash: str, page_id: str):
    """Records the Notes-database page id created by notion_sync.push_recording(),
    so later pushes (Tasks/People/Calendar) can relate back to it."""
    with _lock:
        records = _load()
        record = _find(records, content_hash)
        if record is None:
            return
        record["notion_page_id"] = page_id
        _save(records)


def set_notion_journal_page_id(content_hash: str, page_id: str):
    """Records the Journal-database page id created by
    notion_sync.push_journal() -- needed so poller.
    check_social_post_generation_triggers_once() can poll that page's own
    "Generate Social Media" checkbox for journal-type recordings (which no
    longer get a Notes page at all, see poller.distribute_once)."""
    with _lock:
        records = _load()
        record = _find(records, content_hash)
        if record is None:
            return
        record["notion_journal_page_id"] = page_id
        _save(records)


def set_notion_publication_page_id(content_hash: str, page_id: str):
    """Records the single Publications-database page id created by
    notion_sync.push_social_posts() for this recording -- one page covers
    every platform (sectioned in the body), so poller.
    check_publication_approvals_once()/check_social_publish_once() read/
    write this one id instead of a separate id per platform."""
    with _lock:
        records = _load()
        record = _find(records, content_hash)
        if record is None:
            return
        record["notion_publication_page_id"] = page_id
        _save(records)


def set_obsidian_note_path(content_hash: str, path: str):
    """Records the vault-root note path created by
    obsidian_sync.push_recording() -- Tasks/People/Calendar/Publications
    wiki-link back to this, and it's the note
    check_social_post_generation_triggers_once() polls for the
    "generate_social_media" frontmatter checkbox on non-journal recordings."""
    with _lock:
        records = _load()
        record = _find(records, content_hash)
        if record is None:
            return
        record["obsidian_note_path"] = path
        _save(records)


def set_obsidian_journal_note_path(content_hash: str, path: str):
    """Same as set_obsidian_note_path() but for obsidian_sync.push_journal()'s
    Journal/ note -- the trigger-poll target for journal-classified
    recordings, which have no vault-root note (see obsidian_sync.push_recording)."""
    with _lock:
        records = _load()
        record = _find(records, content_hash)
        if record is None:
            return
        record["obsidian_journal_note_path"] = path
        _save(records)


def set_obsidian_publication_note_path(content_hash: str, path: str):
    """Records the single Publications/ note path created by
    obsidian_sync.push_social_posts() -- one note covers every platform
    (sectioned in the body), same shape as set_notion_publication_page_id()."""
    with _lock:
        records = _load()
        record = _find(records, content_hash)
        if record is None:
            return
        record["obsidian_publication_note_path"] = path
        _save(records)


def set_task_email_links(content_hash: str, links: list):
    """Records notion_sync.push_tasks()'s per-email-item {"index",
    "task_page_id", "person_page_id", "draft_id"} mapping, so
    poller.check_notion_email_approvals_once() can find each Task page
    again to read its "Approve & Send" checkbox and, on send, log the
    email onto the recipient's People page."""
    with _lock:
        records = _load()
        record = _find(records, content_hash)
        if record is None:
            return
        record["task_email_links"] = links
        _save(records)


def merge_task_email_links(content_hash: str, links: list):
    """Like set_task_email_links(), but merges by "index" into whatever's
    already there instead of overwriting -- used when Notion and Obsidian
    are both configured, so obsidian_sync.push_tasks()'s
    {"task_note_path", "person_note_path", ...} entries land on the SAME
    per-action-item link dict as notion_sync.push_tasks()'s
    {"task_page_id", "person_page_id", ...} entries (same "index"/
    "draft_id"), rather than one backend's call clobbering the other's.
    poller.check_notion_email_approvals_once()/check_obsidian_email_approvals_once()
    each just look for the keys relevant to their own backend on the
    merged dict."""
    with _lock:
        records = _load()
        record = _find(records, content_hash)
        if record is None:
            return
        existing = {l.get("index"): l for l in (record.get("task_email_links") or [])}
        for link in links:
            idx = link.get("index")
            if idx in existing:
                existing[idx].update(link)
            else:
                existing[idx] = link
        record["task_email_links"] = [existing[k] for k in sorted(existing, key=lambda x: (x is None, x))]
        _save(records)


def get_undistributed(destination: str):
    """Successfully processed recordings not yet pushed to `destination`
    ("notion" or "obsidian"). Excludes anything absorbed into another
    recording via merge_recordings() (merged_into set) -- a merged-away
    record has no content of its own left to push; the keeper it merged
    into carries the combined content and gets pushed/re-pushed instead."""
    field = f"{destination}_synced"
    with _lock:
        records = _load()
    return [r for r in records if r["status"] == "done" and not r.get(field) and not r.get("merged_into")]


def mark_failed(content_hash: str, error: str):
    with _lock:
        records = _load()
        record = _find(records, content_hash)
        if record is None:
            return
        record["status"] = "failed"
        record["error"] = error
        _save(records)


def delete_recording(content_hash: str) -> bool:
    """Removes a recording's audio file from disk and its entry entirely,
    and tombstones its device name (see is_tombstoned()) so it's never
    re-synced just because its active record is gone -- previously, an
    SD-sourced recording (kept on the device forever by design) would
    silently reappear on the very next poll cycle after being deleted here,
    since the dedup check only looked at the (now-empty) active records
    list. The device itself still has the file -- see
    delete_recording_from_device() for the separate, explicit action that
    also removes it there.

    Returns True if something was actually deleted, False if no record
    matched (already gone / bad hash).
    """
    with _lock:
        records = _load()
        record = _find(records, content_hash)
        if record is None:
            return False
        wav_path = record.get("wav_path")
        if wav_path and os.path.exists(wav_path):
            os.remove(wav_path)
        records = [r for r in records if r["content_hash"] != content_hash]
        _save(records)
    _add_tombstone(record["name"], record.get("size"))
    remove_pending_person_links_for_recording(record["name"])
    return True


def delete_recording_from_device(content_hash: str) -> dict:
    """Like delete_recording(), but also tells the device to actually erase
    the file from its SD card via the current transport's
    delete_recording_from_sd(name) -- a real, physical delete, unlike the
    device's own DELETE command (which only ever clears the RAM fallback
    recording; SD files are otherwise kept forever by design, see
    ble_sync.cpp/wifi_sync.cpp). Returns {"deleted_locally": bool,
    "deleted_on_device": bool, "device_error": str|None} -- the on-device
    delete can fail independently (BLE unreachable, etc.) without losing
    the local deletion+tombstone, which still take effect regardless."""
    with _lock:
        records = _load()
        record = _find(records, content_hash)
    if record is None:
        return {"deleted_locally": False, "deleted_on_device": False, "device_error": "not found"}

    name = record["name"]
    deleted_locally = delete_recording(content_hash)  # tombstones `name` -- see below

    device_error = None
    deleted_on_device = False
    try:
        import poller
        transport = poller._get_transport()
        transport.delete_recording_from_sd(name)
        deleted_on_device = True
        # The file is now genuinely erased from the SD card. recorder.cpp's
        # boot-time index scan (ensureRecIndexInitialized) will happily
        # reuse this exact filename for a completely unrelated future
        # recording once the device next reboots (it only looks at what
        # actually exists on the card, which is correct -- the name is
        # truly free now). A permanent tombstone left over from
        # delete_recording() above would then silently block that
        # unrelated new content from ever syncing, recreating almost the
        # same "new recording looks like an already-deleted one" bug this
        # tombstone system exists to prevent -- so remove it here. Plain
        # (name, size) dedup already protects against the one-in a-million
        # case of a new recording matching the old one's exact byte size,
        # same as any other SD recording. The tombstone stays in place only
        # for the soft-delete path (delete_recording() called alone), where
        # the file is still physically on the card and must never be
        # re-offered as "new" under its unchanged name+size.
        _remove_tombstone(name)
    except Exception as e:
        device_error = str(e)

    return {"deleted_locally": deleted_locally, "deleted_on_device": deleted_on_device, "device_error": device_error}


def set_meeting(content_hash: str, meeting: dict):
    """Attaches calendar metadata ({title, start, end, attendees:[{name,
    email}]}) to a meeting recording -- set once by meeting_recorder.stop(),
    read by the summarization prompt and Notion People email matching.

    Also seeds official_transcript_check_until (meeting end + ~20 minutes,
    a reasonable bound on Google's own transcript-processing delay) so
    poller.check_official_meeting_transcripts_once() knows how long it's
    worth polling for an official Google Meet transcript before giving up
    -- see that function and google_client.get_meeting_transcript()."""
    with _lock:
        records = _load()
        record = _find(records, content_hash)
        if record is None:
            return
        record["meeting"] = meeting
        end = (meeting or {}).get("end")
        if end:
            try:
                end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
                record["official_transcript_check_until"] = (end_dt + timedelta(minutes=20)).isoformat()
            except (ValueError, TypeError):
                pass
        _save(records)


def mark_official_transcript(content_hash: str, status: str):
    """status: "applied" (an official Google Meet transcript was found and
    the recording upgraded to use it) or "gave_up" (the check-until window
    lapsed with nothing found) -- see
    poller.check_official_meeting_transcripts_once()."""
    with _lock:
        records = _load()
        record = _find(records, content_hash)
        if record is None:
            return
        record["official_transcript_status"] = status
        _save(records)


def set_drafts(content_hash: str, drafts: dict):
    """Stores the generated post-meeting follow-up drafts (see
    poller.generate_drafts_once). Drafts are only ever *generated* by the
    poller -- executing one (sending the email etc.) requires an explicit
    approve via the dashboard, which then calls update_draft()."""
    with _lock:
        records = _load()
        record = _find(records, content_hash)
        if record is None:
            return
        record["drafts"] = drafts
        _save(records)


def update_draft(content_hash: str, draft_id: str, **fields) -> bool:
    """Updates one draft item's fields (status/error/sent_at). Returns False
    if the recording or draft doesn't exist."""
    with _lock:
        records = _load()
        record = _find(records, content_hash)
        if record is None or not record.get("drafts"):
            return False
        for item in record["drafts"].get("items", []):
            if item.get("id") == draft_id:
                item.update(fields)
                _save(records)
                return True
    return False


def set_social_posts(content_hash: str, posts: dict):
    """Stores the generated per-platform social post drafts (see
    poller.generate_social_posts_once / notion_sync.push_social_posts).
    Mirrors set_drafts()'s shape -- generation and approval are separate
    steps, approval happens via the Notion Publications database checkbox
    (see poller.check_publication_approvals_once)."""
    with _lock:
        records = _load()
        record = _find(records, content_hash)
        if record is None:
            return
        record["social_posts"] = posts
        _save(records)


def update_social_post(content_hash: str, platform: str, **fields) -> bool:
    """Updates one platform's post fields (status/scheduled_at/published_at/
    url/error). Returns False if the recording or platform entry doesn't
    exist."""
    with _lock:
        records = _load()
        record = _find(records, content_hash)
        if record is None or platform not in (record.get("social_posts") or {}):
            return False
        record["social_posts"][platform].update(fields)
        _save(records)
        return True


def get_recording(content_hash: str):
    with _lock:
        records = _load()
    return _find(records, content_hash)


def get_wav_path(content_hash: str):
    with _lock:
        records = _load()
    record = _find(records, content_hash)
    return record["wav_path"] if record else None


def list_recordings():
    """Newest first."""
    with _lock:
        records = _load()
    return sorted(records, key=lambda r: r["created_at"], reverse=True)


# --- Pending "which person is this?" confirmations -------------------------
# A Notion Task/Calendar entry can be created referencing a name (task
# owner, email draft recipient) with no email to confirm identity by --
# rather than guessing which existing People page (if any) that name
# refers to, notion_sync.py leaves the entry's "Related Person" relation
# unset and registers one of these instead, surfaced on the dashboard for
# the user to pick from (or say "new person") -- see app.py's
# /api/pending-person-links routes. A single JSON file (not part of the
# per-recording records list) since a link isn't really a property of one
# recording -- the same ambiguous name can turn up across several.
_PENDING_PERSON_LINKS_PATH = os.path.join(paths.APP_DATA_DIR, "pending_person_links.json")


def _migrate_pending_link(link: dict) -> dict:
    """Upgrades a pre-dedup entry (single "notion_page_id"/"database_kind")
    to the current {"notion_page_ids", "database_kinds"} list shape -- see
    add_pending_person_link's dedup, which needs more than one target page
    per link (the same ambiguous name showing up in both a Task and a
    Calendar entry for one recording used to register two separate,
    duplicate confirmations instead of one)."""
    if "notion_page_ids" in link:
        return link
    link = dict(link)
    page_id = link.pop("notion_page_id", None)
    kind = link.pop("database_kind", None)
    link["notion_page_ids"] = [page_id] if page_id else []
    link["database_kinds"] = [kind] if kind else []
    return link


def _load_pending_person_links() -> list:
    try:
        with open(_PENDING_PERSON_LINKS_PATH) as f:
            raw = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    return [_migrate_pending_link(link) for link in raw]


def _save_pending_person_links(links: list):
    with open(_PENDING_PERSON_LINKS_PATH, "w") as f:
        json.dump(links, f, indent=2)


def add_pending_person_link(name: str, notion_page_id: str, database_kind: str,
                             candidates: list, recording_name: str) -> str:
    """Registers one ambiguous name->person link awaiting user confirmation,
    or -- if the same (name, recording) is already pending (e.g. the same
    ambiguous owner shows up in both a Task and a Calendar entry for one
    recording) -- adds this page as another target on the EXISTING link
    instead of creating a duplicate confirmation. Confirming once then
    relates every one of those pages to the same person (see
    notion_sync.resolve_pending_person_link), rather than showing the user
    "Which Sanchit is this?" twice for the same recording.

    `candidates` is [{"id","name","note","email"}, ...] -- existing People
    pages that could be this person, resolved by notion_sync.py at push
    time (candidates may be a single page -- per the user's own choice,
    even one same-name match still isn't auto-linked, only a real email
    match is confident enough to skip confirmation entirely). Returns the
    link's id (new or existing)."""
    links = _load_pending_person_links()
    existing = next((l for l in links if l["name"] == name and l["recording_name"] == recording_name), None)
    if existing:
        if notion_page_id not in existing["notion_page_ids"]:
            existing["notion_page_ids"].append(notion_page_id)
            existing["database_kinds"].append(database_kind)
            _save_pending_person_links(links)
        return existing["id"]

    import uuid
    link_id = uuid.uuid4().hex[:12]
    links.append({
        "id": link_id, "name": name,
        "notion_page_ids": [notion_page_id], "database_kinds": [database_kind],
        "candidates": candidates, "recording_name": recording_name,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    _save_pending_person_links(links)
    return link_id


def list_pending_person_links() -> list:
    return _load_pending_person_links()


def remove_pending_person_link(link_id: str) -> dict:
    """Removes and returns the link (or None if not found) -- the caller
    (app.py) removes it once it's actually patched the Notion relation,
    so a failed patch can leave it in the queue to retry rather than
    silently losing the confirmation."""
    links = _load_pending_person_links()
    match = next((l for l in links if l["id"] == link_id), None)
    if match:
        links = [l for l in links if l["id"] != link_id]
        _save_pending_person_links(links)
    return match


def remove_pending_person_links_for_recording(recording_name: str) -> int:
    """Purges any pending links tied to a recording -- called on delete, so
    confirming a since-deleted recording's speaker/attendee name doesn't
    keep surfacing on the dashboard forever. Returns how many were removed."""
    links = _load_pending_person_links()
    kept = [l for l in links if l["recording_name"] != recording_name]
    if len(kept) != len(links):
        _save_pending_person_links(kept)
    return len(links) - len(kept)
