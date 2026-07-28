"""Background loop, split into two independent phases:

1. Sync — check the device's /list, download anything not already synced
   (by name+size, see storage.is_known_by_size), save it to disk and the
   dashboard immediately as status="pending". Never re-downloads something
   already synced, even if processing it later fails.
2. Process — transcribe/summarize anything still "pending" or "failed",
   using the already-downloaded bytes on disk. Doesn't touch the device at
   all, so a bad API key or LLM hiccup can be fixed and retried next cycle
   without needing to re-fetch audio over BLE/WiFi.

Runs as an asyncio task started from app.py's lifespan handler.
"""
import asyncio
import hashlib
import logging
import time
from datetime import datetime, timezone

import requests

import config
import google_client
import meeting_recorder
import notifications
import settings
import status
import storage
from providers import get_transcriber, get_summarizer
from providers.base import format_transcript_with_speakers

log = logging.getLogger("poller")

# Must match RAM_RECORDING_NAME in epaper_transcriber/recorder.cpp — the
# fixed filename the device always uses for its PSRAM fallback recording.
RAM_RECORDING_NAME = "ram_recording.wav"


def _is_valid_wav(data: bytes) -> bool:
    """Sanity-checks the RIFF/WAVE magic bytes before trusting a download.
    Defense-in-depth against BLE transfer corruption (a dropped/reordered
    first packet silently produces a file with no valid header at all,
    which otherwise wouldn't be caught until the STT provider rejects it
    with an opaque "could not decode" error) -- this check is also what
    makes ble_sync.cpp's CHUNK_DELAY_MS safe to tune down for speed: any
    corruption from too-aggressive pacing gets caught here and silently
    retried next poll cycle, rather than being trusted and passed on to
    transcription broken.
    """
    return len(data) >= 44 and data[0:4] == b"RIFF" and data[8:12] == b"WAVE"

# Cache for the "is WiFi actually reachable right now" probe below --
# re-checked on a short interval, not every single poll (POLL_INTERVAL_SECONDS
# is as low as 3s; re-probing that often would mean an HTTP timeout or a BLE
# round-trip on nearly every cycle even when nothing's changed).
_WIFI_PROBE_INTERVAL_SECONDS = 15
_wifi_probe = {"base_url": None, "last_check": 0.0, "reachable": False}

# Firmware-push attempts are throttled separately (and much less often) --
# every reachable-WiFi poll would otherwise mean an extra /version round
# trip forever, even though the answer only ever changes right after a
# real app update ships a newer bundled firmware.bin.
_FIRMWARE_PUSH_CHECK_INTERVAL_SECONDS = 300
_last_firmware_push_check = 0.0


def _http_reachable(base_url: str) -> bool:
    try:
        resp = requests.get(f"{base_url}/list", timeout=2)
        return resp.ok
    except requests.RequestException:
        return False


def _wifi_base_url_if_reachable():
    """Returns a working http://<ip> base URL for the device's WiFi HTTP
    server if one is currently reachable, else None. Prefers re-checking
    the last-known-good URL (cheap, no BLE involved) before falling back
    to asking the device for its current IP over BLE -- which also
    doubles as auto-discovery: the device's WiFi IP is DHCP-assigned and
    can change between reboots/reconnects, so this never assumes
    config.DEVICE_BASE_URL is still correct, it verifies live."""
    now = time.monotonic()
    if now - _wifi_probe["last_check"] < _WIFI_PROBE_INTERVAL_SECONDS:
        return _wifi_probe["base_url"] if _wifi_probe["reachable"] else None

    cached = _wifi_probe["base_url"]
    if cached and _http_reachable(cached):
        _wifi_probe.update(last_check=now, reachable=True)
        return cached

    # No cached URL, or it stopped working -- ask the device for its
    # current WiFi status over BLE (works regardless of sync_transport;
    # see ble_device_client.get_wifi_status()'s docstring) and verify
    # whatever IP it reports is actually reachable before trusting it.
    discovered = None
    try:
        import ble_device_client
        wifi_status = ble_device_client.get_wifi_status()
        if wifi_status.get("connected") and wifi_status.get("ip"):
            candidate = f"http://{wifi_status['ip']}"
            if _http_reachable(candidate):
                discovered = candidate
    except Exception as e:
        log.debug("WiFi reachability auto-discovery via BLE failed: %s", e)

    _wifi_probe.update(base_url=discovered, last_check=now, reachable=discovered is not None)
    return discovered


def get_device_firmware_version():
    """Best-effort current firmware version string from the paired device
    (see wifi_sync.cpp's /version route), or None if it's not reachable
    over WiFi right now. Used by Settings' Device panel -- reuses the same
    throttled reachability check _get_transport() does, so this doesn't add
    its own extra BLE/network round trip beyond what's already cached."""
    base_url = _wifi_base_url_if_reachable()
    if not base_url:
        return None
    try:
        resp = requests.get(f"{base_url}/version", timeout=2)
        if resp.ok:
            return resp.json().get("version")
    except Exception as e:
        log.debug("device firmware version check failed (non-fatal): %s", e)
    return None


def _get_transport():
    """Resolved fresh on every poll, not cached at import — so switching
    SYNC_TRANSPORT via /settings takes effect on a running process without
    needing a restart.

    WiFi is preferred automatically whenever the device is actually
    reachable over it, regardless of the stored sync_transport setting --
    confirmed live that BLE's GATT notify() path (no per-packet ack) stalls
    partway through real transfers in an RF-noisy environment once WiFi
    was available as an alternative, so silently staying on BLE just
    because that's what was configured back when the device couldn't join
    any network yet is the wrong default once it can. sync_transport is
    still meaningful as a *forced* choice: "ble" only actually forces BLE
    when WiFi genuinely isn't reachable (the normal case that setting
    exists for); "wifi" behaves the same as auto since WiFi already wins
    when reachable.
    """
    wifi_url = _wifi_base_url_if_reachable()
    if wifi_url:
        config.DEVICE_BASE_URL = wifi_url

        global _last_firmware_push_check
        now = time.monotonic()
        if now - _last_firmware_push_check > _FIRMWARE_PUSH_CHECK_INTERVAL_SECONDS:
            _last_firmware_push_check = now
            try:
                import update_check
                update_check.push_firmware_update_if_needed(wifi_url)
            except Exception as e:
                log.debug("firmware update check failed (non-fatal): %s", e)
        # One connection to the device at a time -- BLE is strictly the
        # backup path. The ESP32's single radio is time-sliced between BLE
        # and WiFi, so an idle-but-open BLE connection during WiFi
        # transfers measurably cuts throughput (see
        # ble_device_client.release_connection()). is_connected() is a
        # cheap in-memory check, so this is free when already released.
        import ble_device_client
        try:
            if ble_device_client.is_connected():
                ble_device_client.release_connection()
        except Exception as e:
            log.debug("BLE release before WiFi transport failed: %s", e)
        status.update(sync_transport_active="wifi")
        import device_client as transport
        return transport
    if config.SYNC_TRANSPORT in (None, "wifi", "ble"):
        status.update(sync_transport_active="ble")
        import ble_device_client as transport
        return transport
    raise ValueError(f"Unknown SYNC_TRANSPORT '{config.SYNC_TRANSPORT}' — expected 'wifi' or 'ble'")


async def sync_once():
    transport = _get_transport()
    status.update(device_connecting=True)
    try:
        recordings = await asyncio.to_thread(transport.list_recordings)
    except Exception as e:
        log.warning("could not reach device via %s: %s", config.SYNC_TRANSPORT, e)
        status.update(device_connecting=False, device_connected=False)
        return
    status.update(device_connecting=False, device_connected=True)
    if recordings:  # avoid spamming the log every 3s when there's nothing to report
        log.info("device /list: %s", recordings)

    for entry in recordings:
        name = entry["name"]
        size = entry.get("size")

        # Skip re-downloading anything already synced -- checked by
        # name+size *before* touching the device again. This guard is
        # deliberately skipped for the RAM fallback recording specifically
        # (see below): its name never changes, and a false size collision
        # here used to permanently strand a new recording. SD-card
        # recordings keep permanently unique, incrementing filenames
        # (rec_001.wav, rec_002.wav, ...), so a genuine same-name+size
        # collision there really does mean "already synced" -- this guard
        # stays meaningful and safe for those.
        if name != RAM_RECORDING_NAME and storage.is_known_by_size(name, size):
            log.info("skipping %s (%s bytes) -- already synced a recording with this exact name+size before; "
                      "if this is actually new audio, see poller.py's is_known_by_size note", name, size)
            continue

        try:
            wav_bytes = await asyncio.to_thread(transport.download_recording, name)
        except Exception as e:
            log.warning("failed to download %s: %s", name, e)
            continue

        if not _is_valid_wav(wav_bytes):
            # Don't mark this (name, size) as known -- leaving it unknown
            # means the next poll cycle retries the download instead of
            # silently keeping a corrupt file around.
            log.warning("downloaded %s but it's not a valid WAV file (got %d bytes, no RIFF/WAVE header) "
                        "-- likely a dropped BLE packet, will retry next cycle", name, len(wav_bytes))
            continue

        content_hash = hashlib.md5(wav_bytes).hexdigest()
        # RAM recordings skip the pre-download is_known_by_size gate above,
        # so a post-download check is needed here instead -- but by actual
        # content, not (name, size), since that pre-check is exactly what
        # we're bypassing for this name. SD files keep the size-based
        # check (still valid there -- see is_known_by_size's docstring).
        if name == RAM_RECORDING_NAME:
            already_known = storage.is_known_content_hash(content_hash)
        else:
            already_known = storage.is_known_by_size(name, size)
            if already_known:
                continue  # raced with another sync between the check above and now

        if already_known:
            # Identical content already processed -- this happens when a
            # previous confirm-delete (below) failed, so the device is
            # still offering the exact same bytes. Don't create a
            # duplicate record, but do retry telling the device to clear
            # its buffer, since that's presumably why we're seeing it again.
            log.info("%s content already synced -- retrying confirm-delete (a previous attempt likely failed)", name)
        else:
            storage.add_pending(name, size, content_hash, wav_bytes)
            log.info("synced new recording: %s (%d bytes)", name, len(wav_bytes))

        # Confirm-then-clear: only for the RAM fallback recording, which
        # would otherwise sit in PSRAM until silently overwritten by the
        # next recording. SD-card recordings are a permanent archive and
        # are deliberately never deleted this way (the firmware itself also
        # refuses to delete SD files even if asked — see wifi_sync.cpp/
        # ble_sync.cpp's DELETE handling — this check is just to avoid the
        # pointless round-trip for files we already know will be ignored).
        if name == RAM_RECORDING_NAME:
            try:
                await asyncio.to_thread(transport.delete_recording, name)
                log.info("confirmed sync of %s, device clearing RAM buffer", name)
            except Exception as e:
                log.warning("failed to confirm sync of %s (device will keep offering it): %s", name, e)

    # Tell the device it can turn its WiFi radio off now (battery -- see
    # wifi_sync.cpp/handleSynced()). Only device_client (the WiFi transport)
    # implements this; ble_device_client has no radio to gate this way, so
    # getattr's default keeps this a no-op there. Best-effort: the device's
    # own 120s inactivity fallback covers a missed/failed call, so a
    # failure here is logged but never blocks the sync cycle. Fires every
    # cycle (not just when something new synced) so the radio also turns
    # off promptly after a cycle that found nothing to download.
    signal_sync_complete = getattr(transport, "signal_sync_complete", None)
    if signal_sync_complete:
        try:
            await asyncio.to_thread(signal_sync_complete)
        except Exception as e:
            log.debug("signal_sync_complete failed (device will time out its own radio window instead): %s", e)


def _enforce_journal_rule(summary: dict, segments, pre_classified_conversation: bool = False):
    """Journaling means the recording's owner talking to themself -- by
    definition that requires exactly one speaker. The LLM's "journal" vs
    "actionable" call (as part of the big SUMMARY_JSON_INSTRUCTIONS call)
    is prompted for tone/content, which is a judgment call it can get
    wrong (confirmed: a real 2-person conversation with no tasks got
    called "journal" because it read as casual/unstructured). Three
    independent signals now override that guess rather than just informing
    it -- any single one forces "actionable", since the known failure mode
    is real conversations being missed, not the reverse:
      1. Diarized speaker *count* is a fact, not a judgment call.
      2. speaker_names backstop (below) for when diarization undercounts.
      3. pre_classified_conversation -- a dedicated, single-purpose LLM
         call (providers.base.build_recording_type_prompt) run BEFORE the
         big summary call, judged from content/phrasing rather than
         diarization, so it can catch a real conversation even when
         diarization collapsed everyone into one detected voice.
    A genuine multi-person conversation can never end up "journal" here,
    full stop, regardless of what the prose sounds like."""
    if summary.get("type") != "journal":
        return
    if pre_classified_conversation:
        summary["type"] = "actionable"
        return
    distinct_speakers = {seg.get("speaker_id") for seg in (segments or [])}
    if len(distinct_speakers) > 1:
        summary["type"] = "actionable"
        return
    # Backstop for when diarization undercounts speakers (e.g. one voice
    # dominating the mix, or a provider/fragment with weak separation) --
    # confirmed in the wild via a diarized-but-collapsed-to-1-speaker
    # fragment. "speaker_names" is derived by the LLM directly from the
    # transcript text (self-introductions, being addressed by name), not
    # from diarization, so it catches real multi-person conversations that
    # distinct_speakers above misses.
    if len(summary.get("speaker_names") or {}) > 1:
        summary["type"] = "actionable"


def _add_speakers_as_stakeholders(summary: dict):
    """The LLM's "stakeholders" list is prompted for *other* people the
    speaker mentions -- it doesn't reliably include the speaker themself.
    Since we already have a separate, deterministic "speaker_names" guess
    (self-identification), fold any of those in as stakeholders too,
    skipping names already present (case-insensitive) so a speaker who
    also gets mentioned by others isn't duplicated."""
    speaker_names = summary.get("speaker_names") or {}
    if not speaker_names:
        return
    stakeholders = summary.setdefault("stakeholders", [])
    existing = {(s.get("name") or "").strip().lower() for s in stakeholders}
    for name in speaker_names.values():
        if name and name.strip().lower() not in existing:
            stakeholders.append({"name": name, "note": "speaker in this recording"})
            existing.add(name.strip().lower())


async def resync_after_rename(content_hash: str):
    """Called after a speaker is renamed (dashboard's Speakers section /
    inline transcript click, or an edit made directly in Notion -- see
    app.py's rename_speaker route and sync_speaker_edits_once below).

    Renaming only ever changes the raw speaker_id -> display name mapping;
    it doesn't retroactively fix the LLM's own prose. Summary and
    Stakeholders were written once, at original processing time, using
    whatever label existed then (often literally "Speaker 1" if no name
    was known yet) -- a later rename left that text stale. The real fix is
    to re-run the summarizer against a transcript that already has the
    corrected name baked into each line, so the new prose picks it up
    naturally, then push the whole Notion page body again (not just the
    Transcript block).

    The fresh summarize() call also returns its own "speaker_names" guess,
    which gets discarded in favor of record["speaker_names"] -- that's the
    user-confirmed value this whole function exists to propagate; a new
    guess re-run against already-resolved-name text isn't more authoritative
    and could only make things worse if it guessed wrong.
    """
    record = storage.get_recording(content_hash)
    if not record or not record.get("segments"):
        return

    old_type = (record.get("summary") or {}).get("type")

    formatted = format_transcript_with_speakers(
        record.get("transcript") or "", record["segments"], record.get("speaker_names")
    )
    _, summarize = get_summarizer()
    new_summary = await asyncio.to_thread(summarize, formatted, record.get("deepgram_insights"), record.get("meeting"))
    new_summary["speaker_names"] = record.get("speaker_names") or {}
    _enforce_journal_rule(new_summary, record["segments"])
    _add_speakers_as_stakeholders(new_summary)
    storage.update_summary(content_hash, new_summary)

    # A rename can shift the journal/actionable call (this literally
    # happened testing this feature -- renaming "speaker_1" changed enough
    # context that the classification flipped). Without resetting these,
    # a recording that becomes "journal" here would never get pushed to
    # the Journal database (stuck on a stale "already handled" flag from
    # before), and vice versa for Tasks/Calendar. Tasks/Calendar are
    # deliberately only reset on a type change (not every rename) -- unlike
    # People, push_tasks()/push_events() always create a brand-new page
    # with no find-or-create check, so resetting them unconditionally would
    # create a duplicate Task/Event page on every single rename.
    if new_summary.get("type") != old_type:
        storage.reset_distribution_flags(
            content_hash, ["notion_tasks", "notion_people", "notion_events", "notion_journal"]
        )
    else:
        # People, by contrast, IS safe to always re-push: push_people()
        # find-or-creates by email/name (see notion_sync.py), so re-running
        # it after a rename just links the now-correctly-named speaker to
        # an existing or new People page rather than duplicating anything.
        # Without this, a renamed speaker who was already a "stakeholder"
        # under a stale name (or wasn't one at all before the rename) never
        # gets pushed to People at all, since notion_people_synced was
        # already True from the original push and this record would
        # otherwise never be reconsidered.
        storage.reset_distribution_flags(content_hash, ["notion_people"])

    if record.get("notion_page_id"):
        import notion_sync
        fresh_record = storage.get_recording(content_hash)
        try:
            await asyncio.to_thread(notion_sync.update_all_blocks, record["notion_page_id"], fresh_record)
            # Also sync the "Speaker N" *properties*, not just the page
            # body -- without this, a rename made on the dashboard (or a
            # restore after a Notion-side rename) leaves Notion's property
            # value stale, which sync_speaker_edits_once() would then treat
            # as the authoritative source on the next poll cycle and flip
            # the name right back. This is what keeps the two directions
            # from fighting each other.
            slots = {
                idx: name
                for sid, name in fresh_record["speaker_names"].items()
                if (idx := notion_sync.speaker_slot_index(sid))
            }
            if slots:
                await asyncio.to_thread(notion_sync.set_speaker_slot_values, record["notion_page_id"], slots)
        except Exception as e:
            log.error("re-summarized %s locally but failed to refresh Notion page: %s", record["name"], e)

    # Voice-ID enrollment path C (see voice_id.py's module docstring) --
    # a rename is exactly the "confidently associated with a real name"
    # moment enrollment should happen at. Re-enrolls EVERY currently-named
    # speaker in this recording, not just the one just renamed -- simpler
    # than threading which speaker_id changed through this call, and
    # harmless (enroll_or_update just re-averages) for names that were
    # already correct. This is also how correcting a wrong suggestion
    # "retrains" itself, per the plan -- no separate retrain action needed.
    import voice_id
    if voice_id.is_enabled():
        try:
            with open(storage.get_wav_path(content_hash), "rb") as f:
                wav_bytes = f.read()
            for speaker_id, name in (record.get("speaker_names") or {}).items():
                if not name:
                    continue
                embedding = voice_id.embedding_for_speaker(wav_bytes, record["segments"], speaker_id)
                if embedding:
                    voice_id.enroll_or_update(voice_id.normalize_person_key(name), embedding, display_name=name)
        except Exception as e:
            log.warning("voice enrollment after rename failed for %s (non-fatal): %s", content_hash, e)

    await rescan_voice_suggestions(exclude_content_hash=content_hash)


async def rescan_voice_suggestions(exclude_content_hash: str = None):
    """A voiceprint just got trained (or retrained) somewhere -- call this
    right after every voice_id.enroll_or_update() so already-processed
    recordings benefit immediately too, not just future ones. process_once()
    only ever computes speaker_name_suggestions/candidates once, at original
    processing time (see its voice-recognition block); without this, a
    recording transcribed before you were enrolled would show "no voice
    match" forever, even after training improved.

    Only touches summary["speaker_name_suggestions"]/["speaker_name_candidates"]
    for speakers that still have no confirmed speaker_names entry -- never
    re-summarizes, re-pushes to Notion/Obsidian, or touches a speaker who
    already has a name (renaming is still the only way to confirm one)."""
    import voice_id
    if not voice_id.is_enabled():
        return
    for record in storage.list_recordings():
        content_hash = record.get("content_hash")
        if record.get("status") != "done" or not record.get("segments") or content_hash == exclude_content_hash:
            continue
        speaker_names = record.get("speaker_names") or {}
        unnamed_sids = [sid for sid in dict.fromkeys(s.get("speaker_id") for s in record["segments"])
                        if sid and sid not in speaker_names]
        if not unnamed_sids:
            continue
        try:
            with open(storage.get_wav_path(content_hash), "rb") as f:
                wav_bytes = f.read()
        except OSError:
            continue

        summary = record.get("summary") or {}
        suggestions = dict(summary.get("speaker_name_suggestions") or {})
        candidates = dict(summary.get("speaker_name_candidates") or {})
        changed = False
        try:
            for sid in unnamed_sids:
                embedding = voice_id.embedding_for_speaker(wav_bytes, record["segments"], sid)
                if not embedding:
                    continue
                result = voice_id.match(embedding)
                if result:
                    person_key, display_name, score = result
                    new_val = {"person_key": person_key, "name": display_name, "score": round(score, 3)}
                    if suggestions.get(sid) != new_val:
                        suggestions[sid] = new_val
                        candidates.pop(sid, None)
                        changed = True
                    continue
                cands = voice_id.match_candidates(embedding)
                new_val = [{"person_key": k, "name": n, "score": round(s, 3)} for k, n, s in cands] if cands else None
                if new_val and candidates.get(sid) != new_val:
                    candidates[sid] = new_val
                    changed = True
                elif not new_val and (sid in suggestions or sid in candidates):
                    suggestions.pop(sid, None)
                    candidates.pop(sid, None)
                    changed = True
        except Exception as e:
            log.warning("voice suggestion rescan failed for %s (non-fatal): %s", content_hash, e)
            continue

        if changed:
            summary["speaker_name_suggestions"] = suggestions
            summary["speaker_name_candidates"] = candidates
            storage.update_summary(content_hash, summary)


_last_retry_attempt = 0.0


def _wav_duration_seconds(wav_bytes: bytes) -> float:
    """Best-effort WAV duration from the RIFF header -- used only for the
    usage-analytics digest (analytics.track_event), so a malformed/partial
    file just contributes 0 rather than raising."""
    import io
    import wave
    try:
        with wave.open(io.BytesIO(wav_bytes)) as w:
            return w.getnframes() / float(w.getframerate())
    except Exception:
        return 0.0


async def process_once():
    # New ("pending") recordings are always attempted right away -- that's
    # the whole point of syncing fast. But retrying something that already
    # failed once shouldn't happen on the same tight interval as sync itself
    # (default 3s) — a bad API key or rate limit would otherwise get hammered
    # every few seconds. Failed items only get retried every
    # PROCESS_RETRY_INTERVAL_SECONDS.
    global _last_retry_attempt
    now = time.monotonic()
    retry_due = (now - _last_retry_attempt) >= config.PROCESS_RETRY_INTERVAL_SECONDS
    if retry_due:
        _last_retry_attempt = now

    for record in storage.get_unprocessed():
        if record["status"] == "failed" and not retry_due:
            continue
        content_hash = record["content_hash"]
        try:
            with open(record["wav_path"], "rb") as f:
                wav_bytes = f.read()
        except OSError as e:
            log.error("could not read stored audio for %s: %s", record["name"], e)
            continue

        status.update(sync_in_progress=True)
        try:
            stt_name, transcribe = get_transcriber()
            llm_name, summarize = get_summarizer()
            transcription = await asyncio.to_thread(transcribe, wav_bytes)
            transcript, segments = transcription["text"], transcription.get("segments")
            insights = transcription.get("deepgram_insights")  # None for non-Deepgram providers

            # Crosstalk mitigation (a noisy venue with other nearby
            # conversations) -- classifies each segment as "primary" or
            # "background" by relative volume, so format_transcript_with_speakers
            # can steer the summarizer away from a quieter, unrelated
            # conversation without deleting it from the record. See
            # audio_analysis.py's module docstring for what this can't do
            # (true simultaneous-speech separation).
            if segments and settings.get_all().get("filter_background_conversations", True):
                import audio_analysis
                segments = audio_analysis.annotate_segment_loudness(wav_bytes, segments)

            formatted = format_transcript_with_speakers(transcript, segments)

            # Dedicated journal-vs-conversation classification, run BEFORE
            # the big summary call rather than left as one field competing
            # for attention inside it -- see providers.base.
            # build_recording_type_prompt's docstring. Feeds
            # _enforce_journal_rule as a third independent signal.
            from providers.base import build_recording_type_prompt
            from providers import get_completer
            _, complete = get_completer()
            type_verdict = await asyncio.to_thread(complete, build_recording_type_prompt(formatted))
            pre_classified_conversation = type_verdict.strip().upper() == "CONVERSATION"

            # Pass insights/meeting through -- each provider's summarize()
            # forwards them to build_summary_prompt() so entity/topic/intent/
            # sentiment context and calendar attendee names enrich the LLM's
            # output without a separate API call.
            summary = await asyncio.to_thread(summarize, formatted, insights, record.get("meeting"))
            _enforce_journal_rule(summary, segments, pre_classified_conversation)
            _add_speakers_as_stakeholders(summary)

            # A journal entry has no action items owed to anyone else, no
            # stakeholders, no calendar logistics -- pushing it through the
            # meeting-oriented Summary/Action items/Stakeholders/Calendar
            # events page layout (see notion_sync._build_blocks) reads as
            # empty meeting boilerplate. Dedicated LLM call builds a
            # journal-specific structure instead (see
            # providers.base.build_journal_writeup_prompt), only for
            # recordings that actually classified as "journal" -- no extra
            # cost for the common (meeting/actionable) case.
            if summary.get("type") == "journal":
                from providers.base import build_journal_writeup_prompt, parse_journal_writeup_json
                try:
                    writeup_raw = await asyncio.to_thread(complete, build_journal_writeup_prompt(formatted))
                    summary["journal_writeup"] = parse_journal_writeup_json(writeup_raw)
                except Exception as e:
                    log.warning("journal writeup generation failed for %s (falling back to plain summary): %s", record["name"], e)

            # Supplement LLM's speaker_names guesses with PERSON entities from
            # Deepgram -- entity detection often catches names the LLM misses
            # (or misattributes to the wrong speaker). Only fills blanks, never
            # overwrites a confident LLM guess or a user's manual rename.
            if insights and insights.get("entities"):
                persons = [
                    e["value"] for e in insights["entities"]
                    if e.get("label") in ("PER", "PERSON")
                ]
                existing_names = set((summary.get("speaker_names") or {}).values())
                # Map persons to speaker slots in order, skipping already-claimed names
                if persons and segments:
                    speaker_order = list(dict.fromkeys(
                        seg["speaker_id"] for seg in segments
                    ))
                    extra_guesses = {}
                    person_iter = iter(p for p in persons if p not in existing_names)
                    for sid in speaker_order:
                        if sid not in (summary.get("speaker_names") or {}):
                            name = next(person_iter, None)
                            if name:
                                extra_guesses[sid] = name
                    if extra_guesses:
                        summary.setdefault("speaker_names", {}).update(extra_guesses)

            # Local voice recognition (see voice_id.py) -- opt-in, default
            # off. For any recording with diarized segments: for a speaker
            # not already named, check enrolled voiceprints for a match and
            # surface it as a SUGGESTION only (summary
            # ["speaker_name_suggestions"]), never silently written into
            # speaker_names -- the dashboard prefills the rename field
            # with it, and typing a different name both corrects the
            # display and re-enrolls under the right name (see
            # resync_after_rename). NOTE: unlike the macOS copy, this
            # doesn't auto-enroll the device owner from a meeting
            # recording's mic channel -- that heuristic assumes
            # meetingcap's specific left=system/right=mic stereo layout,
            # which hasn't been verified to hold for Windows' meeting
            # capture path. Owner voice still gets enrolled fine via the
            # normal rename path (path C) once named like anyone else.
            import voice_id
            if segments and voice_id.is_enabled():
                try:
                    suggestions = {}
                    candidates = {}
                    for sid in dict.fromkeys(seg.get("speaker_id") for seg in segments):
                        if sid is None or sid in (summary.get("speaker_names") or {}):
                            continue
                        embedding = voice_id.embedding_for_speaker(wav_bytes, segments, sid)
                        if not embedding:
                            continue
                        result = voice_id.match(embedding)
                        if result:
                            person_key, display_name, score = result
                            suggestions[sid] = {"person_key": person_key, "name": display_name, "score": round(score, 3)}
                            continue
                        # Nothing confident enough to auto-suggest, but a
                        # few enrolled voices might still be close -- offer
                        # them as "might be one of these" rather than
                        # leaving the field with no hint at all. Still
                        # never auto-applied (see voice_id.match_candidates).
                        cands = voice_id.match_candidates(embedding)
                        if cands:
                            candidates[sid] = [
                                {"person_key": k, "name": n, "score": round(s, 3)} for k, n, s in cands
                            ]
                    if suggestions:
                        summary["speaker_name_suggestions"] = suggestions
                    if candidates:
                        summary["speaker_name_candidates"] = candidates
                except Exception as e:
                    log.warning("voice recognition failed for %s (non-fatal): %s", record["name"], e)

            storage.mark_processed(content_hash, transcript, segments, summary, stt_name, llm_name,
                                    deepgram_insights=insights)
            storage.apply_speaker_name_guesses(content_hash, summary.get("speaker_names"))
            log.info("processed %s via stt=%s llm=%s%s%s", record["name"], stt_name, llm_name,
                      " (diarized)" if segments else "",
                      " (deepgram-insights)" if insights else "")
            status.update(sync_ok=True)

            import analytics
            analytics.track_event("recordings_count")
            analytics.track_event("stt_provider_counts", key=stt_name)
            analytics.track_event("llm_provider_counts", key=llm_name)
            analytics.track_event(
                "recordings_journal_count" if summary.get("type") == "journal" else "recordings_actionable_count")
            analytics.track_event("total_recording_seconds", seconds=_wav_duration_seconds(wav_bytes))
        except Exception as e:
            log.error("failed to process %s: %s", record["name"], e)
            storage.mark_failed(content_hash, str(e))
            status.update(sync_ok=False)
        finally:
            status.update(sync_in_progress=False)


async def distribute_once():
    """Pushes successfully processed recordings to any configured
    destinations (Notion, Obsidian) — independent of sync/process so a
    Notion outage doesn't block transcription and vice versa. Retried every
    cycle for anything not yet marked distributed, same reasoning as
    process_once(): the underlying work (recording+transcript) never needs
    to be redone, just the push itself.
    """
    saved = settings.get_all()

    # A "journal"-classified recording (see providers/base.py's "type"
    # field, and poller._enforce_journal_rule) goes to the Journal
    # database ONLY, not also Notes -- previously every recording was
    # pushed to Notes regardless of type, so a journal entry showed up
    # duplicated in both places. Tasks/People/Calendar were already
    # correctly skipped for journal recordings (a self-reflective
    # monologue shouldn't spawn stray Task/Person/Event pages); this
    # extends the same skip to Notes itself.
    def _is_journal(record):
        return (record.get("summary") or {}).get("type") == "journal"

    if saved.get("notion_token") and saved.get("notion_database_id"):
        import notion_sync
        for record in storage.get_undistributed("notion"):
            if _is_journal(record):
                # No notion_page_id set -- this record's only Notion home
                # is the Journal database (see the push below). Marking
                # distributed here just stops this loop retrying it forever.
                storage.mark_distributed(record["content_hash"], "notion")
                continue
            try:
                page_id = await asyncio.to_thread(notion_sync.push_recording, record)
                storage.set_notion_page_id(record["content_hash"], page_id)
                storage.mark_distributed(record["content_hash"], "notion")
                import analytics
                analytics.track_event("notion_pushes")
            except Exception as e:
                log.error("failed to push %s to Notion: %s", record["name"], e)

    # Tasks/People/Calendar all relate back to the Notes page, so they only
    # run once push_recording() above has produced a notion_page_id for
    # this record -- get_undistributed("notion") already excludes those,
    # but a record can be "notion_synced" from *before* notion_page_id
    # existed (upgrade path), so the notion_page_id check is the real gate.
    # Also skipped entirely for journal recordings (see _is_journal above).

    if saved.get("notion_token") and saved.get("notion_tasks_database_id"):
        import notion_sync
        for record in storage.get_undistributed("notion_tasks"):
            if not record.get("notion_page_id"):
                continue
            if _is_journal(record):
                storage.mark_distributed(record["content_hash"], "notion_tasks")
                continue
            try:
                email_links = await asyncio.to_thread(notion_sync.push_tasks, record, record["notion_page_id"])
                if email_links:
                    storage.set_task_email_links(record["content_hash"], email_links)
                storage.mark_distributed(record["content_hash"], "notion_tasks")
            except Exception as e:
                log.error("failed to push %s's tasks to Notion: %s", record["name"], e)

    if saved.get("notion_token") and saved.get("notion_people_database_id"):
        import notion_sync
        for record in storage.get_undistributed("notion_people"):
            if not record.get("notion_page_id"):
                continue
            if _is_journal(record):
                storage.mark_distributed(record["content_hash"], "notion_people")
                continue
            try:
                await asyncio.to_thread(notion_sync.push_people, record, record["notion_page_id"])
                storage.mark_distributed(record["content_hash"], "notion_people")
            except Exception as e:
                log.error("failed to push %s's people to Notion: %s", record["name"], e)

    if saved.get("notion_token") and saved.get("notion_events_database_id"):
        import notion_sync
        for record in storage.get_undistributed("notion_events"):
            if not record.get("notion_page_id"):
                continue
            if _is_journal(record):
                storage.mark_distributed(record["content_hash"], "notion_events")
                continue
            try:
                await asyncio.to_thread(notion_sync.push_events, record, record["notion_page_id"])
                storage.mark_distributed(record["content_hash"], "notion_events")
            except Exception as e:
                log.error("failed to push %s's events to Notion: %s", record["name"], e)

    if saved.get("notion_token") and saved.get("notion_journal_database_id"):
        import notion_sync
        for record in storage.get_undistributed("notion_journal"):
            if not _is_journal(record):
                storage.mark_distributed(record["content_hash"], "notion_journal")
                continue
            try:
                # notion_page_id is None for journal records (Notes push is
                # skipped for them, see _is_journal above) -- push_journal's
                # note_page_id param is optional and just degrades to no
                # "Related Note" relation when absent.
                page_id = await asyncio.to_thread(notion_sync.push_journal, record, record.get("notion_page_id"))
                if page_id:
                    storage.set_notion_journal_page_id(record["content_hash"], page_id)
                storage.mark_distributed(record["content_hash"], "notion_journal")
            except Exception as e:
                log.error("failed to push %s to Notion Journal: %s", record["name"], e)

    if saved.get("obsidian_vault_path"):
        import obsidian_sync
        for record in storage.get_undistributed("obsidian"):
            if _is_journal(record):
                # No obsidian_note_path set -- this record's only Obsidian
                # home is the Journal/ note (see the obsidian_journal block
                # below), same dedup as the Notion "notion" block above.
                storage.mark_distributed(record["content_hash"], "obsidian")
                continue
            try:
                note_path = await asyncio.to_thread(obsidian_sync.push_recording, record)
                if note_path:
                    storage.set_obsidian_note_path(record["content_hash"], note_path)
                storage.mark_distributed(record["content_hash"], "obsidian")
                import analytics
                analytics.track_event("obsidian_pushes")
            except Exception as e:
                log.error("failed to push %s to Obsidian: %s", record["name"], e)

    # Tasks/People/Calendar/Journal/Publications mirror the notion_* blocks
    # above, one-for-one -- see obsidian_sync.py's module docstring for the
    # frontmatter-as-properties design this all rests on.

    if saved.get("obsidian_vault_path"):
        import obsidian_sync
        for record in storage.get_undistributed("obsidian_tasks"):
            if not record.get("obsidian_note_path"):
                continue
            if _is_journal(record):
                storage.mark_distributed(record["content_hash"], "obsidian_tasks")
                continue
            try:
                links = await asyncio.to_thread(obsidian_sync.push_tasks, record, record["obsidian_note_path"])
                if links:
                    storage.merge_task_email_links(record["content_hash"], links)
                storage.mark_distributed(record["content_hash"], "obsidian_tasks")
            except Exception as e:
                log.error("failed to push %s's tasks to Obsidian: %s", record["name"], e)

    if saved.get("obsidian_vault_path"):
        import obsidian_sync
        for record in storage.get_undistributed("obsidian_people"):
            if not record.get("obsidian_note_path"):
                continue
            if _is_journal(record):
                storage.mark_distributed(record["content_hash"], "obsidian_people")
                continue
            try:
                await asyncio.to_thread(obsidian_sync.push_people, record, record["obsidian_note_path"])
                storage.mark_distributed(record["content_hash"], "obsidian_people")
            except Exception as e:
                log.error("failed to push %s's people to Obsidian: %s", record["name"], e)

    if saved.get("obsidian_vault_path"):
        import obsidian_sync
        for record in storage.get_undistributed("obsidian_events"):
            if not record.get("obsidian_note_path"):
                continue
            if _is_journal(record):
                storage.mark_distributed(record["content_hash"], "obsidian_events")
                continue
            try:
                await asyncio.to_thread(obsidian_sync.push_events, record, record["obsidian_note_path"])
                storage.mark_distributed(record["content_hash"], "obsidian_events")
            except Exception as e:
                log.error("failed to push %s's events to Obsidian: %s", record["name"], e)

    if saved.get("obsidian_vault_path"):
        import obsidian_sync
        for record in storage.get_undistributed("obsidian_journal"):
            if not _is_journal(record):
                storage.mark_distributed(record["content_hash"], "obsidian_journal")
                continue
            try:
                path = await asyncio.to_thread(obsidian_sync.push_journal, record)
                if path:
                    storage.set_obsidian_journal_note_path(record["content_hash"], path)
                storage.mark_distributed(record["content_hash"], "obsidian_journal")
            except Exception as e:
                log.error("failed to push %s to Obsidian Journal: %s", record["name"], e)


def _build_drafts(record: dict) -> dict:
    """Builds post-meeting follow-up drafts deterministically from the
    already-generated summary -- no extra LLM call needed, since
    action_items/calendar_events/summary text is all this needs. Nothing
    here is sent anywhere; these are previews only. See app.py's
    /recordings/{hash}/drafts/{id}/approve for the only path that actually
    calls Gmail/Tasks/Calendar, gated on an explicit user click.

    Email drafts are NOT a single recap sent to every attendee covering
    every action item -- see _build_email_drafts, shared with standalone
    (non-meeting) recordings -- each goes to the one person responsible
    for their own item only."""
    summary = record.get("summary") or {}
    meeting = record.get("meeting") or {}
    attendee_emails = [a["email"] for a in meeting.get("attendees", []) if a.get("email")]
    items = list(_build_email_drafts(record)["items"])

    for i, action_item in enumerate(summary.get("action_items") or [], start=1):
        items.append({
            "id": f"task-{i}", "kind": "task", "status": "pending",
            "title": action_item.get("text", ""),
            "notes": f"From meeting: {meeting.get('title') or record['name']}",
            "due": action_item.get("due_date"),
            "error": None, "sent_at": None,
        })

    for i, event in enumerate(summary.get("calendar_events") or [], start=1):
        if not event.get("date"):
            continue  # need at least a date to create a real calendar event
        start_time = event.get("time") or "09:00"
        items.append({
            "id": f"event-{i}", "kind": "calendar_event", "status": "pending",
            "title": event.get("title", ""),
            "start": f"{event['date']}T{start_time}:00",
            "end": f"{event['date']}T{start_time}:00",  # duration left to the user to adjust in Calendar after creation
            "attendees": attendee_emails,
            "error": None, "sent_at": None,
        })

    return {"generated_at": datetime.now(timezone.utc).isoformat(), "items": items}


async def generate_drafts_once():
    """Generates post-meeting follow-up drafts (email/tasks/calendar event)
    for meeting recordings once processing has finished -- previews only,
    shown on the dashboard for the user to Approve or Dismiss individually.
    Never sends anything itself; see app.py's approve route for the only
    code path that actually calls Gmail/Tasks/Calendar. Runs once per
    recording (gated on drafts being unset), not every cycle."""
    for record in storage.list_recordings():
        if record["status"] != "done" or not record.get("meeting") or record.get("drafts") is not None:
            continue
        drafts = _build_drafts(record)
        storage.set_drafts(record["content_hash"], drafts)
        log.info("generated %d follow-up draft(s) for %s", len(drafts["items"]), record["name"])


def _lookup_email_for_name(name: str) -> str:
    """Best-effort address lookup for a standalone (non-meeting) email
    draft's recipient -- meeting recordings get this for free from the
    calendar attendee list (see _build_drafts' attendee_emails), but a
    plain voice memo has no such context, just a name the LLM pulled out
    of "I need to email Vijay". Checks the Notion People database (if
    configured) for a page whose title matches the name and has an Email
    property set. Returns "" on no match/no People database configured/any
    error -- this is a convenience, never a hard requirement for building
    the draft (see _build_email_drafts, which still creates a
    draft with an empty recipient for the user to fill in by hand)."""
    database_id = settings.get_all().get("notion_people_database_id")
    if not database_id or not name:
        return ""
    try:
        import notion_sync
        page = notion_sync._find_person_page(name, database_id)
        if page:
            return (page["properties"].get("Email", {}).get("email") or "").strip()
    except Exception as e:
        log.debug("email lookup for %r failed: %s", name, e)
    return ""


def _build_email_drafts(record: dict) -> dict:
    """Shared by both meeting and standalone (non-meeting) recordings --
    one draft per action item flagged comm_type == "email" (see
    providers/base.py's SUMMARY_JSON_INSTRUCTIONS), addressed to just that
    item's comm_recipient about just that one item, never a single email
    bundling every attendee's/action item's business together. Subject/
    body come from the LLM's own professionally-composed email_subject/
    email_body (see SUMMARY_JSON_INSTRUCTIONS) -- this function only
    resolves the recipient's address and shapes the draft record, it
    doesn't compose any text itself. Recipient resolved via
    _lookup_email_for_name (Notion People) when possible -- left blank
    (recipient_name set instead) for the user to fill in on the dashboard
    otherwise. Nothing here is sent anywhere; see app.py's approve route.

    `id` uses the action item's 1-based position in summary["action_items"]
    (e.g. "email-item-3") -- notion_sync.push_tasks() creates one Notion
    Task page per action item in that same order/index, so this id is how
    poller.check_notion_email_approvals_once() later matches a Task page's
    "Approve & Send" checkbox back to the right local draft."""
    summary = record.get("summary") or {}
    items = []
    for i, action_item in enumerate(summary.get("action_items") or [], start=1):
        if action_item.get("comm_type") != "email":
            continue
        recipient_name = (action_item.get("comm_recipient") or "").strip()
        email = _lookup_email_for_name(recipient_name) if recipient_name else ""
        items.append({
            "id": f"email-item-{i}", "kind": "email", "status": "pending",
            "to": [email] if email else [],
            "recipient_name": recipient_name or None,  # shown by the dashboard when "to" is empty, so the user knows who to fill in
            "subject": action_item.get("email_subject") or action_item.get("text", "")[:200] or f"Follow-up from {record['name']}",
            "body": action_item.get("email_body") or action_item.get("text", ""),
            "error": None, "sent_at": None,
        })
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "items": items}


def _find_draft(record: dict, draft_id: str):
    drafts = record.get("drafts")
    if not drafts:
        return None
    return next((d for d in drafts["items"] if d["id"] == draft_id), None)


def approve_and_send_draft(content_hash: str, draft_id: str, to_override: str = "") -> dict:
    """The only code path that actually sends an email, creates a Google
    Task, or books a calendar event -- generate_drafts_once() only ever
    previews these. Shared by app.py's dashboard approve route and
    check_notion_email_approvals_once() (Notion's "Approve & Send"
    checkbox), so both surfaces dispatch through the same logic. Failure
    leaves the draft "pending" with an error message so it can be retried,
    rather than silently losing it.

    `to_override` is only used for an email draft whose recipient
    couldn't be resolved automatically (see _lookup_email_for_name) --
    the dashboard prompts for an address inline in that case; ignored for
    drafts that already have a recipient, and never applicable to the
    Notion-checkbox path (there's no such prompt there, see
    check_notion_email_approvals_once's own recipient check).

    Returns {"body": <JSON-serializable dict>, "status_code": int} so the
    dashboard route can pass both straight through unchanged."""
    record = storage.get_recording(content_hash)
    if not record or not record.get("drafts"):
        return {"body": {"error": "not_found"}, "status_code": 404}
    draft = _find_draft(record, draft_id)
    if not draft:
        return {"body": {"error": "not_found"}, "status_code": 404}
    if draft["status"] != "pending":
        return {"body": {"error": "already_handled", "status": draft["status"]}, "status_code": 409}

    if draft["kind"] == "email" and not draft.get("to") and to_override:
        draft = dict(draft, to=[to_override])
        # Persist immediately (not just this call's local `draft`) so a
        # user-entered address survives even if the send itself then
        # fails for an unrelated reason (bad Gmail token, etc.) -- retrying
        # shouldn't mean re-typing the recipient.
        storage.update_draft(content_hash, draft_id, to=draft["to"])

    try:
        if draft["kind"] == "email":
            if not draft.get("to"):
                return {"body": {"error": "missing_recipient"}, "status_code": 400}
            google_client.send_email(draft["to"], draft["subject"], draft["body"])
        elif draft["kind"] == "task":
            google_client.create_task(draft["title"], draft.get("notes", ""), draft.get("due"))
        elif draft["kind"] == "calendar_event":
            google_client.create_event(draft["title"], draft["start"], draft["end"], draft.get("attendees"))
        else:
            return {"body": {"error": "unknown_kind"}, "status_code": 400}
    except RuntimeError as e:
        storage.update_draft(content_hash, draft_id, error=str(e))
        return {"body": {"error": "send_failed", "detail": str(e)}, "status_code": 502}

    storage.update_draft(content_hash, draft_id, status="approved_sent", sent_at=datetime.now(timezone.utc).isoformat(), error=None)
    import analytics
    analytics.track_event("drafts_approved")
    return {"body": {"ok": True}, "status_code": 200}


async def generate_standalone_email_drafts_once():
    """Same idea as generate_drafts_once(), but for plain voice memos (no
    meeting/attendee context) that mention needing to email someone --
    see _build_email_drafts. Meeting recordings are handled exclusively by
    generate_drafts_once() (which also covers tasks/calendar events, not
    just email, and itself pulls in _build_email_drafts for the email
    portion), so this only runs for the other case to avoid generating
    drafts twice for the same recording."""
    for record in storage.list_recordings():
        if record["status"] != "done" or record.get("meeting") or record.get("drafts") is not None:
            continue
        action_items = (record.get("summary") or {}).get("action_items") or []
        if not any(item.get("comm_type") == "email" for item in action_items):
            continue
        drafts = _build_email_drafts(record)
        if drafts["items"]:
            storage.set_drafts(record["content_hash"], drafts)
            log.info("generated %d standalone email draft(s) for %s", len(drafts["items"]), record["name"])


async def sync_speaker_edits_once():
    """Lets a speaker be renamed directly in Notion, not just the
    dashboard -- reads each synced recording's "Speaker N" properties
    (see notion_sync.SPEAKER_SLOT_COUNT) every poll cycle and treats a
    non-empty value that differs from local state as an edit made in
    Notion. Also the other direction: if the dashboard already knows a
    name (auto-guessed or renamed there) but the Notion property is still
    blank, pushes it into Notion so both surfaces show the same thing
    without the user having to touch both. Runs every cycle rather than
    being gated by a "distributed" flag, since an edit can happen at any
    point after the initial push, not just once."""
    saved = settings.get_all()
    if not (saved.get("notion_token") and saved.get("notion_database_id")):
        return
    import notion_sync

    for record in storage.list_recordings():
        if not record.get("notion_page_id") or not record.get("segments"):
            continue
        try:
            notion_slots = await asyncio.to_thread(notion_sync.get_speaker_slot_values, record["notion_page_id"])
        except Exception as e:
            # A page the user archived/deleted in Notion keeps 404ing or
            # rejecting writes ("archived") forever otherwise -- reset so
            # distribute_once() re-creates it fresh next cycle instead of
            # logging the same failure every poll indefinitely.
            if "object_not_found" in str(e) or "archived" in str(e).lower():
                storage.reset_notion_sync(record["content_hash"])
                log.warning("Notion page for %s is gone/archived -- will re-push a fresh one next cycle", record["name"])
            else:
                log.error("failed to read Notion speaker properties for %s: %s", record["name"], e)
            continue

        current_names = record.get("speaker_names") or {}
        speaker_ids = {seg["speaker_id"] for seg in record["segments"]}
        changed_locally = False
        slots_to_push = {}

        for speaker_id in speaker_ids:
            idx = notion_sync.speaker_slot_index(speaker_id)
            if not idx:
                continue
            notion_value = notion_slots.get(idx, "")
            local_value = current_names.get(speaker_id, "")
            if notion_value and notion_value != local_value:
                storage.set_speaker_name(record["content_hash"], speaker_id, notion_value)
                changed_locally = True
            elif local_value and not notion_value:
                slots_to_push[idx] = local_value

        if slots_to_push:
            try:
                await asyncio.to_thread(notion_sync.set_speaker_slot_values, record["notion_page_id"], slots_to_push)
            except Exception as e:
                if "object_not_found" in str(e) or "archived" in str(e).lower():
                    storage.reset_notion_sync(record["content_hash"])
                    log.warning("Notion page for %s is gone/archived -- will re-push a fresh one next cycle", record["name"])
                    continue
                log.error("failed to push speaker names to Notion for %s: %s", record["name"], e)

        if changed_locally:
            try:
                await resync_after_rename(record["content_hash"])
                log.info("picked up speaker rename from Notion for %s", record["name"])
            except Exception as e:
                log.error("failed to resync after Notion speaker rename for %s: %s", record["name"], e)


async def check_notion_email_approvals_once():
    """Lets an email draft be approved from Notion, not just the
    dashboard -- reads each email-item Task page's "Approve & Send"
    checkbox (see notion_sync.push_tasks/ensure_email_draft_properties)
    every poll cycle, same "read a Notion property, act on it" pattern as
    sync_speaker_edits_once(). This is now the primary approval surface;
    the dashboard route (app.py's approve_draft) still works as a
    fallback since both call the same approve_and_send_draft()."""
    saved = settings.get_all()
    if not (saved.get("notion_token") and saved.get("notion_tasks_database_id")):
        return
    import notion_sync

    for record in storage.list_recordings():
        for link in record.get("task_email_links") or []:
            draft = _find_draft(record, link["draft_id"])
            if not draft or draft["status"] != "pending":
                continue
            try:
                page = await asyncio.to_thread(notion_sync.get_page, link["task_page_id"])
            except Exception as e:
                log.warning("failed to check Approve & Send for %s: %s", link["draft_id"], e)
                continue
            checked = (page.get("properties") or {}).get("Approve & Send", {}).get("checkbox")
            if not checked:
                continue

            # Resolve the recipient's address at SEND time, not just draft
            # time -- priority: the Task page's own editable "Send To"
            # property (typed right where the user approves, no People-page
            # detour), then the draft's stored address, then a fresh People
            # lookup (covers an Email added there since the draft was made).
            send_to = ((page.get("properties") or {}).get("Send To", {}).get("email") or "").strip()
            if send_to and send_to not in (draft.get("to") or []):
                storage.update_draft(record["content_hash"], link["draft_id"], to=[send_to])
                draft = dict(draft, to=[send_to])
                # The user typed this address in Notion -- remember it on
                # the person's People page so it auto-resolves next time.
                try:
                    await asyncio.to_thread(
                        notion_sync.backfill_person_email, link.get("recipient_name"), send_to)
                except Exception as e:
                    log.warning("failed to backfill People email for %r: %s", link.get("recipient_name"), e)
            elif not draft.get("to"):
                email = _lookup_email_for_name(link.get("recipient_name") or draft.get("recipient_name") or "")
                if email:
                    storage.update_draft(record["content_hash"], link["draft_id"], to=[email])
                    draft = dict(draft, to=[email])

            if not draft.get("to"):
                # Still no address from any source -- can't send. Leave the
                # box checked so this fires again next cycle once "Send To"
                # is filled in (the ✍️ hint on the page says to do exactly
                # that); unchecking is the user's way to abort.
                continue
            result = approve_and_send_draft(record["content_hash"], link["draft_id"])
            if not result["body"].get("ok"):
                log.warning("Notion-approved send failed for %s: %s", link["draft_id"], result["body"])
                continue
            # drafts_approved is tracked inside approve_and_send_draft()
            # itself now, since it's the single shared path for both this
            # Notion-checkbox trigger and the dashboard's Approve button --
            # tracking it here too would double-count Notion-triggered sends.
            sent_note = f"✅ Sent {datetime.now(timezone.utc).isoformat()}"
            try:
                await asyncio.to_thread(
                    notion_sync.append_blocks, link["task_page_id"],
                    notion_sync._text_block("paragraph", sent_note))
            except Exception as e:
                log.warning("sent email but failed to update Task page %s: %s", link["task_page_id"], e)
            if link.get("person_page_id"):
                log_entry = (f"📧 Sent {datetime.now(timezone.utc).isoformat()} — "
                             f"Subject: {draft['subject']}\n\n{draft['body']}")
                try:
                    await asyncio.to_thread(
                        notion_sync.append_blocks, link["person_page_id"],
                        notion_sync._text_block("paragraph", log_entry))
                except Exception as e:
                    log.warning("sent email but failed to log it on People page %s: %s", link["person_page_id"], e)
            log.info("sent %s via Notion approval for %s", link["draft_id"], record["name"])


async def check_obsidian_email_approvals_once():
    """Obsidian equivalent of check_notion_email_approvals_once() -- reads
    each email-item Task note's "approve_send" frontmatter checkbox (see
    obsidian_sync.push_tasks) every poll cycle instead of a Notion page.
    Same dedup (skips unless the local draft is still "pending"), same
    "send_to" resolution priority (note frontmatter -> stored draft ->
    fresh People/ lookup), same shared send path
    (approve_and_send_draft()) so this and the Notion trigger (and the
    dashboard's own Approve button) can never double-send the same draft."""
    saved = settings.get_all()
    if not saved.get("obsidian_vault_path"):
        return
    import obsidian_sync

    for record in storage.list_recordings():
        for link in record.get("task_email_links") or []:
            task_path = link.get("task_note_path")
            if not task_path:
                continue
            draft = _find_draft(record, link["draft_id"])
            if not draft or draft["status"] != "pending":
                continue
            fm = await asyncio.to_thread(obsidian_sync.read_frontmatter, task_path)
            checked = fm.get("approve_send")
            if not checked:
                continue

            # Same send_to resolution priority as the Notion path: the
            # Task note's own editable "send_to" frontmatter first, then
            # the draft's stored address, then a fresh People/ lookup.
            send_to = (fm.get("send_to") or "").strip()
            if send_to and send_to not in (draft.get("to") or []):
                storage.update_draft(record["content_hash"], link["draft_id"], to=[send_to])
                draft = dict(draft, to=[send_to])
                try:
                    await asyncio.to_thread(
                        obsidian_sync.backfill_person_email, link.get("recipient_name"), send_to)
                except Exception as e:
                    log.warning("failed to backfill People email for %r: %s", link.get("recipient_name"), e)
            elif not draft.get("to"):
                email = _lookup_email_for_name(link.get("recipient_name") or draft.get("recipient_name") or "")
                if email:
                    storage.update_draft(record["content_hash"], link["draft_id"], to=[email])
                    draft = dict(draft, to=[email])

            if not draft.get("to"):
                # Still no address -- leave the box checked so this fires
                # again next cycle once send_to is filled in.
                continue
            result = approve_and_send_draft(record["content_hash"], link["draft_id"])
            if not result["body"].get("ok"):
                log.warning("Obsidian-approved send failed for %s: %s", link["draft_id"], result["body"])
                continue
            sent_note = f"✅ Sent {datetime.now(timezone.utc).isoformat()}"
            await asyncio.to_thread(obsidian_sync.append_body, task_path, sent_note)
            person_path = link.get("person_note_path")
            if person_path:
                log_entry = (f"📧 Sent {datetime.now(timezone.utc).isoformat()} — "
                             f"Subject: {draft['subject']}\n\n{draft['body']}")
                await asyncio.to_thread(obsidian_sync.append_body, person_path, log_entry)
            log.info("sent %s via Obsidian approval for %s", link["draft_id"], record["name"])


_auto_recording_event = None  # the calendar event (dict) this loop itself started recording for, else None


async def _current_or_next_event_any_source(window_min: int):
    """Tries Google Calendar first (if connected via OAuth), then Apple
    Calendar (if the menu-bar agent's EventKit access is available) --
    whichever source gives a qualifying event wins. Two independent,
    optional sources so this works whether someone uses Google Calendar,
    Apple Calendar, or both; neither is required. Not merged/deduplicated
    across sources -- a same meeting synced to both would just be returned
    from whichever is checked first (Google), a rare edge case not worth
    the complexity of reconciling."""
    import google_client
    import apple_calendar

    if google_client.is_connected():
        event = await asyncio.to_thread(google_client.current_or_next_event, window_min)
        if event:
            return event

    if apple_calendar.is_available():
        event = await asyncio.to_thread(apple_calendar.current_or_next_event, window_min)
        if event:
            return event

    return None


def _any_calendar_source_available() -> bool:
    import google_client
    import apple_calendar
    return google_client.is_connected() or apple_calendar.is_available()


async def check_meeting_auto_start_once():
    """Calendar-based meeting auto-detect: if a calendar event with a
    Meet/Teams link is currently in progress (its start <= now <= end, not
    just "coming up soon" -- we don't want to start recording during the
    lead-in before a meeting) and nothing is already recording, starts one
    automatically. Also auto-stops when that same event's end time passes --
    but only for a recording *this loop* started; a manually-started
    recording (menu-bar click) is never auto-stopped here, since the user
    controls that one directly.

    Checks both Google Calendar and Apple Calendar (see
    _current_or_next_event_any_source) -- non-fatal by design either way:
    any calendar hiccup here just means no auto-start this cycle, never an
    error surfaced to the user -- manual recording via the menu-bar icon
    always works regardless.
    """
    global _auto_recording_event

    if not _any_calendar_source_available():
        return

    state = meeting_recorder.state()

    if state["recording"]:
        # Auto-stop once the event we auto-started for has ended.
        if _auto_recording_event is not None:
            try:
                end = _auto_recording_event.get("end", "")
                if end and datetime.now(timezone.utc) >= datetime.fromisoformat(end.replace("Z", "+00:00")):
                    meeting_recorder.stop()
                    _auto_recording_event = None
                    log.info("auto-stopped meeting recording (calendar event ended)")
            except (ValueError, TypeError) as e:
                log.warning("could not parse calendar event end time for auto-stop: %s", e)
        return

    _auto_recording_event = None  # nothing recording -- clear any stale reference

    event = await _current_or_next_event_any_source(1)  # tight window: only an event starting almost exactly now
    if not event or not event.get("meeting_url"):
        return

    try:
        start = datetime.fromisoformat(event["start"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(event["end"].replace("Z", "+00:00"))
    except (ValueError, TypeError, KeyError):
        return
    now = datetime.now(timezone.utc)
    if not (start <= now <= end):
        return  # only auto-start once the meeting has actually begun

    result = await asyncio.to_thread(meeting_recorder.start, event)
    if result.get("ok"):
        _auto_recording_event = event
        log.info("auto-started meeting recording for calendar event %r", event.get("title"))
    else:
        log.warning("calendar auto-start attempted but recording failed to start: %s", result.get("detail"))


_prep_notified_events = set()  # (title, start) pairs already notified this run -- avoids re-firing every poll cycle
_PREP_WINDOW_MIN = 20   # look this far ahead for an upcoming meeting
_PREP_MIN_LEAD_MIN = 8  # ...but don't fire until it's within this many minutes (avoids notifying way too early)


_STOPWORDS = {"the", "a", "an", "and", "or", "with", "for", "of", "to", "on", "in",
              "meeting", "sync", "call", "chat", "catch", "up", "weekly", "monthly"}


def _title_words(title: str) -> set:
    """Lowercased significant words from a meeting title, for a cheap
    deterministic topic-relevance signal -- common filler words ("sync",
    "meeting", "weekly") are stripped so two meetings both titled "Weekly
    Sync" don't look topically related just because they share that noise."""
    if not title:
        return set()
    return {w for w in title.lower().split() if len(w) > 2 and w not in _STOPWORDS}


def _relative_date(iso_str: str) -> str:
    """Human-friendly recency label ("today", "3 days ago", "2 months ago")
    -- staleness matters a lot for how much weight to put on old context;
    a decision from yesterday and one from 4 months ago shouldn't read the
    same in the prep note."""
    try:
        then = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return ""
    days = (datetime.now(timezone.utc) - then).days
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 14:
        return f"{days} days ago"
    if days < 60:
        return f"{days // 7} weeks ago"
    return f"{days // 30} months ago"


def _past_context_for_attendees(attendees: list, meeting_title: str = None, limit_per_person: int = 2) -> dict:
    """Scans processed recordings for past meetings involving any of the
    given attendee emails (matched via record["meeting"]["attendees"], the
    same field Notion People email-matching uses -- see notion_sync.py's
    _attendee_email_for_name), ranks them by topic relevance to the
    upcoming meeting (title word overlap) then recency, and collects a
    dated summary + open action items owned by them. Also pulls each
    person's Notion People "Note" (role/relationship), if the People
    database is configured, so the note isn't just a bare name.

    Deterministic, no LLM call -- keeps this cheap enough to run every poll
    cycle. Returns {email: {"name", "role", "items": [{"text","date_label",
    "relevant"}], "open_items": [...]}}, only for attendees who actually
    have prior history (a first-time meeting with someone correctly yields
    nothing for them, rather than fabricated context)."""
    emails = {a["email"].lower(): a.get("name", a["email"]) for a in attendees if a.get("email")}
    if not emails:
        return {}

    upcoming_words = _title_words(meeting_title)
    candidates = {email: [] for email in emails}  # email -> list of (relevance, created_at, record)
    open_items = {email: [] for email in emails}

    for record in storage.list_recordings():
        if record["status"] != "done":
            continue
        meeting = record.get("meeting") or {}
        if meeting_title and meeting.get("title") == meeting_title:
            continue  # don't reference the very meeting we're prepping for
        record_emails = {a["email"].lower() for a in meeting.get("attendees", []) if a.get("email")}
        matched = record_emails & emails.keys()
        if not matched:
            continue

        record_words = _title_words(meeting.get("title"))
        relevance = len(upcoming_words & record_words) if upcoming_words else 0
        summary = (record.get("summary") or {}).get("summary", "")

        for email in matched:
            if summary:
                candidates[email].append((relevance, record.get("created_at", ""), summary))
            for item in (record.get("summary") or {}).get("action_items", []):
                if (item.get("owner") or "").strip().lower() == emails[email].strip().lower():
                    open_items[email].append(item.get("text", ""))

    context = {}
    for email, name in emails.items():
        # Most topic-relevant first, then most recent -- a same-project
        # discussion from 3 weeks ago beats an unrelated 1:1 from yesterday.
        ranked = sorted(candidates[email], key=lambda c: (c[0], c[1]), reverse=True)[:limit_per_person]
        if not ranked and not open_items[email]:
            continue
        role = notion_people_note(email, name)
        context[email] = {
            "name": name,
            "role": role,
            "items": [{"text": summary, "date_label": _relative_date(created_at), "relevant": rel > 0}
                      for rel, created_at, summary in ranked],
            "open_items": open_items[email],
        }
    return context


def notion_people_note(email: str, name: str) -> str:
    """Best-effort lookup of a person's Notion People "Note" field (their
    role/relationship) for the prep note -- returns "" if Notion People
    isn't configured or the lookup fails; never blocks or errors out the
    caller over an enrichment that's explicitly optional."""
    saved = settings.get_all()
    database_id = saved.get("notion_people_database_id")
    if not (saved.get("notion_token") and database_id):
        return ""
    try:
        import notion_sync
        return notion_sync.get_person_note(email, name, database_id)
    except Exception as e:
        log.warning("Notion People note lookup failed for %s: %s", email, e)
        return ""


def _notify_macos(title: str, body: str):
    """Native OS notification banner -- fallback path only, used when the
    menu-bar agent (meeting_recorder.show_prep_note, macOS-only -- see its
    own docstring) isn't reachable, e.g. because this is a Windows build,
    which has no such agent at all and always takes this path. Truncated
    to keep the banner readable, unlike the agent popover which can show
    the full text. Named for its original macOS-only implementation (see
    other call sites); now dispatches per-OS below."""
    import subprocess
    import sys

    if sys.platform == "darwin":
        # AppleScript string literals need their own quotes/backslashes escaped.
        safe_title = title.replace("\\", "\\\\").replace('"', '\\"')
        safe_body = body[:250].replace("\\", "\\\\").replace('"', '\\"')
        try:
            subprocess.run(
                ["osascript", "-e", f'display notification "{safe_body}" with title "{safe_title}" sound name "Glass"'],
                timeout=5, capture_output=True,
            )
        except (subprocess.SubprocessError, OSError) as e:
            log.warning("failed to show meeting-prep notification: %s", e)
    elif sys.platform == "win32":
        # PowerShell string literals need their own single-quotes doubled
        # (PowerShell's escape convention, not backslash-based like
        # AppleScript above).
        safe_title = title.replace("'", "''")
        safe_body = body[:250].replace("'", "''")
        script = (
            "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] > $null;"
            "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime] > $null;"
            "$template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
            "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
            f"$text = $template.GetElementsByTagName('text'); $text[0].AppendChild($template.CreateTextNode('{safe_title}')) > $null;"
            f"$text[1].AppendChild($template.CreateTextNode('{safe_body}')) > $null;"
            "$toast = [Windows.UI.Notifications.ToastNotification]::new($template);"
            "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Clicky').Show($toast);"
        )
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", script],
                timeout=5, capture_output=True,
            )
        except (subprocess.SubprocessError, OSError) as e:
            log.warning("failed to show meeting-prep notification: %s", e)


async def check_meeting_prep_once():
    """Pre-meeting prep reminder: for a calendar event starting in roughly
    _PREP_MIN_LEAD_MIN to _PREP_WINDOW_MIN minutes, looks up past recordings
    involving the same attendees and shows a popover anchored under the
    menu-bar icon (see meetingcap/main.swift's PrepPopover) summarizing
    context + still-open action items -- so you walk in already knowing
    what was discussed last time and what's outstanding, without digging
    through old recordings yourself. Falls back to a (truncated) OS
    notification if the menu-bar agent isn't reachable for any reason.

    Separate from check_meeting_auto_start_once() by design: that one only
    acts once a meeting has *started* (to begin recording); this one has to
    fire *before* it starts to be useful as a heads-up, so it needs its own
    earlier window and its own dedupe (a recording can only start once, but
    without a dedupe guard this would renotify every ~poll-interval seconds
    for the whole 12-minute window). Checks both Google Calendar and Apple
    Calendar, same as check_meeting_auto_start_once()."""
    if not _any_calendar_source_available():
        return

    event = await _current_or_next_event_any_source(_PREP_WINDOW_MIN)
    if not event or not event.get("attendees"):
        return

    try:
        start = datetime.fromisoformat(event["start"].replace("Z", "+00:00"))
    except (ValueError, TypeError, KeyError):
        return
    # current_or_next_event(_PREP_WINDOW_MIN) deliberately looks further
    # ahead than we want to fire at, so the event is already known by the
    # time it enters the actual notify window below -- only the check here
    # gates *when* the notification actually fires.
    minutes_until = (start - datetime.now(timezone.utc)).total_seconds() / 60
    if not (0 <= minutes_until <= _PREP_MIN_LEAD_MIN):
        return  # too early (or already started) -- check again next cycle

    dedupe_key = (event.get("title"), event["start"])
    if dedupe_key in _prep_notified_events:
        return

    context = await asyncio.to_thread(_past_context_for_attendees, event["attendees"], event.get("title"))
    if not context:
        _prep_notified_events.add(dedupe_key)  # no history for anyone -- still mark done, nothing to say
        return

    lines = []
    for c in context.values():
        who = f"{c['name']} ({c['role']})" if c["role"] else c["name"]
        for item in c["items"]:
            marker = "★ " if item["relevant"] else ""  # topically related to this meeting, not just same person
            lines.append(f"{marker}{who} — {item['date_label']}: {item['text']}")
        if c["open_items"]:
            lines.append(f"Open for {c['name']}: {'; '.join(c['open_items'][:2])}")
    body = "\n".join(lines)  # untruncated, newline-separated -- the popover has room, unlike a Notification Center banner

    title = f"Upcoming: {event.get('title', 'Meeting')}"
    if not meeting_recorder.show_prep_note(title, body):
        _notify_macos(title, body)  # agent unreachable -- fall back to a (truncated) OS notification
    _prep_notified_events.add(dedupe_key)
    log.info("sent pre-meeting prep note for %r: %s", event.get("title"), body)


async def poll_once():
    if not settings.is_configured():
        return  # nothing to do until first-run /setup has been completed
    await sync_once()
    await process_once()
    await distribute_once()
    await generate_drafts_once()
    await generate_standalone_email_drafts_once()
    await sync_speaker_edits_once()
    await check_notion_email_approvals_once()
    await check_obsidian_email_approvals_once()
    await check_social_post_generation_triggers_once()
    await check_publication_approvals_once()
    await check_social_publish_once()
    await check_usage_report_once()
    await check_meeting_auto_start_once()
    await check_meeting_prep_once()
    check_notifications_once()


# Platforms Substack/Medium carry the full essay; LinkedIn/X carry a short
# teaser linking to it. Medium has no publish API at all (see
# substack_client.py's docstring for why Substack is unofficial too) --
# its "publish" is the user manually using Medium's own Import-a-story flow
# and pasting the resulting URL back into the Notion "Post URL" property,
# which check_publication_approvals_once() below treats the same as an
# automated publish completing.
LONG_FORM_PLATFORMS = ("substack", "medium")
TEASER_PLATFORMS = ("linkedin", "x")


async def check_social_post_generation_triggers_once():
    """On-demand replacement for the old always-journal-only auto-generation:
    polls the "Generate Social Media" checkbox -- notion_sync.
    is_generate_social_triggered on the recording's Notes/Journal page,
    and/or obsidian_sync.is_generate_social_triggered on its vault-root/
    Journal note, whichever backend(s) are configured -- and, if either is
    checked, generates that recording's social posts right now via
    providers.base.build_social_post_prompt, unchanged from before EXCEPT
    it's no longer gated to journal-type recordings (any recording can be
    turned into social posts this way, since the user triggers it
    explicitly rather than it happening automatically). Generates ONCE per
    triggered recording even if both backends' checkboxes happen to be set
    at the same time, then pushes to every configured backend and resets
    every trigger that was on -- this only changes what *triggers*
    generation, not where the results live. The momentary-trigger contract
    (auto-uncheck after generating, so checking it again later regenerates
    a fresh batch) is unchanged from the Notion-only version."""
    saved = settings.get_all()
    notion_configured = bool(saved.get("notion_publications_database_id") and saved.get("notion_token"))
    obsidian_configured = bool(saved.get("obsidian_vault_path"))
    if not notion_configured and not obsidian_configured:
        return  # feature isn't set up on either backend -- see /integrations
    from providers.base import build_social_post_prompt, parse_social_post_json
    from providers import get_completer

    notion_sync = None
    if notion_configured:
        import notion_sync
        # ensure_generate_social_trigger_property is idempotent (module-level
        # cache) -- cheap to call every poll cycle, and this is what adds the
        # checkbox to a user's pre-existing Notes/Journal database (new
        # workspaces already get it at creation, see notion_setup.create_workspace).
        if saved.get("notion_database_id"):
            await asyncio.to_thread(notion_sync.ensure_generate_social_trigger_property, saved["notion_database_id"])
        if saved.get("notion_journal_database_id"):
            await asyncio.to_thread(notion_sync.ensure_generate_social_trigger_property, saved["notion_journal_database_id"])
    obsidian_sync = None
    if obsidian_configured:
        import obsidian_sync

    for record in storage.list_recordings():
        if record["status"] != "done":
            continue
        notion_trigger_id = (record.get("notion_page_id") or record.get("notion_journal_page_id")) if notion_configured else None
        obsidian_trigger_path = (record.get("obsidian_note_path") or record.get("obsidian_journal_note_path")) if obsidian_configured else None
        if not notion_trigger_id and not obsidian_trigger_path:
            continue

        triggered = False
        if notion_trigger_id:
            try:
                if await asyncio.to_thread(notion_sync.is_generate_social_triggered, notion_trigger_id):
                    triggered = True
            except Exception as e:
                log.error("failed to read Generate Social Media checkbox (Notion) for %s: %s", record["name"], e)
        if obsidian_trigger_path:
            try:
                if await asyncio.to_thread(obsidian_sync.is_generate_social_triggered, obsidian_trigger_path):
                    triggered = True
            except Exception as e:
                log.error("failed to read generate_social_media frontmatter (Obsidian) for %s: %s", record["name"], e)
        if not triggered:
            continue

        try:
            _, complete = get_completer()
            # summary intentionally NOT passed -- build_social_post_prompt
            # generates from the raw transcript only, not the already-
            # condensed summary (see its docstring).
            prompt = build_social_post_prompt(record.get("transcript") or "", meeting=record.get("meeting"))
            raw = await asyncio.to_thread(complete, prompt)
            generated = parse_social_post_json(raw)
        except Exception as e:
            log.error("failed to generate social post for %s: %s", record["name"], e)
            continue

        if not generated.get("long_form_body"):
            if notion_trigger_id:
                await asyncio.to_thread(notion_sync.reset_generate_social_trigger, notion_trigger_id)
            if obsidian_trigger_path:
                await asyncio.to_thread(obsidian_sync.reset_generate_social_trigger, obsidian_trigger_path)
            continue

        posts = {}
        # Deterministic signature, not LLM-generated -- guarantees the exact
        # text every time rather than relying on the model to remember it.
        long_form_body = generated["long_form_body"]
        if long_form_body:
            long_form_body = long_form_body.rstrip() + "\n\n— written using Clicky"
        for platform in LONG_FORM_PLATFORMS:
            posts[platform] = {
                "status": "draft", "title": generated["long_form_title"], "body": long_form_body,
                "notion_page_id": None, "scheduled_at": None, "published_at": None, "url": None, "error": None,
            }
        teaser_body = generated["linkedin_teaser"]
        if teaser_body:
            # Signature goes before the {{LONG_FORM_URL}} placeholder (kept
            # on its own trailing line, same as the LLM was told to produce
            # it) so the link stays the last thing in the post either way.
            if "{{LONG_FORM_URL}}" in teaser_body:
                before, _, after = teaser_body.rpartition("{{LONG_FORM_URL}}")
                teaser_body = before.rstrip() + "\n\n— written using Clicky\n{{LONG_FORM_URL}}" + after
            else:
                teaser_body = teaser_body.rstrip() + "\n\n— written using Clicky"
        for platform in TEASER_PLATFORMS:
            posts[platform] = {
                "status": "draft", "title": None, "body": teaser_body,
                "notion_page_id": None, "scheduled_at": None, "published_at": None, "url": None, "error": None,
            }

        record_with_posts = dict(record, social_posts=posts)
        pushed_any = False
        if notion_trigger_id:
            try:
                created = await asyncio.to_thread(notion_sync.push_social_posts, record_with_posts, record.get("notion_page_id"))
                if created.get("notion_page_id"):
                    storage.set_notion_publication_page_id(record["content_hash"], created["notion_page_id"])
                    pushed_any = True
            except Exception as e:
                log.error("failed to push social post drafts for %s to Notion: %s", record["name"], e)
        if obsidian_trigger_path:
            try:
                path = await asyncio.to_thread(obsidian_sync.push_social_posts, record_with_posts, record.get("obsidian_note_path"))
                if path:
                    storage.set_obsidian_publication_note_path(record["content_hash"], path)
                    pushed_any = True
            except Exception as e:
                log.error("failed to push social post drafts for %s to Obsidian: %s", record["name"], e)
        if not pushed_any:
            continue

        storage.set_social_posts(record["content_hash"], posts)
        if notion_trigger_id:
            await asyncio.to_thread(notion_sync.reset_generate_social_trigger, notion_trigger_id)
        if obsidian_trigger_path:
            await asyncio.to_thread(obsidian_sync.reset_generate_social_trigger, obsidian_trigger_path)
        log.info("generated %d social post draft(s) for %s", len(posts), record["name"])
        import analytics
        for platform in posts:
            analytics.track_event("social_posts_generated", key=platform)


async def check_publication_approvals_once():
    """Notion Publications database / Obsidian Publications/ note is the
    approval surface (see notion_sync.push_social_posts / obsidian_sync.
    push_social_posts) -- one page/note per recording, sectioned per
    platform in the body, with an independent property set per platform
    (f"Approve {label}"/f"{label} Scheduled At"/f"{label} Status"/
    f"{label} Post URL" as Notion properties, or approve_{platform}/
    {platform}_scheduled_at/{platform}_status/{platform}_post_url as
    Obsidian frontmatter). Checks whichever backend(s) are configured for
    each record, independently -- a record can have both. Also handles
    Medium's manual-publish path: since it has no publish API, a user
    pasting the real URL into the Post URL field after using Medium's own
    Import-a-story flow is treated the same as an automated publish
    completing -- this is what lets a waiting LinkedIn/X teaser (see
    check_social_publish_once) find a long-form URL to link to."""
    saved = settings.get_all()
    notion_configured = bool(saved.get("notion_publications_database_id"))
    obsidian_configured = bool(saved.get("obsidian_vault_path"))
    if not notion_configured and not obsidian_configured:
        return
    notion_sync = None
    PLATFORM_LABELS = None
    if notion_configured:
        import notion_sync
        from notion_sync import PLATFORM_LABELS
    obsidian_sync = None
    if obsidian_configured:
        import obsidian_sync

    for record in storage.list_recordings():
        posts = record.get("social_posts") or {}
        if not posts:
            continue

        page_id = record.get("notion_publication_page_id") if notion_configured else None
        if page_id:
            try:
                page = await asyncio.to_thread(notion_sync.get_page, page_id)
            except Exception as e:
                log.warning("failed to check Publications page %s (%s): %s", page_id, record["name"], e)
                page = None
            if page is not None:
                props = page.get("properties") or {}
                for platform, post in posts.items():
                    label = PLATFORM_LABELS.get(platform, platform.capitalize())
                    post_url = ((props.get(f"{label} Post URL") or {}).get("url") or "").strip()

                    if platform == "medium":
                        # No auto-publish path exists -- a filled-in Post
                        # URL IS the publish signal, from the user's own
                        # manual import.
                        if post_url and post["status"] != "published":
                            storage.update_social_post(record["content_hash"], platform,
                                                        status="published", url=post_url,
                                                        published_at=datetime.now(timezone.utc).isoformat())
                            await asyncio.to_thread(notion_sync.update_publication_platform_status, page_id, platform, "Published")
                        continue

                    if post["status"] != "draft":
                        continue
                    checked = (props.get(f"Approve {label}") or {}).get("checkbox")
                    if not checked:
                        continue
                    scheduled_at = ((props.get(f"{label} Scheduled At") or {}).get("date") or {}).get("start")
                    storage.update_social_post(
                        record["content_hash"], platform,
                        status="scheduled",
                        scheduled_at=scheduled_at or datetime.now(timezone.utc).isoformat(),
                    )
                    # Without this, the Notion page's own per-platform
                    # Status select stayed on "Draft" forever regardless of
                    # what actually happened -- the local dashboard tracked
                    # "scheduled" internally, but nothing wrote that back
                    # to the surface the user actually looks at.
                    await asyncio.to_thread(notion_sync.update_publication_platform_status, page_id, platform, "Scheduled")

        note_path = record.get("obsidian_publication_note_path") if obsidian_configured else None
        if note_path:
            fm = await asyncio.to_thread(obsidian_sync.read_frontmatter, note_path)
            for platform, post in posts.items():
                post_url = (fm.get(f"{platform}_post_url") or "").strip()

                if platform == "medium":
                    if post_url and post["status"] != "published":
                        storage.update_social_post(record["content_hash"], platform,
                                                    status="published", url=post_url,
                                                    published_at=datetime.now(timezone.utc).isoformat())
                        await asyncio.to_thread(obsidian_sync.update_publication_platform_status, note_path, platform, "published")
                    continue

                if post["status"] != "draft":
                    continue
                checked = fm.get(f"approve_{platform}")
                if not checked:
                    continue
                scheduled_at = fm.get(f"{platform}_scheduled_at") or None
                storage.update_social_post(
                    record["content_hash"], platform,
                    status="scheduled",
                    scheduled_at=scheduled_at or datetime.now(timezone.utc).isoformat(),
                )
                await asyncio.to_thread(obsidian_sync.update_publication_platform_status, note_path, platform, "scheduled")


async def check_social_publish_once():
    """Fires at the scheduled time (same datetime.fromisoformat-vs-now
    pattern as check_meeting_auto_start_once()). Publish ordering matters:
    long-form platforms (Substack -- the only auto-publishable one; Medium
    is always manual, see check_publication_approvals_once) go first, then
    LinkedIn/X teasers substitute the real long-form URL for the
    {{LONG_FORM_URL}} placeholder in their body -- so a teaser scheduled
    for the same moment as its long-form post waits (retried next cycle,
    not failed) until a real URL exists to link to."""
    import linkedin_client
    import x_client
    import substack_client
    import notion_sync
    import obsidian_sync

    now = datetime.now(timezone.utc)

    def _due(post):
        if post["status"] != "scheduled" or not post.get("scheduled_at"):
            return False
        try:
            when = datetime.fromisoformat(post["scheduled_at"].replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return False
        return when <= now

    for record in storage.list_recordings():
        posts = record.get("social_posts") or {}
        if not posts:
            continue
        content_hash = record["content_hash"]

        pub_page_id = record.get("notion_publication_page_id")
        pub_note_path = record.get("obsidian_publication_note_path")

        # Long-form first (Substack only -- Medium never auto-publishes).
        substack_post = posts.get("substack")
        if substack_post and _due(substack_post):
            try:
                url = await asyncio.to_thread(substack_client.post, substack_post["title"], substack_post["body"])
                storage.update_social_post(content_hash, "substack", status="published", url=url,
                                            published_at=now.isoformat(), error=None)
                import analytics
                analytics.track_event("social_posts_published", key="substack")
                if pub_page_id:
                    await asyncio.to_thread(notion_sync.update_publication_platform_status,
                                             pub_page_id, "substack", "Published", url)
                if pub_note_path:
                    await asyncio.to_thread(obsidian_sync.update_publication_platform_status,
                                             pub_note_path, "substack", "published", url)
            except Exception as e:
                log.warning("Substack publish failed for %s: %s", record["name"], e)
                storage.update_social_post(content_hash, "substack", error=str(e)[:500])
                if pub_page_id:
                    await asyncio.to_thread(notion_sync.update_publication_platform_status,
                                             pub_page_id, "substack", "Failed")
                if pub_note_path:
                    await asyncio.to_thread(obsidian_sync.update_publication_platform_status,
                                             pub_note_path, "substack", "failed")

        # Re-fetch: the Substack publish above may have just set a URL this
        # same cycle, and the teaser below should see it immediately rather
        # than waiting a full extra poll cycle.
        record = storage.get_recording(content_hash) or record
        posts = record.get("social_posts") or {}
        long_form_url = None
        for platform in LONG_FORM_PLATFORMS:
            p = posts.get(platform)
            if p and p.get("status") == "published" and p.get("url"):
                long_form_url = p["url"]
                break

        for platform, client in (("linkedin", linkedin_client), ("x", x_client)):
            post = posts.get(platform)
            if not post or not _due(post):
                continue
            if not long_form_url:
                storage.update_social_post(
                    content_hash, platform,
                    error="waiting for the long-form post (Substack/Medium) to be published first")
                continue
            text = (post.get("body") or "").replace("{{LONG_FORM_URL}}", long_form_url)
            try:
                url = await asyncio.to_thread(client.post, text)
                storage.update_social_post(content_hash, platform, status="published", url=url,
                                            published_at=now.isoformat(), error=None)
                import analytics
                analytics.track_event("social_posts_published", key=platform)
                if pub_page_id:
                    await asyncio.to_thread(notion_sync.update_publication_platform_status,
                                             pub_page_id, platform, "Published", url)
                if pub_note_path:
                    await asyncio.to_thread(obsidian_sync.update_publication_platform_status,
                                             pub_note_path, platform, "published", url)
            except Exception as e:
                log.warning("%s publish failed for %s: %s", platform, record["name"], e)
                storage.update_social_post(content_hash, platform, error=str(e)[:500])
                if pub_page_id:
                    await asyncio.to_thread(notion_sync.update_publication_platform_status,
                                             pub_page_id, platform, "Failed")
                if pub_note_path:
                    await asyncio.to_thread(obsidian_sync.update_publication_platform_status,
                                             pub_note_path, platform, "failed")


def _format_usage_digest(summary: dict) -> str:
    saved = settings.get_all()
    owner_name = saved.get("owner_name") or "(not set -- add it in Settings -> Account)"
    lines = [
        f"Owner: {owner_name}",
        f"Device ID: {settings.get_or_create_device_id()}",
        "",
        "Clicky usage summary since " + (summary.get("period_start") or "install") + ":",
        "",
        f"Recordings: {summary.get('recordings_count', 0)} "
        f"(journal: {summary.get('recordings_journal_count', 0)}, "
        f"actionable: {summary.get('recordings_actionable_count', 0)})",
        f"Total recording time: {summary.get('total_recording_seconds', 0) / 60:.1f} min",
    ]
    stt = summary.get("stt_provider_counts") or {}
    if stt:
        lines.append("Transcription provider usage: " + ", ".join(f"{k}={v}" for k, v in stt.items()))
    llm = summary.get("llm_provider_counts") or {}
    if llm:
        lines.append("Summarization provider usage: " + ", ".join(f"{k}={v}" for k, v in llm.items()))
    lines.append(f"Pushed to Notion: {summary.get('notion_pushes', 0)}, Obsidian: {summary.get('obsidian_pushes', 0)}")
    generated = summary.get("social_posts_generated") or {}
    published = summary.get("social_posts_published") or {}
    if generated or published:
        lines.append("Social posts generated: " + ", ".join(f"{k}={v}" for k, v in generated.items()) if generated else "Social posts generated: 0")
        lines.append("Social posts published: " + ", ".join(f"{k}={v}" for k, v in published.items()) if published else "Social posts published: 0")
    lines.append(f"Notifications sent to device: {summary.get('notifications_sent', 0)}")
    lines.append(f"Email drafts approved & sent: {summary.get('drafts_approved', 0)}")
    if summary.get("recordings_deleted") or summary.get("recordings_deleted_from_device"):
        lines.append(f"Recordings deleted: {summary.get('recordings_deleted', 0)} "
                      f"(from device: {summary.get('recordings_deleted_from_device', 0)})")
    if summary.get("people_contact_added"):
        lines.append(f"Speaker/stakeholder contact info added: {summary['people_contact_added']}")
    if summary.get("people_merged"):
        lines.append(f"Duplicate People pages merged: {summary['people_merged']}")
    if summary.get("feedback_submitted_count"):
        lines.append(f"Feedback submitted: {summary['feedback_submitted_count']}")
    return "\n".join(lines)


async def check_usage_report_once():
    """Emails a usage digest to the developer's own address once per
    calendar day (see analytics.py's module docstring -- this is the
    user's own visibility into their own device, not third-party
    telemetry). Gated on wall-clock date rather than an interval timer,
    unlike poller.py's other _last_*-style gates, since "once a day"
    doesn't mean "every 86400s after the app happened to start" -- it
    should fire once real-world-calendar-day regardless of uptime.

    No dedicated "is the Mac online" check exists -- send_email() simply
    fails if there's no internet, and reset_period() (which also records
    last_report_date) is only called on success, so a failed attempt
    naturally retries on the very next poll cycle rather than waiting
    until tomorrow. This satisfies "send when it connects to WiFi" without
    a separate connectivity probe: the retry-until-success behavior IS the
    connectivity gate.
    """
    if not google_client.is_connected():
        return
    import analytics
    today = datetime.now().date().isoformat()
    if analytics.last_report_date() == today:
        return
    summary = analytics.get_period_summary()
    if summary.get("recordings_count", 0) == 0 and not any(
        summary.get(k) for k in ("notifications_sent", "drafts_approved", "notion_pushes", "obsidian_pushes")
    ):
        # Nothing happened today -- still mark it sent so this doesn't
        # retry every poll cycle for an empty report, but skip the email.
        analytics.reset_period(today)
        return
    try:
        await asyncio.to_thread(
            google_client.send_email,
            ["sanchit.gupta01@gmail.com"],
            "Clicky usage digest",
            _format_usage_digest(summary),
        )
    except Exception as e:
        log.warning("usage digest email failed (will retry next poll cycle): %s", e)
        return
    analytics.reset_period(today)


def check_notifications_once():
    """AI-pager push feed (Gmail/calendar -> device). Synchronous and
    best-effort -- a slow/broken source here must never block sync/
    transcription, so any failure is swallowed inside notifications.py
    itself, not here.

    Always routed over BLE, regardless of the configured sync_transport --
    the firmware now gates its WiFi radio off except during a bounded sync
    session (battery, see wifi_sync.cpp), so a notification riding the
    "wifi" transport would fail almost all the time once WiFi is the active
    sync_transport. BLE stays continuously advertising/connectable whenever
    the device is paired (slow-interval reconnect, see ble_sync.cpp), which
    is exactly what a notification needs regardless of which transport is
    moving audio files. notifications._push() only marks a notification
    "seen" once send_notification() actually succeeds, so a failed push
    here (device asleep/out of range) is retried automatically on the next
    poll cycle -- no separate queue needed."""
    import ble_device_client
    try:
        notifications.check_once(ble_device_client)
    except Exception as e:
        log.warning("notifications check failed: %s", e)


async def poll_forever():
    while True:
        await poll_once()
        await asyncio.sleep(config.POLL_INTERVAL_SECONDS)
