import asyncio
import logging
import os
import re
import urllib.parse
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import BackgroundTasks, FastAPI, Request, Form
from fastapi import Response
from fastapi.responses import JSONResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

import secrets

import paths
import config
import google_client
import linkedin_client
import x_client
import substack_client
import meeting_recorder
import settings
import status
import storage
import poller
from poller import poll_forever
import voice_id
import update_check

import os as _os
# The packaged .app is launched via `open`/Launch Services with no attached
# terminal, so plain stdout (basicConfig's default) goes nowhere anyone can
# read -- a background-task exception (e.g. voice-ID enrollment failing
# silently after a rename) was previously undiagnosable outside a dev venv.
# A real file next to the rest of the pipeline's persistent data fixes that.
_log_path = _os.path.join(paths.APP_DATA_DIR, "clicky.log")
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(_log_path)],
)
log = logging.getLogger("app")

PROVIDERS_NEEDING_KEY = {
    "mistral": "mistral_api_key",
    "openai": "openai_api_key",
    "anthropic": "anthropic_api_key",
    "deepgram": "deepgram_api_key",
    "local": None,  # Ollama/faster-whisper — no cloud key needed
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    meeting_recorder.launch()  # persistent menu-bar agent, lives for the app's lifetime
    task = asyncio.create_task(poll_forever())
    # Backfill: process_once() only ever computes a recording's voice-ID
    # suggestions/candidates once, at original processing time (see its
    # docstring on rescan_voice_suggestions) -- otherwise a recording
    # transcribed before the matching voiceprint existed (or before its
    # score happened to clear a threshold) shows "no voice match" forever,
    # even after later enrollments should have fixed it. Previously this
    # only re-ran right after a fresh enroll_or_update() call; nothing ever
    # retried it for the app's entire existing history on its own.
    asyncio.create_task(poller.rescan_voice_suggestions())
    yield
    task.cancel()
    meeting_recorder.shutdown()


app = FastAPI(lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=settings.session_secret())
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Device Fleet is for the app author only, not every Clicky user -- gated on
# a specific configured owner_email (Settings -> Account) rather than just
# the app's regular login, since every install has its own independent
# password. Registered as a Jinja global (not threaded through every route's
# context dict) so _nav.html can call is_admin() directly regardless of
# which route rendered the page.
ADMIN_EMAIL = "sanchit.gupta01@gmail.com"


def _is_admin() -> bool:
    return (settings.get_all().get("owner_email") or "").strip().lower() == ADMIN_EMAIL


templates.env.globals["is_admin"] = _is_admin


def _gate(request: Request):
    """Returns a redirect if this request shouldn't proceed, else None.
    Call at the top of every protected route."""
    if not settings.is_configured():
        return RedirectResponse("/setup", status_code=303)
    if not request.session.get("authenticated"):
        return RedirectResponse("/login", status_code=303)
    return None


def _recordings_for_display():
    """storage.list_recordings() plus a "merged_segments" field per record --
    consecutive same-speaker segments combined (see
    providers.base.merge_consecutive_segments) so the transcript displays
    as coherent per-turn blocks instead of choppy diarization fragments.
    Computed here (not stored) so storage.json keeps the raw per-segment
    data speaker-rename and other features rely on; both the initial page
    render and the JS poll (/api/recordings) use this same shared
    computation so they never drift out of sync with each other."""
    from providers.base import merge_consecutive_segments
    records = storage.list_recordings()
    enabled = voice_id.is_enabled()
    vault_path = settings.get_all().get("obsidian_vault_path")
    for r in records:
        r["merged_segments"] = merge_consecutive_segments(r.get("segments"))
        r["voice_id_enabled"] = enabled

        # Direct links to the pushed-to destination, if any -- journal-type
        # recordings use the *_journal_* fields instead of the plain ones
        # (see storage.py's field docstrings), never both at once.
        page_id = r.get("notion_page_id") or r.get("notion_journal_page_id")
        r["notion_url"] = f"https://www.notion.so/{page_id.replace('-', '')}" if page_id else None

        note_path = r.get("obsidian_note_path") or r.get("obsidian_journal_note_path")
        if note_path and vault_path:
            # obsidian:// deep links address a vault by its display name,
            # which defaults to (and is only reliably knowable as) the
            # vault folder's own basename unless the user renamed it inside
            # Obsidian itself -- a reasonable default, not a guarantee.
            # relpath uses this OS's separator ("\" on Windows) -- Obsidian's
            # URI scheme expects "/" between subfolders regardless of OS.
            vault_name = os.path.basename(os.path.normpath(vault_path))
            rel_path = os.path.relpath(note_path, vault_path).replace(os.sep, "/")
            r["obsidian_url"] = (
                f"obsidian://open?vault={urllib.parse.quote(vault_name)}"
                f"&file={urllib.parse.quote(rel_path)}"
            )
        else:
            r["obsidian_url"] = None
    return records


@app.get("/")
def index(request: Request):
    redirect = _gate(request)
    if redirect:
        return redirect
    # Jarvis voice commands get their own page (see /jarvis) -- they aren't
    # a recording in the memo/journal sense (no speakers/summary/action
    # items), so they no longer render inline in the main list here.
    # merged_into: absorbed into another recording by backlog #5's
    # continuation merge (see poller.merge_continuations_once) -- its
    # content now lives on the keeper record, so it has nothing left of
    # its own to show.
    recordings = [r for r in _recordings_for_display() if r.get("kind") != "command" and not r.get("merged_into")]
    return templates.TemplateResponse(
        request,
        "index.html",
        {"recordings": recordings, "sync_transport": config.SYNC_TRANSPORT, "active_nav": "transcription",
         "voice_id_enabled": voice_id.is_enabled()},
    )


@app.get("/api/recordings")
def api_recordings(request: Request):
    redirect = _gate(request)
    if redirect:
        return redirect
    return JSONResponse([r for r in _recordings_for_display() if r.get("kind") != "command" and not r.get("merged_into")])


@app.get("/audio/{content_hash}.wav")
def get_audio(request: Request, content_hash: str):
    redirect = _gate(request)
    if redirect:
        return redirect
    wav_path = storage.get_wav_path(content_hash)
    if not wav_path:
        return JSONResponse({"error": "not found"}, status_code=404)
    # No `filename` (so no Content-Disposition header) for the default
    # inline-playback case -- the dashboard's <audio> player hits this same
    # URL, and forcing "attachment" there risks some browsers refusing to
    # play it inline. ?download=1 (the download button/link) opts into a
    # real filename + attachment disposition so "Save As" offers something
    # readable instead of a bare content-hash.
    if request.query_params.get("download"):
        record = storage.get_recording(content_hash)
        name = (record.get("name") if record else None) or content_hash
        if not name.lower().endswith(".wav"):
            name += ".wav"
        return FileResponse(wav_path, media_type="audio/wav", filename=name)
    return FileResponse(wav_path, media_type="audio/wav")


@app.delete("/recordings/{content_hash}")
def delete_recording_route(request: Request, content_hash: str):
    redirect = _gate(request)
    if redirect:
        return redirect
    deleted = storage.delete_recording(content_hash)
    if deleted:
        import analytics
        analytics.track_event("recordings_deleted")
    return JSONResponse({"deleted": deleted}, status_code=200 if deleted else 404)


@app.delete("/recordings/{content_hash}/device")
def delete_recording_from_device_route(request: Request, content_hash: str):
    """Real, irreversible delete — also erases the file from the device's
    SD card (see storage.delete_recording_from_device()), unlike the plain
    delete above which only removes the local copy. The dashboard should
    ask for confirmation before calling this."""
    redirect = _gate(request)
    if redirect:
        return redirect
    result = storage.delete_recording_from_device(content_hash)
    status_code = 200 if result["deleted_locally"] else 404
    if result["deleted_locally"]:
        import analytics
        analytics.track_event("recordings_deleted_from_device")
    return JSONResponse(result, status_code=status_code)


@app.post("/recordings/{content_hash}/speakers/{speaker_id}")
def rename_speaker(request: Request, content_hash: str, speaker_id: str, name: str = Form(""), background_tasks: BackgroundTasks = None):
    redirect = _gate(request)
    if redirect:
        return redirect
    ok = storage.set_speaker_name(content_hash, speaker_id, name.strip())
    if ok:
        # Re-summarizing (so Summary/Stakeholders prose picks up the new
        # name too, not just the Transcript block -- see
        # poller.resync_after_rename) means an LLM call plus rewriting the
        # whole Notion page; running it as a background task keeps this
        # response fast and means a Notion hiccup can't fail the rename,
        # which has already succeeded locally by this point.
        background_tasks.add_task(poller.resync_after_rename, content_hash)
    return JSONResponse({"ok": ok}, status_code=200 if ok else 404)


@app.post("/recordings/{content_hash}/drafts/{draft_id}/approve")
def approve_draft(request: Request, content_hash: str, draft_id: str, to: str = Form("")):
    """Thin wrapper around poller.approve_and_send_draft() -- the actual
    Gmail/Tasks/Calendar dispatch logic lives there so it's shared with
    poller.check_notion_email_approvals_once() (Notion's "Approve & Send"
    checkbox is now the primary approval surface; this dashboard route is
    a fallback). `to` is only used for an email draft whose recipient
    couldn't be resolved automatically (see poller.py's
    _build_email_drafts/_lookup_email_for_name) -- the dashboard prompts
    for an address inline before enabling Approve in that case and
    submits it here; ignored for drafts that already have a recipient."""
    redirect = _gate(request)
    if redirect:
        return redirect
    result = poller.approve_and_send_draft(content_hash, draft_id, to_override=to.strip())
    return JSONResponse(result["body"], status_code=result["status_code"])


@app.post("/recordings/{content_hash}/drafts/{draft_id}/dismiss")
def dismiss_draft(request: Request, content_hash: str, draft_id: str):
    redirect = _gate(request)
    if redirect:
        return redirect
    ok = storage.update_draft(content_hash, draft_id, status="dismissed")
    return JSONResponse({"ok": ok}, status_code=200 if ok else 404)


@app.post("/recordings/{content_hash}/jarvis-status")
def set_jarvis_status(request: Request, content_hash: str, status: str = Form(...)):
    """Marks a Jarvis command done/discarded/pending from the dashboard --
    a user-disposition field distinct from jarvis_result["ok"] (whether
    the action itself executed successfully). One route, not a pair like
    approve/dismiss above: unlike email drafts, none of these three values
    trigger any dispatch logic, so there's no asymmetry worth documenting
    with separate routes."""
    redirect = _gate(request)
    if redirect:
        return redirect
    if status not in ("pending", "done", "discarded"):
        return JSONResponse({"error": "invalid_status"}, status_code=400)
    ok = storage.set_jarvis_user_status(content_hash, status)
    return JSONResponse({"ok": ok}, status_code=200 if ok else 404)


@app.post("/recordings/{keeper_hash}/unmerge/{loser_hash}")
async def unmerge_recordings(request: Request, keeper_hash: str, loser_hash: str):
    """Splits a continuation-merged pair back apart (see backlog #5,
    poller.merge_continuations_once) -- storage.unmerge_recording()
    restores the keeper's exact pre-merge transcript and clears the
    loser's synced flags; re-summarizing and re-pushing the keeper is
    this route's job, not storage's (same division as merge_recordings()).
    The loser needs no push here -- clearing its *_synced flags is enough
    for distribute_once() to push it as its own document on the next poll
    cycle, same as any freshly processed recording."""
    redirect = _gate(request)
    if redirect:
        return redirect
    ok = storage.unmerge_recording(keeper_hash, loser_hash)
    if not ok:
        return JSONResponse({"ok": False}, status_code=404)

    from providers import get_summarizer
    keeper = storage.get_recording(keeper_hash)
    _, summarize = get_summarizer()
    restored_summary = await asyncio.to_thread(
        summarize, keeper["transcript"], keeper.get("deepgram_insights"), keeper.get("meeting"))
    poller._enforce_journal_rule(restored_summary, keeper.get("segments") or [])
    poller._add_speakers_as_stakeholders(restored_summary)
    storage.update_summary(keeper_hash, restored_summary)

    keeper = storage.get_recording(keeper_hash)
    if keeper.get("notion_page_id"):
        import notion_sync
        try:
            await asyncio.to_thread(notion_sync.update_all_blocks, keeper["notion_page_id"], keeper)
        except Exception as e:
            logging.getLogger("app").error("unmerged %s locally but failed to refresh Notion page: %s", keeper["name"], e)
    if keeper.get("obsidian_note_path"):
        import obsidian_sync
        try:
            await asyncio.to_thread(obsidian_sync.push_recording, keeper)
        except Exception as e:
            logging.getLogger("app").error("unmerged %s locally but failed to refresh Obsidian note: %s", keeper["name"], e)

    return JSONResponse({"ok": True})


@app.get("/api/pending-person-links")
def api_pending_person_links(request: Request):
    """Task/Calendar entries created with a name (task owner, email draft
    recipient) that matched an ambiguous or unconfirmed set of existing
    People pages -- see notion_sync.resolve_person_for_relation. Polled by
    the dashboard to show a small "Confirm who this is" queue."""
    redirect = _gate(request)
    if redirect:
        return redirect
    return JSONResponse(storage.list_pending_person_links())


@app.post("/api/pending-person-links/{link_id}/resolve")
def resolve_pending_person_link_route(
    request: Request, link_id: str,
    person_page_id: str = Form(""), new_person_name: str = Form(""),
):
    """Applies the user's choice from the confirmation queue -- either an
    existing People page id, or a brand-new person's name if none of the
    candidates were actually them."""
    redirect = _gate(request)
    if redirect:
        return redirect
    link = storage.remove_pending_person_link(link_id)
    if link is None:
        return JSONResponse({"error": "not_found"}, status_code=404)
    import notion_sync
    # A link can have more than one target page (see
    # storage.add_pending_person_link's dedup -- the same ambiguous name
    # showing up in a Task and a Calendar entry for one recording shares
    # a single confirmation) -- resolve_pending_person_link patches all of
    # them with the one person the user just picked/created.
    page_id_to_kind = dict(zip(link["notion_page_ids"], link["database_kinds"]))
    try:
        people_database_id = settings.get_all().get("notion_people_database_id")
        _, failed = notion_sync.resolve_pending_person_link(
            link["notion_page_ids"],
            person_page_id=person_page_id or None,
            new_person_name=new_person_name or None,
            people_database_id=people_database_id,
        )
    except Exception as e:
        # Couldn't even resolve/create the person itself (e.g. Notion
        # hiccup before any page got patched) -- re-register the whole
        # link so it isn't silently lost; the user can just retry.
        for page_id, kind in page_id_to_kind.items():
            storage.add_pending_person_link(link["name"], page_id, kind, link["candidates"], link["recording_name"])
        return JSONResponse({"error": str(e)}, status_code=502)

    for page_id in failed:
        # This specific target page failed transiently (not 404 -- those
        # are already skipped for good inside resolve_pending_person_link)
        # -- re-register just it, not the ones that already succeeded.
        storage.add_pending_person_link(
            link["name"], page_id, page_id_to_kind.get(page_id), link["candidates"], link["recording_name"])
    if failed:
        return JSONResponse({"error": "partial_failure", "failed_count": len(failed)}, status_code=502)
    return JSONResponse({"ok": True})


@app.get("/api/status")
def api_status(request: Request):
    redirect = _gate(request)
    if redirect:
        return redirect
    return JSONResponse({
        **status.get_all(),
        "sync_transport": config.SYNC_TRANSPORT,
        # Manual recording control lives in the menu-bar agent, not the
        # dashboard -- this is exposed so the dashboard can still show
        # "recording in progress" passively (and for a possible future
        # browser-based control surface), without the dashboard itself
        # being how you start/stop a meeting recording.
        "meeting": meeting_recorder.state(),
    })


@app.post("/meeting/start")
def meeting_start(request: Request):
    # Not used by the dashboard UI (manual control is the menu-bar icon) --
    # kept as an API surface for Phase B's calendar auto-detect and any
    # future browser-based control widget.
    redirect = _gate(request)
    if redirect:
        return redirect
    result = meeting_recorder.start()
    if result.get("ok"):
        return JSONResponse(result)
    code = 409 if result.get("error") == "already_recording" else 500
    return JSONResponse(result, status_code=code)


@app.post("/meeting/stop")
def meeting_stop(request: Request):
    redirect = _gate(request)
    if redirect:
        return redirect
    result = meeting_recorder.stop()
    if result.get("ok"):
        return JSONResponse(result)
    code = 409 if result.get("error") == "not_recording" else 500
    return JSONResponse(result, status_code=code)


@app.get("/setup")
def setup_form(request: Request):
    # Editing settings later requires being logged in; first-run setup
    # (no password stored yet) is open since there's nothing to protect yet.
    if settings.is_configured() and not request.session.get("authenticated"):
        return RedirectResponse("/login", status_code=303)
    if settings.is_configured():
        # /settings is the real settings surface post-first-run (sidebar
        # panels: Device/Providers/Account) -- this wizard is only for the
        # initial unauthenticated run. Its POST route stays live since
        # settings.html's Providers/Account panels submit to it directly.
        return RedirectResponse("/settings", status_code=303)
    return templates.TemplateResponse(request, "setup.html", {"current": settings.get_all(), "error": None})


@app.post("/setup")
def setup_submit(
    request: Request,
    password: str = Form(""),
    owner_name: str = Form(""),
    owner_email: str = Form(""),
    stt_provider: str = Form(...),
    llm_provider: str = Form(...),
    sync_transport: str = Form(...),
    mistral_api_key: str = Form(""),
    openai_api_key: str = Form(""),
    anthropic_api_key: str = Form(""),
    deepgram_api_key: str = Form(""),
):
    if settings.is_configured() and not request.session.get("authenticated"):
        return RedirectResponse("/login", status_code=303)

    # A key is required the first time a provider is chosen, but re-saving
    # settings later shouldn't force re-entering keys already on file.
    current = settings.get_all()
    for provider in {stt_provider, llm_provider}:
        key_field = PROVIDERS_NEEDING_KEY.get(provider)
        if key_field is None:
            continue
        new_value = {"mistral_api_key": mistral_api_key, "openai_api_key": openai_api_key,
                     "anthropic_api_key": anthropic_api_key, "deepgram_api_key": deepgram_api_key}[key_field]
        if not new_value and not current.get(key_field):
            error = f"'{provider}' needs an API key — enter one below."
            return templates.TemplateResponse(request, "setup.html", {"current": current, "error": error})

    updates = {
        "stt_provider": stt_provider,
        "llm_provider": llm_provider,
        "sync_transport": sync_transport,
        # Only overwrite if provided -- same "blank means keep current" rule
        # as the API key fields above, so resubmitting the Account panel
        # (which round-trips owner_name through a visible, prefilled input,
        # not a masked one) doesn't accidentally require retyping it.
        "owner_name": owner_name or current.get("owner_name"),
        "owner_email": owner_email or current.get("owner_email"),
        "mistral_api_key": mistral_api_key or None,
        "openai_api_key": openai_api_key or None,
        "anthropic_api_key": anthropic_api_key or None,
        "deepgram_api_key": deepgram_api_key or None,
    }
    if password:
        updates["password_hash"] = settings.hash_password(password)
    elif not current.get("password_hash"):
        error = "Set a password to protect this page."
        return templates.TemplateResponse(request, "setup.html", {"current": current, "error": error})

    settings.update(**updates)
    config.reload_settings()
    request.session["authenticated"] = True
    return RedirectResponse("/", status_code=303)


@app.get("/login")
def login_form(request: Request):
    if request.session.get("authenticated"):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login")
def login_submit(request: Request, password: str = Form(...)):
    stored = settings.get_all().get("password_hash", "")
    if stored and settings.verify_password(password, stored):
        request.session["authenticated"] = True
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": "Wrong password."})


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/pair")
async def pair_form(request: Request, next: str = "/"):
    redirect = _gate(request)
    if redirect:
        return redirect
    if config.SYNC_TRANSPORT != "ble":
        return templates.TemplateResponse(
            request, "pair.html",
            {"devices": [], "error": "Pairing only applies when Sync Transport is set to BLE (see Settings → Device).", "current_address": None, "next": next},
        )
    import ble_device_client
    try:
        # discover_devices_with_diagnostics() is a plain sync function --
        # internally it already blocks on the BLE scan via _run_coro() and
        # returns the (devices, total_seen) tuple directly, not a
        # coroutine. Awaiting it raised "object tuple can't be used in
        # 'await' expression".
        devices, total_seen = ble_device_client.discover_devices_with_diagnostics()
        if devices:
            error = None
        elif total_seen == 0:
            error = ("Bluetooth scan saw zero devices of any kind — this usually means macOS hasn't granted "
                      "Bluetooth permission to this app. Check System Settings → Privacy & Security → "
                      "Bluetooth, then reload this page.")
        else:
            error = (f"Bluetooth scan saw {total_seen} other device(s) nearby, but none advertising as "
                      "\"EpaperTranscriber*\" — make sure the device is powered on, in range, and running "
                      "firmware with BLE sync enabled, then reload this page.")
    except Exception as e:
        devices, error = [], str(e)
    return templates.TemplateResponse(
        request, "pair.html",
        {"devices": devices, "error": error, "current_address": config.PAIRED_BLE_ADDRESS, "next": next},
    )


@app.post("/pair")
def pair_submit(request: Request, address: str = Form(...), next: str = Form("/")):
    redirect = _gate(request)
    if redirect:
        return redirect
    settings.update(paired_ble_address=address)
    config.reload_settings()
    return RedirectResponse(next, status_code=303)


@app.post("/settings/ble/forget")
def settings_ble_forget(request: Request, next: str = Form("/settings?panel=device")):
    """Clears the stored paired_ble_address entirely (settings.delete(), not
    update(paired_ble_address=None) -- see settings.py's docstring on why
    those aren't equivalent). Needed because a stale address from a
    previously-paired device (e.g. swapped for a new physical unit) just
    sits there forever otherwise -- pairing a NEW device overwrites it fine,
    but there was previously no way to just go back to "not paired" without
    pairing something else first."""
    redirect = _gate(request)
    if redirect:
        return redirect
    settings.delete("paired_ble_address")
    config.reload_settings()
    return RedirectResponse(next, status_code=303)


@app.get("/settings")
def settings_form(request: Request, panel: str = "device", wifi_msg: str = ""):
    redirect = _gate(request)
    if redirect:
        return redirect
    return templates.TemplateResponse(request, "settings.html", _settings_context(panel, wifi_msg))


@app.post("/settings/transport")
def settings_transport(request: Request, sync_transport: str = Form(...)):
    redirect = _gate(request)
    if redirect:
        return redirect
    settings.update(sync_transport=sync_transport)
    config.reload_settings()
    return RedirectResponse("/settings?panel=device", status_code=303)


@app.post("/settings/audio")
def settings_audio(request: Request, filter_background_conversations: str = Form("")):
    """Toggles audio_analysis.py's volume-based background-conversation
    filtering (see poller.process_once). A plain checkbox, kept separate
    from the /setup route since that one's shared by first-run setup and
    the Account panel's own hidden-field forms."""
    redirect = _gate(request)
    if redirect:
        return redirect
    settings.update(filter_background_conversations=bool(filter_background_conversations))
    return RedirectResponse("/settings?panel=providers", status_code=303)


@app.post("/settings/notifications")
def settings_notifications(
    request: Request,
    notify_gmail_enabled: bool = Form(False),
    notify_calendar_enabled: bool = Form(False),
    notify_mac_enabled: bool = Form(False),
    notification_sensitivity: str = Form("medium"),
    notification_vip_list: str = Form(""),
):
    redirect = _gate(request)
    if redirect:
        return redirect
    settings.update(
        notify_gmail_enabled=notify_gmail_enabled,
        notify_calendar_enabled=notify_calendar_enabled,
        notify_mac_enabled=notify_mac_enabled,
        notification_sensitivity=notification_sensitivity,
        notification_vip_list=notification_vip_list,
    )
    config.reload_settings()
    return RedirectResponse("/settings?panel=notifications", status_code=303)


@app.post("/settings/conversation-merge")
def settings_conversation_merge(
    request: Request,
    merge_continuations_enabled: bool = Form(False),
    merge_gap_minutes: int = Form(10),
):
    redirect = _gate(request)
    if redirect:
        return redirect
    settings.update(
        merge_continuations_enabled=merge_continuations_enabled,
        merge_gap_minutes=merge_gap_minutes,
    )
    return RedirectResponse("/settings?panel=conversation-merge", status_code=303)


@app.post("/settings/jarvis")
def settings_jarvis(
    request: Request,
    jarvis_enabled: bool = Form(False),
    jarvis_repo_path: str = Form(""),
):
    """jarvis_repo_path is only used by the code_task action (always the
    real Claude Code CLI, invoked with cwd=jarvis_repo_path -- see
    jarvis.py's _action_code_task) -- everything else Jarvis does (Q&A,
    Calendar/Reminders/Mail automation) needs no path configured."""
    redirect = _gate(request)
    if redirect:
        return redirect
    settings.update(
        jarvis_enabled=jarvis_enabled,
        jarvis_repo_path=jarvis_repo_path.strip() or None,
    )
    return RedirectResponse("/settings?panel=jarvis", status_code=303)


@app.post("/settings/voice-id")
def settings_voice_id(request: Request, voice_id_enabled: bool = Form(False)):
    redirect = _gate(request)
    if redirect:
        return redirect
    settings.update(voice_id_enabled=voice_id_enabled)
    config.reload_settings()
    return RedirectResponse("/settings?panel=voice-id", status_code=303)


@app.post("/settings/voice-id/forget")
def settings_voice_id_forget(request: Request, person_key: str = Form(...)):
    redirect = _gate(request)
    if redirect:
        return redirect
    voice_id.delete_voiceprint(person_key)
    return RedirectResponse("/settings?panel=voice-id", status_code=303)


@app.post("/settings/voice-id/forget-all")
def settings_voice_id_forget_all(request: Request):
    redirect = _gate(request)
    if redirect:
        return redirect
    voice_id.delete_all_voiceprints()
    return RedirectResponse("/settings?panel=voice-id", status_code=303)


@app.post("/settings/custom-statuses")
async def settings_custom_statuses(request: Request):
    redirect = _gate(request)
    if redirect:
        return redirect
    # Repeated <select name="icon"> / <input name="text"> pairs, one per
    # row -- parsed positionally (row i's icon pairs with row i's text),
    # same shape the add/remove-row JS in settings.html builds. A row left
    # blank (no text typed) is dropped rather than synced as an empty
    # custom status.
    form = await request.form()
    icons = form.getlist("icon")
    texts = form.getlist("text")
    statuses = [{"icon": icon, "text": text.strip()} for icon, text in zip(icons, texts) if text.strip()]
    settings.update(custom_statuses=statuses)
    config.reload_settings()

    import ble_device_client
    try:
        ble_device_client.send_custom_statuses(statuses)
        msg = "Saved and sent to device."
    except Exception as e:
        msg = f"Saved locally, but failed to reach device over BLE: {e}. It'll pick up the change next time you resave once reconnected."
    return RedirectResponse(f"/settings?panel=custom-statuses&wifi_msg={msg}", status_code=303)


@app.post("/settings/custom-statuses/clear")
async def settings_custom_statuses_clear(request: Request):
    redirect = _gate(request)
    if redirect:
        return redirect
    settings.update(custom_statuses=[])
    config.reload_settings()

    import ble_device_client
    try:
        ble_device_client.send_custom_statuses([])
        msg = "Cleared and synced to device."
    except Exception as e:
        msg = f"Cleared locally, but failed to reach device over BLE: {e}. It'll pick up the change next time you resave once reconnected."
    return RedirectResponse(f"/settings?panel=custom-statuses&wifi_msg={msg}", status_code=303)


@app.post("/settings/wifi/connect")
def settings_wifi_connect(request: Request, ssid: str = Form(...), password: str = Form("")):
    redirect = _gate(request)
    if redirect:
        return redirect
    # BLE is backup-only now (see ble_sync.cpp's resumeIdleAdvertising() --
    # it stops idle-advertising while WiFi is connected), so reach the
    # device over its own WiFi HTTP server whenever that's actually
    # reachable, same as every other device call already does (see
    # poller._get_transport()); BLE is the fallback for "device isn't on
    # WiFi yet at all," which is exactly the case this route also needs to
    # keep working for (first-time setup).
    wifi_url = poller.wifi_base_url_if_reachable()
    try:
        if wifi_url:
            import device_client
            device_client.set_wifi_credentials(ssid, password)
        else:
            import ble_device_client
            ble_device_client.set_wifi_credentials(ssid, password)
        msg = f"Sent -- device is connecting to \"{ssid}\". Reload in a few seconds to check status."
    except Exception as e:
        transport = "WiFi" if wifi_url else "BLE"
        msg = f"Failed to reach device over {transport}: {e}"
    return RedirectResponse(f"/settings?panel=device&wifi_msg={msg}", status_code=303)


@app.get("/api/wifi-status")
def api_wifi_status(request: Request):
    redirect = _gate(request)
    if redirect:
        return redirect
    wifi_url = poller.wifi_base_url_if_reachable()
    try:
        if wifi_url:
            import device_client
            return device_client.get_wifi_status()
        import ble_device_client
        return ble_device_client.get_wifi_status()
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=503)


@app.post("/api/wifi-scan")
def api_wifi_scan(request: Request):
    redirect = _gate(request)
    if redirect:
        return redirect
    wifi_url = poller.wifi_base_url_if_reachable()
    try:
        if wifi_url:
            import device_client
            return {"networks": device_client.scan_wifi_networks()}
        import ble_device_client
        return {"networks": ble_device_client.scan_wifi_networks()}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=503)


def _has_active_network_route() -> bool:
    """True if this machine has a working default route right now (i.e. is
    online via *some* interface) -- independent of WiFi/SSID visibility,
    used to distinguish "genuinely not on WiFi" from "the OS is hiding the
    SSID" below. Portable across macOS/Windows/Linux on purpose: opening a
    UDP socket "connect" never actually sends a packet (UDP is
    connectionless) but does make the OS pick a real outbound route/local
    IP if one exists, which is exactly what's needed here -- no
    platform-specific command (route/netsh/ip) required at all."""
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(2)
            s.connect(("8.8.8.8", 80))
            return bool(s.getsockname()[0])
    except OSError:
        return False


def _current_mac_wifi_ssid() -> dict:
    """Best-effort read of the current machine's connected WiFi network
    name -- SSID only, never the password (that lives in Keychain/Windows
    Credential Manager behind its own OS-level prompt, which we
    deliberately don't try to bypass; see /settings' "Use my Mac's
    network" button, which fills only the network-name field and leaves
    password entry to the user).

    Returns {"ssid": str|None, "blocked": bool}. Named for its original
    macOS-only implementation; now dispatches per-OS below, but the shape
    and "Use my Mac's network" button label are unchanged since that's
    still the primary/only platform this app ships packaged for today
    (see clicky_windows.spec's comment for what Windows support that
    exists is/isn't).

    On macOS: confirmed live that since Big Sur, reading the connected
    SSID requires the calling process to hold Location Services permission
    (SSID implies rough location) -- without it, `networksetup
    -getairportnetwork` returns "You are not associated with an AirPort
    network." EVEN WHILE ACTUALLY CONNECTED, indistinguishable from
    genuinely being offline by that string alone. `blocked=True` is this
    function's best guess that it's the permission gate, not a real
    disconnection: there's a working default route (some interface is
    online) but the SSID lookup still came back empty.
    On Windows: `netsh wlan show interfaces` needs no special permission
    grant the way macOS does, so a miss there just means "not on WiFi" --
    blocked is still computed the same way for a consistent response
    shape, but should rarely end up true in practice.
    On anything else (Linux, etc.): no attempt made, same blocked logic."""
    import platform
    import subprocess
    ssid = None
    system = platform.system()

    try:
        if system == "Darwin":
            ports = subprocess.run(
                ["networksetup", "-listallhardwareports"],
                capture_output=True, text=True, timeout=5,
            ).stdout.splitlines()
            device = None
            for i, line in enumerate(ports):
                if line.strip() == "Hardware Port: Wi-Fi" and i + 1 < len(ports):
                    next_line = ports[i + 1].strip()
                    if next_line.startswith("Device:"):
                        device = next_line.split(":", 1)[1].strip()
                    break
            if device:
                result = subprocess.run(
                    ["networksetup", "-getairportnetwork", device],
                    capture_output=True, text=True, timeout=5,
                ).stdout.strip()
                # Success: "Current Wi-Fi Network: MySSID". Blocked-or-offline:
                # "You are not associated with an AirPort network." (no colon).
                if ":" in result:
                    ssid = result.split(":", 1)[1].strip()
        elif system == "Windows":
            result = subprocess.run(
                ["netsh", "wlan", "show", "interfaces"],
                capture_output=True, text=True, timeout=5,
            ).stdout
            for line in result.splitlines():
                line = line.strip()
                # Matches "SSID" but not "BSSID" (the AP's own MAC-derived
                # identifier, a different field entirely) -- startswith,
                # not "in", specifically to avoid that collision.
                if line.startswith("SSID") and not line.startswith("BSSID") and ":" in line:
                    ssid = line.split(":", 1)[1].strip()
                    break
    except Exception:
        pass

    if ssid:
        return {"ssid": ssid, "blocked": False}
    return {"ssid": None, "blocked": _has_active_network_route()}


@app.get("/api/mac-wifi-ssid")
def api_mac_wifi_ssid(request: Request):
    redirect = _gate(request)
    if redirect:
        return redirect
    return _current_mac_wifi_ssid()


def _settings_context(active_panel: str = "device", wifi_msg: str = "", saved: bool = False, setup_error: str = None) -> dict:
    """Common context every settings.html render needs -- centralized so a
    new integration/panel doesn't mean touching every route that renders
    this template. Integrations (Notion/Google/Obsidian/social accounts)
    live as panels on this one page, alongside the original Device/
    Providers/Notifications/Feedback/Account panels -- active_panel picks
    which sidebar item is pre-selected (e.g. a Google-specific OAuth error
    should land on the Google panel). Social Media Posting itself is a
    separate top-level page (/social), not nested here -- see that route.
    """
    return {
        "current": settings.get_all(),
        "active_panel": active_panel,
        "active_nav": "settings",
        "wifi_msg": wifi_msg,
        "saved": saved,
        "setup_error": setup_error,
        "google_connected": google_client.is_connected(),
        "google_configured": google_client.has_client_credentials(),
        "linkedin_connected": linkedin_client.is_connected(),
        "linkedin_configured": linkedin_client.has_client_credentials(),
        "x_connected": x_client.is_connected(),
        "x_configured": x_client.has_client_credentials(),
        "substack_connected": substack_client.is_connected(),
        "device_id": settings.get_or_create_device_id(),
        "jarvis_device_api_key": settings.get_or_create_jarvis_device_api_key(),
        "voiceprints": voice_id.list_voiceprints(),
        "app_update": update_check.check_app_update(),
        "app_version": config.APP_VERSION,
        "bundled_firmware_version": update_check.get_bundled_firmware_version(),
        "device_firmware_version": poller.get_device_firmware_version(),
    }


@app.get("/integrations")
def integrations_form(request: Request, panel: str = "notion"):
    redirect = _gate(request)
    if redirect:
        return redirect
    return templates.TemplateResponse(request, "settings.html", _settings_context(panel))


def _extract_notion_id(raw: str) -> str:
    """Accepts a bare ID or a full Notion URL (either notion.so/workspace/
    <id>?v=... or app.notion.com/p/<id> style) and returns just the
    32-character ID, dashed into standard UUID form. Notion's API rejects
    anything else with a validation error — easy to trip if you paste the
    whole browser URL including the "?v=" view parameter instead of just
    the ID (a real mistake a user hit here)."""
    match = re.search(r"[0-9a-fA-F]{32}", raw.replace("-", ""))
    if not match:
        return raw.strip()  # let Notion's own API error surface anything we couldn't parse
    hex32 = match.group(0)
    return f"{hex32[0:8]}-{hex32[8:12]}-{hex32[12:16]}-{hex32[16:20]}-{hex32[20:32]}"


@app.post("/integrations")
def integrations_submit(
    request: Request,
    panel: str = Form("notion"),  # which sidebar tab submitted this -- see integrations.html's hidden field
    notion_token: str = Form(""),
    notion_database_id: str = Form(""),
    notion_tasks_database_id: str = Form(""),
    notion_people_database_id: str = Form(""),
    notion_events_database_id: str = Form(""),
    notion_journal_database_id: str = Form(""),
    obsidian_vault_path: str = Form(""),
    linkedin_client_id: str = Form(""),
    linkedin_client_secret: str = Form(""),
    x_client_id: str = Form(""),
    x_client_secret: str = Form(""),
    substack_session_cookie: str = Form(""),
    substack_publication_url: str = Form(""),
):
    redirect = _gate(request)
    if redirect:
        return redirect
    settings.update(
        notion_token=notion_token or None,
        notion_database_id=_extract_notion_id(notion_database_id) if notion_database_id else None,
        notion_tasks_database_id=_extract_notion_id(notion_tasks_database_id) if notion_tasks_database_id else None,
        notion_people_database_id=_extract_notion_id(notion_people_database_id) if notion_people_database_id else None,
        notion_events_database_id=_extract_notion_id(notion_events_database_id) if notion_events_database_id else None,
        notion_journal_database_id=_extract_notion_id(notion_journal_database_id) if notion_journal_database_id else None,
        obsidian_vault_path=obsidian_vault_path or None,
        linkedin_client_id=linkedin_client_id or None,
        linkedin_client_secret=linkedin_client_secret or None,
        x_client_id=x_client_id or None,
        x_client_secret=x_client_secret or None,
        # Unofficial/ToS-risk integration -- see substack_client.py's module
        # docstring; the /integrations panel repeats this warning inline,
        # this isn't the only place it's disclosed.
        substack_session_cookie=substack_session_cookie or None,
        substack_publication_url=substack_publication_url or None,
    )
    return templates.TemplateResponse(request, "settings.html", _settings_context(panel, saved=True))


@app.post("/integrations/setup-notion")
def integrations_setup_notion(
    request: Request,
    notion_token: str = Form(...),
    notion_parent_page_id: str = Form(...),
):
    redirect = _gate(request)
    if redirect:
        return redirect
    import notion_setup
    try:
        page_id = _extract_notion_id(notion_parent_page_id)
        ids = notion_setup.create_workspace(notion_token, page_id)
        settings.update(notion_token=notion_token, **ids)
        return templates.TemplateResponse(request, "settings.html", _settings_context("notion", saved=True, setup_error=None))
    except notion_setup.NotionSetupError as e:
        return templates.TemplateResponse(request, "settings.html", _settings_context("notion", saved=False, setup_error=str(e)))


@app.get("/google/connect")
def google_connect(request: Request):
    # No credentials form -- the OAuth client is baked into the app at
    # build time (config.GOOGLE_CLIENT_ID/SECRET), so this is the only step
    # a user ever needs: click Connect, see Google's consent screen.
    redirect = _gate(request)
    if redirect:
        return redirect
    if not google_client.has_client_credentials():
        return RedirectResponse("/settings?panel=google", status_code=303)
    state = secrets.token_urlsafe(16)
    request.session["google_oauth_state"] = state
    return RedirectResponse(google_client.authorize_url(request, state), status_code=303)


@app.get("/google/callback", name="google_callback")
def google_callback(request: Request, code: str = None, state: str = None, error: str = None):
    redirect = _gate(request)
    if redirect:
        return redirect
    expected_state = request.session.pop("google_oauth_state", None)
    if error or not code or not state or state != expected_state:
        return templates.TemplateResponse(request, "settings.html",
            _settings_context("google", setup_error=f"Google sign-in failed: {error or 'invalid response'}"))
    try:
        google_client.exchange_code(request, code)
    except RuntimeError as e:
        return templates.TemplateResponse(request, "settings.html", _settings_context("google", saved=False, setup_error=str(e)))
    return RedirectResponse("/settings?panel=google", status_code=303)


@app.post("/google/disconnect")
def google_disconnect(request: Request):
    redirect = _gate(request)
    if redirect:
        return redirect
    google_client.disconnect()
    return RedirectResponse("/settings?panel=google", status_code=303)


@app.post("/integrations/setup-publications")
def integrations_setup_publications(
    request: Request,
    notion_parent_page_id: str = Form(...),
):
    """One-off: creates the Publications database (see
    notion_setup.create_publications_database) under the same parent page
    the rest of the workspace lives in. Requires Notion + the Notes
    database to already be configured -- Publications relates back to it
    ("Source Recording")."""
    redirect = _gate(request)
    if redirect:
        return redirect
    saved = settings.get_all()
    notion_token = saved.get("notion_token")
    notes_database_id = saved.get("notion_database_id")
    if not notion_token or not notes_database_id:
        return templates.TemplateResponse(request, "settings.html",
            _settings_context("social-accounts", setup_error="Set up Notion (Notes database) first, then add Publications."))
    import notion_setup
    try:
        page_id = _extract_notion_id(notion_parent_page_id)
        ids = notion_setup.create_publications_database(notion_token, page_id, notes_database_id)
        settings.update(**ids)
        return templates.TemplateResponse(request, "settings.html", _settings_context("social-accounts", saved=True, setup_error=None))
    except notion_setup.NotionSetupError as e:
        return templates.TemplateResponse(request, "settings.html", _settings_context("social-accounts", saved=False, setup_error=str(e)))


@app.post("/integrations/setup-jarvis")
def integrations_setup_jarvis(
    request: Request,
    notion_parent_page_id: str = Form(...),
):
    """One-off: creates the Jarvis database (see
    notion_setup.create_jarvis_database) under the same parent page the
    rest of the workspace lives in. Independent of the Notes database --
    Jarvis commands are their own log, not related back to Notes."""
    redirect = _gate(request)
    if redirect:
        return redirect
    saved = settings.get_all()
    notion_token = saved.get("notion_token")
    if not notion_token:
        return templates.TemplateResponse(request, "settings.html",
            _settings_context("jarvis", setup_error="Set up Notion first, then add the Jarvis database."))
    import notion_setup
    try:
        page_id = _extract_notion_id(notion_parent_page_id)
        ids = notion_setup.create_jarvis_database(notion_token, page_id)
        settings.update(**ids)
        return templates.TemplateResponse(request, "settings.html", _settings_context("jarvis", saved=True, setup_error=None))
    except notion_setup.NotionSetupError as e:
        return templates.TemplateResponse(request, "settings.html", _settings_context("jarvis", saved=False, setup_error=str(e)))


@app.post("/jarvis/enable-live-agent")
def jarvis_enable_live_agent(request: Request, groq_api_key: str = Form("")):
    """Pushes the device the config it needs to start using the live
    Deepgram Voice Agent path (voice_agent.cpp) instead of always
    recording Jarvis commands to SD -- see device_client.set_jarvis_config's
    docstring for why only groq_api_key is ever new user input here."""
    redirect = _gate(request)
    if redirect:
        return redirect
    saved = settings.get_all()
    deepgram_key = saved.get("deepgram_api_key") or ""
    if not deepgram_key:
        return templates.TemplateResponse(request, "settings.html",
            _settings_context("jarvis", setup_error="Set a Deepgram API key under Settings -> Providers first."))
    if groq_api_key:
        settings.update(groq_api_key=groq_api_key)
    else:
        groq_api_key = saved.get("groq_api_key") or ""
    if not groq_api_key:
        return templates.TemplateResponse(request, "settings.html",
            _settings_context("jarvis", setup_error="A Groq API key is required (free tier) -- enter one below."))

    mac_base_url = poller.wifi_base_url_if_reachable()
    if not mac_base_url:
        return templates.TemplateResponse(request, "settings.html",
            _settings_context("jarvis", setup_error="Device isn't reachable over WiFi right now -- try again once it is."))

    device_key = settings.get_or_create_jarvis_device_api_key()
    import device_client
    try:
        device_client.set_jarvis_config(deepgram_key, groq_api_key, mac_base_url, device_key, base_url=mac_base_url)
    except Exception as e:
        return templates.TemplateResponse(request, "settings.html",
            _settings_context("jarvis", setup_error=f"Couldn't push config to the device: {e}"))
    return templates.TemplateResponse(request, "settings.html", _settings_context("jarvis", saved=True, setup_error=None))


@app.get("/linkedin/connect")
def linkedin_connect(request: Request):
    redirect = _gate(request)
    if redirect:
        return redirect
    if not linkedin_client.has_client_credentials():
        return RedirectResponse("/settings?panel=social-accounts", status_code=303)
    state = secrets.token_urlsafe(16)
    request.session["linkedin_oauth_state"] = state
    return RedirectResponse(linkedin_client.authorize_url(request, state), status_code=303)


@app.get("/linkedin/callback", name="linkedin_callback")
def linkedin_callback(request: Request, code: str = None, state: str = None, error: str = None):
    redirect = _gate(request)
    if redirect:
        return redirect
    expected_state = request.session.pop("linkedin_oauth_state", None)
    if error or not code or not state or state != expected_state:
        return templates.TemplateResponse(request, "settings.html",
            _settings_context("social-accounts", setup_error=f"LinkedIn sign-in failed: {error or 'invalid response'}"))
    try:
        linkedin_client.exchange_code(request, code)
    except RuntimeError as e:
        return templates.TemplateResponse(request, "settings.html", _settings_context("social-accounts", saved=False, setup_error=str(e)))
    return RedirectResponse("/settings?panel=social-accounts", status_code=303)


@app.post("/linkedin/disconnect")
def linkedin_disconnect(request: Request):
    redirect = _gate(request)
    if redirect:
        return redirect
    linkedin_client.disconnect()
    return RedirectResponse("/settings?panel=social-accounts", status_code=303)


@app.get("/x/connect")
def x_connect(request: Request):
    redirect = _gate(request)
    if redirect:
        return redirect
    if not x_client.has_client_credentials():
        return RedirectResponse("/settings?panel=social-accounts", status_code=303)
    state = secrets.token_urlsafe(16)
    request.session["x_oauth_state"] = state
    return RedirectResponse(x_client.authorize_url(request, state), status_code=303)


@app.get("/x/callback", name="x_callback")
def x_callback(request: Request, code: str = None, state: str = None, error: str = None):
    redirect = _gate(request)
    if redirect:
        return redirect
    expected_state = request.session.pop("x_oauth_state", None)
    if error or not code or not state or state != expected_state:
        return templates.TemplateResponse(request, "settings.html",
            _settings_context("social-accounts", setup_error=f"X sign-in failed: {error or 'invalid response'}"))
    try:
        x_client.exchange_code(request, code, state)
    except RuntimeError as e:
        return templates.TemplateResponse(request, "settings.html", _settings_context("social-accounts", saved=False, setup_error=str(e)))
    return RedirectResponse("/settings?panel=social-accounts", status_code=303)


@app.post("/x/disconnect")
def x_disconnect(request: Request):
    redirect = _gate(request)
    if redirect:
        return redirect
    x_client.disconnect()
    return RedirectResponse("/settings?panel=social-accounts", status_code=303)


def _admin_gate(request: Request):
    """Device Fleet is for the app author only (ADMIN_EMAIL), not every
    Clicky user -- every install has its own independent login, so the
    regular _gate() alone doesn't distinguish "the author's own install"
    from "a user's install". Redirects to Settings' Account panel (where
    owner_email is set) rather than a bare 404, so a legitimate owner who
    hasn't set it yet finds their way there instead of a dead end."""
    redirect = _gate(request)
    if redirect:
        return redirect
    if not _is_admin():
        return RedirectResponse("/settings?panel=account", status_code=303)
    return None


def _admin_context(result=None, ip=""):
    import firmware_flash
    detected_dir = firmware_flash.firmware_binaries_dir()
    if not ip:
        # Default to whatever IP the poller's own sync loop currently knows
        # the device by (same source Settings' Device panel uses) -- saves
        # re-typing an address you can already see live-tracked elsewhere
        # in the app, and stays correct across DHCP re-leases without
        # editing anything here.
        wifi_url = poller.wifi_base_url_if_reachable()
        if wifi_url:
            ip = wifi_url.split("://", 1)[-1].rstrip("/")
    return {
        "active_nav": "admin", "result": result, "ip": ip,
        "serial_ports": firmware_flash.list_serial_ports(),
        "usb_flash_available": detected_dir is not None,
        "firmware_binaries_dir": detected_dir or "",
    }


@app.get("/admin")
def admin_page(request: Request):
    """Fleet-management page for the app owner only -- see _admin_gate().
    Local-network only: talks directly to whatever IP is entered, no cloud
    relay -- meant for naming and flashing devices before shipping them
    out, or a returned unit reconnected to your LAN, not for reaching a
    device already out with a user on their own network."""
    redirect = _admin_gate(request)
    if redirect:
        return redirect
    return templates.TemplateResponse(request, "admin.html", _admin_context())


@app.post("/admin/info")
def admin_info(request: Request, ip: str = Form("")):
    redirect = _admin_gate(request)
    if redirect:
        return redirect
    if not ip.strip():
        return templates.TemplateResponse(request, "admin.html",
            _admin_context(result={"ok": False, "action": "info", "error": "Enter a device IP address first"}, ip=ip))
    import device_client
    base_url = f"http://{ip}"
    try:
        info = device_client.get_device_info(base_url)
        result = {"ok": True, "action": "info", "info": info}
    except Exception as e:
        result = {"ok": False, "action": "info", "error": str(e)}
    return templates.TemplateResponse(request, "admin.html", _admin_context(result=result, ip=ip))


@app.post("/admin/set-name")
def admin_set_name(request: Request, ip: str = Form(""), device_name: str = Form("")):
    redirect = _admin_gate(request)
    if redirect:
        return redirect
    if not ip.strip() or not device_name.strip():
        return templates.TemplateResponse(request, "admin.html",
            _admin_context(result={"ok": False, "action": "set-name", "error": "Enter both a device IP and a name"}, ip=ip))
    import device_client
    base_url = f"http://{ip}"
    try:
        device_client.set_device_name(device_name, base_url)
        info = device_client.get_device_info(base_url)
        result = {"ok": True, "action": "set-name", "info": info}
    except Exception as e:
        result = {"ok": False, "action": "set-name", "error": str(e)}
    return templates.TemplateResponse(request, "admin.html", _admin_context(result=result, ip=ip))


@app.post("/admin/flash")
def admin_flash(request: Request, ip: str = Form("")):
    """Unconditional push of this app's bundled firmware.bin (see
    update_check.force_push_firmware) -- for flashing a batch of devices
    onto the exact same build before shipping, regardless of whatever
    version each one currently has."""
    redirect = _admin_gate(request)
    if redirect:
        return redirect
    if not ip.strip():
        return templates.TemplateResponse(request, "admin.html",
            _admin_context(result={"ok": False, "action": "flash", "error": "Enter a device IP address first"}, ip=ip))
    base_url = f"http://{ip}"
    push_result = update_check.force_push_firmware(base_url)
    result = {"ok": push_result.get("ok", False), "action": "flash",
              "error": push_result.get("error"), "note": push_result.get("note"),
              "bundled_version": update_check.get_bundled_firmware_version()}
    return templates.TemplateResponse(request, "admin.html", _admin_context(result=result, ip=ip))


@app.post("/admin/flash-usb")
def admin_flash_usb(request: Request, port: str = Form(""), binaries_dir: str = Form("")):
    """Full USB flash (bootloader + partition table + app) for a brand-new
    device that's never run any firmware, or any board you have physical
    access to -- unlike /admin/flash (OTA over WiFi), this doesn't need
    the device to already be running firmware or have a known IP. Uses
    the three binaries bundled into config.FIRMWARE_DIR alongside the
    app-partition firmware.bin already shipped for OTA (see
    firmware_flash.py's module docstring), so this works in the packaged
    app for any user -- not just a dev checkout with PlatformIO installed."""
    redirect = _admin_gate(request)
    if redirect:
        return redirect
    if not port.strip():
        return templates.TemplateResponse(request, "admin.html",
            _admin_context(result={"ok": False, "action": "flash-usb", "error": "Select or enter a serial port first"}))
    import firmware_flash
    flash_result = firmware_flash.flash_full(port, binaries_dir_override=binaries_dir)
    result = {"ok": flash_result.get("ok", False), "action": "flash-usb",
              "error": flash_result.get("error"), "output": flash_result.get("output")}
    return templates.TemplateResponse(request, "admin.html", _admin_context(result=result))


@app.post("/jarvis/execute-decision")
async def jarvis_execute_decision(request: Request):
    """Called BY THE DEVICE (not a browser) -- the first route in this app
    that the ESP32 calls into, rather than the Mac pulling from the device.
    An on-device live agent (Deepgram Voice Agent, streaming BOOT-captured
    audio) decides an action in real time and forwards just the structured
    fields here for execution, skipping jarvis.decide_action()'s own
    classification call since that already happened on-device.

    No session cookie exists on the firmware side, so this can't use
    _gate() -- instead it requires the shared secret from
    settings.get_or_create_jarvis_device_api_key() in the X-Jarvis-Key
    header. Without this, any device on the same WiFi could hit this route
    and make the app draft emails / add calendar events."""
    expected_key = settings.get_or_create_jarvis_device_api_key()
    if request.headers.get("X-Jarvis-Key") != expected_key:
        return JSONResponse({"ok": False, "error": "invalid or missing X-Jarvis-Key"}, status_code=401)

    try:
        decision = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON body"}, status_code=400)

    import jarvis
    try:
        result = await asyncio.to_thread(jarvis.execute_decided_action, decision)
    except Exception as e:
        log.warning("execute_decided_action failed: %s", e)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    return JSONResponse(result)


@app.post("/jarvis/search-memory")
async def jarvis_search_memory(request: Request):
    """Called BY THE DEVICE -- the live Voice Agent's search_memory function
    (see voice_agent.cpp), forwarded here to actually run the semantic
    search (rag_index.py) since the ESP32 has no embedding model of its
    own. Returns {"result": "<text>"} for the agent's live turn to
    incorporate. Same X-Jarvis-Key auth as /jarvis/execute-decision."""
    expected_key = settings.get_or_create_jarvis_device_api_key()
    if request.headers.get("X-Jarvis-Key") != expected_key:
        return JSONResponse({"result": "Search unavailable -- invalid key."}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"result": "Search failed -- invalid request."}, status_code=400)

    # The device already resolves relative dates itself (Deepgram's own LLM
    # turn fills date_start/date_end as concrete ISO dates, not a label like
    # "last week"), so this calls rag_index directly rather than
    # jarvis.search_memory() (which expects a time_range *label* to parse).
    import rag_index
    try:
        results = await asyncio.to_thread(
            rag_index.search, body.get("query") or "",
            5, body.get("date_start"), body.get("date_end"), body.get("speaker"),
        )
    except Exception as e:
        log.warning("search_memory (from device) failed: %s", e)
        return JSONResponse({"result": "Search failed."})
    if not results:
        return JSONResponse({"result": "Nothing found."})
    combined = " / ".join(f"[{r.get('date') or 'unknown date'}] {r['text'][:400]}" for r in results[:3])
    return JSONResponse({"result": combined})


@app.get("/jarvis/memory-facts")
def jarvis_memory_facts(request: Request):
    """Called BY THE DEVICE once per live session (voice_agent.cpp's
    fetchMemoryFacts(), before streaming starts) to inject memory_store's
    always-known facts into the agent's own prompt -- distinct from
    search_memory's on-demand retrieval. Same X-Jarvis-Key auth."""
    expected_key = settings.get_or_create_jarvis_device_api_key()
    if request.headers.get("X-Jarvis-Key") != expected_key:
        return Response("", status_code=401)

    import memory_store
    return Response(memory_store.facts_context(), media_type="text/plain")


@app.get("/jarvis")
def jarvis_page(request: Request):
    """Jarvis voice commands, kept off the main dashboard (see index()) --
    they aren't a recording in the memo/journal sense (no speakers/summary/
    action items), so they get the same top-level-page treatment /social
    already established for a different non-memo recording kind."""
    redirect = _gate(request)
    if redirect:
        return redirect
    commands = [r for r in _recordings_for_display() if r.get("kind") == "command"]
    return templates.TemplateResponse(request, "jarvis.html", {"commands": commands, "active_nav": "jarvis"})


@app.get("/social")
def social_page(request: Request):
    """Read-only mirror of the Notion Publications database -- approving,
    editing, and scheduling all happen in Notion (see
    poller.check_publication_approvals_once), consistent with the existing
    email-draft flow. This groups each recording's sibling posts (LinkedIn
    teaser + Substack/Medium long-form) together so you can see "this
    LinkedIn post links to this Substack post" without opening Notion. A
    top-level page (site-wide nav item), not nested under Settings --
    account credentials/setup for these platforms still live in Settings
    (Social Media Accounts panel)."""
    redirect = _gate(request)
    if redirect:
        return redirect
    saved = settings.get_all()
    can_sync = bool(
        (saved.get("notion_publications_database_id") and saved.get("notion_token"))
        or saved.get("obsidian_vault_path")
    )
    cards = []
    for record in storage.list_recordings():
        posts = record.get("social_posts") or {}
        if not posts:
            continue
        synced = bool(record.get("notion_publication_page_id") or record.get("obsidian_publication_note_path"))
        cards.append({
            "name": record["name"],
            "content_hash": record["content_hash"],
            "posts": posts,
            # Jarvis's "write me a post" action generates posts before any
            # database is necessarily configured yet (dashboard-first, see
            # jarvis.py's _action_social_post) -- synced is false and
            # can_sync becomes true the moment the user configures a
            # backend, showing the "Sync now" button below.
            "synced": synced,
            "can_sync": can_sync and not synced,
        })
    return templates.TemplateResponse(request, "social.html", {"cards": cards, "active_nav": "social"})


@app.post("/social/sync-now/{content_hash}")
def social_sync_now(request: Request, content_hash: str):
    """Pushes a dashboard-only social post (generated before any Notion
    Publications DB / Obsidian vault was configured -- see jarvis.py's
    _action_social_post) to whichever backend is configured now."""
    redirect = _gate(request)
    if redirect:
        return redirect
    import poller
    poller.push_social_posts_now(content_hash)
    return RedirectResponse("/social", status_code=303)


@app.get("/social/{content_hash}/medium.md")
def social_medium_markdown(request: Request, content_hash: str):
    """Medium has no publish API (see substack_client.py's docstring for
    the parallel Substack situation) -- this is the entire "publish to
    Medium" flow: download the post as markdown, then paste/import it via
    Medium's own editor or Import-a-story page."""
    redirect = _gate(request)
    if redirect:
        return redirect
    record = storage.get_recording(content_hash)
    if not record:
        return JSONResponse({"error": "recording not found"}, status_code=404)
    post = (record.get("social_posts") or {}).get("medium")
    if not post:
        return JSONResponse({"error": "no Medium draft for this recording"}, status_code=404)
    title = post.get("title") or record["name"]
    body = post.get("body") or ""
    md = f"# {title}\n\n{body}\n\n— Live recorded from Clicky\n"
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", title)[:80] or "post"
    return Response(
        content=md, media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}.md"'},
    )


FEEDBACK_RECIPIENT = "sanchit.gupta01@gmail.com"


@app.post("/feedback")
def submit_feedback(request: Request, kind: str = Form("feedback"), message: str = Form(...)):
    """Sends directly via Gmail if connected; otherwise redirects to a
    mailto: link so feedback is never blocked on an OAuth connection
    existing. Either way, count it in the usage digest (analytics.py)."""
    redirect = _gate(request)
    if redirect:
        return redirect
    message = message.strip()
    if not message:
        return templates.TemplateResponse(request, "settings.html",
            _settings_context("feedback", setup_error="Feedback message can't be empty."))

    subject = f"Clicky {kind.replace('_', ' ')}"
    import analytics
    if google_client.is_connected():
        try:
            google_client.send_email([FEEDBACK_RECIPIENT], subject, message)
        except RuntimeError as e:
            return templates.TemplateResponse(request, "settings.html",
                _settings_context("feedback", setup_error=f"Failed to send: {e}"))
        analytics.track_event("feedback_submitted_count")
        return templates.TemplateResponse(request, "settings.html", _settings_context("feedback", saved=True))

    analytics.track_event("feedback_submitted_count")
    import urllib.parse
    mailto = f"mailto:{FEEDBACK_RECIPIENT}?subject={urllib.parse.quote(subject)}&body={urllib.parse.quote(message)}"
    return RedirectResponse(mailto, status_code=303)


@app.post("/people/contact")
def people_contact(request: Request, name: str = Form(...), email: str = Form(""), linkedin: str = Form("")):
    """Manual email/LinkedIn entry for a speaker/stakeholder Google Meet
    didn't supply one for (see notion_sync.set_person_contact_by_name --
    looks the person up by name, creating a minimal People page if none
    exists). Name-based here is fine: this IS the human confirming who
    these contact details belong to, not an automated guess."""
    redirect = _gate(request)
    if redirect:
        return redirect
    people_database_id = settings.get_all().get("notion_people_database_id")
    if not people_database_id:
        return JSONResponse({"error": "Notion People database isn't configured"}, status_code=400)
    import notion_sync
    try:
        notion_sync.set_person_contact_by_name(name.strip(), people_database_id,
                                                email=email.strip() or None, linkedin=linkedin.strip() or None)
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    import analytics
    analytics.track_event("people_contact_added")
    return JSONResponse({"ok": True})


@app.get("/api/people/duplicates")
def people_duplicates(request: Request):
    """Scans the People database for pages sharing an Email or LinkedIn
    value under different names (see notion_sync.find_duplicate_people) --
    the dashboard shows these as "these look like the same person"
    suggestions with an explicit Merge button; nothing here merges
    automatically."""
    redirect = _gate(request)
    if redirect:
        return redirect
    people_database_id = settings.get_all().get("notion_people_database_id")
    if not people_database_id:
        return JSONResponse({"groups": []})
    import notion_sync
    try:
        groups = notion_sync.find_duplicate_people(people_database_id)
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    return JSONResponse({"groups": groups})


@app.post("/people/merge")
def people_merge(request: Request, keeper_id: str = Form(...), loser_id: str = Form(...)):
    """Explicit, user-confirmed merge of two People pages found by
    /api/people/duplicates -- see notion_sync.merge_person_pages for what
    actually happens (content append, relation re-pointing, archive)."""
    redirect = _gate(request)
    if redirect:
        return redirect
    import notion_sync
    try:
        notion_sync.merge_person_pages(keeper_id, loser_id)
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    import analytics
    analytics.track_event("people_merged")
    return JSONResponse({"ok": True})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=False)
