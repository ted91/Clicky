"""Jarvis: BOOT-captured voice commands (cmd_*.wav) are routed here instead
of the memo pipeline (see poller.process_once's kind=="command" branch).

Scope, per explicit user direction: the cloud LLM (get_completer(), whatever
provider is configured -- Mistral today) is the answer engine for "qa" and
also handles the small routing/composition steps any voice assistant needs
(decide_action's dispatch call, multi-source recall summarization) -- but no
Jarvis ACTION executes via a cloud API. Genuine coding tasks always go
through the real Claude Code CLI (never GUI automation -- only the CLI has
real file/repo access).

Windows-specific gap (flagged explicitly, not silently degraded): the macOS
version drives Calendar.app/Reminders.app/Mail.app via AppleScript. No
Outlook COM automation exists here yet -- instead, calendar_event/reminder/
email_draft write a structured note into a connected Obsidian vault's
"Jarvis" folder (see _write_obsidian_note) as their real Windows
implementation; with no vault configured, they return a clear "connect
Obsidian" message rather than pretending to work. GUI-automation-qa and
speak (device TTS) are still plain stubs -- see their own docstrings. qa
(default LLM), social_post, code_task, and open_app all work identically to
macOS. A real Outlook-COM-automation path (via pywin32) is a separate
follow-up if Obsidian-backed notes aren't enough."""
import json
import logging
import os
import subprocess
from datetime import datetime, timedelta, timezone

import settings
import storage
from providers import get_completer

log = logging.getLogger("jarvis")

# In-memory multi-turn session state -- {} when nothing's open. Cleared
# whenever decide_action() classifies a new command as not continuing the
# open flow (see process_command). Not persisted across app restarts --
# Phase 1 scope, a restart mid-flow just starts fresh.
_session = {}

_CODE_SESSIONS_PATH = os.path.join(os.path.dirname(__file__), "jarvis_code_sessions.json")


def _load_code_sessions() -> dict:
    try:
        with open(_CODE_SESSIONS_PATH, "r") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_code_sessions(data: dict):
    try:
        with open(_CODE_SESSIONS_PATH, "w") as f:
            json.dump(data, f)
    except OSError as e:
        log.warning("could not persist jarvis code sessions: %s", e)


# --- decision / routing ----------------------------------------------------

def _parse_decision_json(raw_text: str) -> dict:
    text = raw_text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, AttributeError):
        return {
            "action_type": "unknown", "continues_session": False, "app_name": None,
            "title": None, "date": None, "time": None, "recipient_name": None,
            "referenced_person": None, "referenced_topic": None, "referenced_time_range": None,
            "target_app": None, "query": raw_text.strip(),
        }
    return {
        "action_type": data.get("action_type") or "unknown",
        "continues_session": bool(data.get("continues_session")),
        "app_name": data.get("app_name") or None,
        "title": data.get("title") or None,
        "date": data.get("date") or None,
        "time": data.get("time") or None,
        "recipient_name": data.get("recipient_name") or None,
        "referenced_person": data.get("referenced_person") or None,
        "referenced_topic": data.get("referenced_topic") or None,
        "referenced_time_range": data.get("referenced_time_range") or None,
        "target_app": data.get("target_app") or None,
        "query": data.get("query") or raw_text.strip(),
    }


def decide_action(transcript: str, session: dict = None) -> dict:
    """The one dispatcher call that has to exist to route any transcript at
    all -- decides action type AND session continuity in one shot, per the
    user's explicit choice that continuation be context-based rather than a
    fixed timeout."""
    session = session or {}
    context_block = ""
    if session:
        context_block = "\n\n[An action flow is currently open: " + json.dumps(session.get("context", {})) + \
                         " -- decide whether this command continues it.]"
    prompt = (
        "You route one spoken voice command for a personal-assistant device to exactly one action "
        "type. Return ONLY JSON (no markdown fences, no commentary) matching exactly:\n"
        '{"action_type": "open_app" | "calendar_event" | "reminder" | "email_draft" | "social_post" | "qa" | "code_task" | "unknown", '
        '"continues_session": true or false, '
        '"app_name": "app name or null", '
        '"title": "event/reminder title or null", '
        '"date": "YYYY-MM-DD or null", '
        '"time": "HH:MM or null", '
        '"recipient_name": "person name or null (email_draft recipient)", '
        '"referenced_person": "person name or null (if this references a past conversation with someone)", '
        '"referenced_topic": "a project/topic name or null (if this references past recordings about a '
        'subject rather than a specific person, e.g. \\"the Clicky project\\", \\"my trip planning\\")", '
        '"referenced_time_range": "e.g. \\"last week\\"/\\"yesterday\\"/\\"last month\\", or null", '
        '"target_app": "an assistant/app name the user explicitly named (e.g. ChatGPT, Cursor), or null", '
        '"query": "the actual question/instruction text to act on"}\n'
        "action_type meanings: open_app = launch a named app. calendar_event = add a calendar event. "
        "reminder = add a reminder. email_draft = compose an email (recipient_name is who the email goes "
        "TO -- separate from referenced_person, who the command's CONTEXT comes from, e.g. \"based on the "
        "conversation with Paul, email Jeremie about...\" has recipient_name=Jeremie, referenced_person=Paul). "
        "social_post = a request to write/draft a social/blog post based on the speaker's journal or a "
        "past conversation with someone (referenced_person is who that conversation was with, or null for "
        "\"my journal\"/their own recent reflections). "
        "qa = a general question, a pure recall request (\"remind me what we discussed with X\"), or a "
        "request to continue a chat with a named assistant (target_app). code_task = build/fix/write code "
        "or continue a coding project. unknown = doesn't clearly fit any of the above.\n"
        f"Resolve relative dates against today's actual date: {datetime.now():%Y-%m-%d}."
        + context_block +
        f"\n\nCommand: \"{transcript}\""
    )
    _, complete = get_completer()
    raw = complete(prompt)
    return _parse_decision_json(raw)


# --- cross-source retrieval --------------------------------------------------

def _parse_time_range(label):
    """Resolves a relative label to a concrete (start, end) ISO date pair
    in plain code against the real current date -- deterministic given a
    label, not re-asked of the LLM."""
    if not label:
        return None, None
    label = label.strip().lower()
    today = datetime.now(timezone.utc).date()
    if "yesterday" in label:
        d = today - timedelta(days=1)
        return d.isoformat(), d.isoformat()
    if "last week" in label:
        start = today - timedelta(days=today.weekday() + 7)
        end = start + timedelta(days=6)
        return start.isoformat(), end.isoformat()
    if "last month" in label:
        first_of_this_month = today.replace(day=1)
        last_of_prev_month = first_of_this_month - timedelta(days=1)
        return last_of_prev_month.replace(day=1).isoformat(), last_of_prev_month.isoformat()
    return None, None


def _search_local_recordings(keyword: str, start=None, end=None) -> list:
    """Matches `keyword` against a recording's speaker/stakeholder names
    AND its full recording name/transcript/summary text -- broad on
    purpose, so this same function serves a person's name, a project name,
    a topic, or any other phrase the user references ("based on my
    journal", "the Clicky project", "that conversation about X"), not just
    people. This is the "give Jarvis access to all transcripts by name,
    date, conversation, project" retrieval the user asked for."""
    needle = keyword.strip().lower()
    matches = []
    for record in storage.list_recordings():
        date = (record.get("created_at") or "")[:10]
        if start and date < start:
            continue
        if end and date > end:
            continue
        names = [n.lower() for n in (record.get("speaker_names") or {}).values()]
        stakeholders = [(s.get("name") or "").lower() for s in ((record.get("summary") or {}).get("stakeholders") or [])]
        transcript = (record.get("transcript") or "")
        summary_text = (record.get("summary") or {}).get("summary") or ""
        haystack = f"{record.get('name', '')} {transcript} {summary_text}".lower()
        if needle in names or needle in stakeholders or needle in haystack:
            text = summary_text or transcript
            if text:
                matches.append({"source": "local", "date": date, "text": text})
    return matches


def _search_obsidian_vault(keyword: str, start=None, end=None) -> list:
    vault_path = settings.get_all().get("obsidian_vault_path")
    if not vault_path or not os.path.isdir(vault_path):
        return []
    needle = keyword.lower()
    matches = []
    for root, _dirs, files in os.walk(vault_path):
        for fname in files:
            if not fname.endswith(".md"):
                continue
            path = os.path.join(root, fname)
            try:
                mtime = datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc).date().isoformat()
                if start and mtime < start:
                    continue
                if end and mtime > end:
                    continue
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    text = f.read()
            except OSError:
                continue
            if needle in text.lower():
                matches.append({"source": "obsidian", "date": mtime, "text": text[:2000]})
    return matches


def find_context(keyword: str, time_range: str = None):
    """Real retrieval across everything the pipeline already writes to --
    local recordings store (by name, date, transcript, or summary text),
    Notion (Notes/Journal, via a substring match on page content -- see
    notion_sync.query_pages_mentioning), Obsidian vault -- merged, deduped,
    and (if more than one match) summarized into one string via the
    configured LLM. `keyword` can be a person's name, a project name, a
    topic, or any other phrase -- this is intentionally general (not
    person-specific) so Jarvis can pull context from any past conversation,
    not just ones naming a specific person. Returns None if nothing found
    anywhere."""
    if not keyword:
        return None
    start, end = _parse_time_range(time_range)
    results = _search_local_recordings(keyword, start, end)

    try:
        import notion_sync
        results.extend(notion_sync.query_pages_mentioning(keyword, start, end))
    except Exception as e:
        log.debug("Notion context search for %r failed: %s", keyword, e)

    try:
        results.extend(_search_obsidian_vault(keyword, start, end))
    except Exception as e:
        log.debug("Obsidian context search for %r failed: %s", keyword, e)

    if not results:
        return None

    seen = set()
    deduped = []
    for r in results:
        key = (r.get("date"), (r.get("text") or "")[:80])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)

    if len(deduped) == 1:
        return deduped[0]["text"]

    combined = "\n\n".join(f"[{r.get('date', 'unknown date')}] {r['text']}" for r in deduped)
    _, complete = get_completer()
    prompt = (
        f"Summarize these notes related to \"{keyword}\" into one concise paragraph, "
        "suitable to be read aloud or acted on:\n\n" + combined
    )
    try:
        return complete(prompt).strip()
    except Exception as e:
        log.debug("context summarization failed, returning raw combined text: %s", e)
        return combined


def find_person_context(person_name: str, time_range: str = None):
    """Thin, name-preserving alias -- existing call sites read more clearly
    with a person-specific name even though find_context is the general
    implementation underneath."""
    return find_context(person_name, time_range)


def _lookup_email_for_name(name: str) -> str:
    import poller
    return poller._lookup_email_for_name(name)


def _parse_subject_body(raw_text: str, recipient_name: str, fallback_query: str):
    text = raw_text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        data = json.loads(text)
        return data.get("subject") or f"Message for {recipient_name}", data.get("body") or fallback_query
    except (json.JSONDecodeError, AttributeError):
        return f"Message for {recipient_name}", raw_text.strip() or fallback_query


# --- Obsidian logging / Windows fallback storage ----------------------------
# On Windows (no AppleScript/Outlook COM automation yet -- see module
# docstring), a connected Obsidian vault is the REAL implementation for
# calendar_event/reminder/email_draft below, not just a log of them: with
# no vault configured those actions genuinely can't do anything on this
# platform, so this is the difference between "not available" and "works."

def _obsidian_jarvis_dir():
    vault_path = settings.get_all().get("obsidian_vault_path")
    if not vault_path or not os.path.isdir(vault_path):
        return None
    jarvis_dir = os.path.join(vault_path, "Jarvis")
    os.makedirs(jarvis_dir, exist_ok=True)
    return jarvis_dir


def _write_obsidian_note(action_type: str, frontmatter_extra: dict, body_lines: list) -> bool:
    jarvis_dir = _obsidian_jarvis_dir()
    if not jarvis_dir:
        return False
    try:
        now = datetime.now()
        fname = f"{now:%Y-%m-%d-%H%M%S}-{action_type}.md"
        path = os.path.join(jarvis_dir, fname)
        lines = ["---", f"date: {now:%Y-%m-%d %H:%M:%S}", f"action_type: {action_type}"]
        for k, v in frontmatter_extra.items():
            lines.append(f"{k}: {v}")
        lines += ["---", ""] + body_lines
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        return True
    except Exception as e:
        log.warning("Obsidian note write failed: %s", e)
        return False


def _log_to_obsidian(action_type: str, decision: dict, result: dict):
    """Best-effort audit log for every Jarvis command -- see macOS jarvis.py
    for the parity version. On Windows this only fires for action types
    that DIDN'T already write their own note above (calendar_event/
    reminder/email_draft write directly in their own handler instead, so
    this doesn't double-log them)."""
    if action_type in ("calendar_event", "reminder", "email_draft"):
        return
    body = [f"**Heard:** {decision.get('query') or ''}", ""]
    if result.get("spoken"):
        body += [f"**Replied:** {result['spoken']}", ""]
    _write_obsidian_note(action_type, {"ok": "true" if result.get("ok") else "false"}, body)


# --- local-app action execution ---------------------------------------------

def _action_open_app(decision: dict) -> dict:
    app_name = decision.get("app_name") or decision.get("query")
    if not app_name:
        return {"ok": False, "spoken": "I didn't catch which app to open."}
    try:
        # Windows has no direct equivalent of macOS's "open -a <app name>"
        # (fuzzy app-name resolution) -- `start` via the shell resolves a
        # registered app name/executable on PATH, which covers the common
        # case (browsers, Office, etc.) without needing a full path.
        subprocess.run(f'start "" "{app_name}"', shell=True, check=True, timeout=10)
        return {"ok": True, "spoken": f"Opened {app_name}."}
    except Exception as e:
        log.warning("open_app failed for %r: %s", app_name, e)
        return {"ok": False, "spoken": f"Couldn't open {app_name}."}


def _action_calendar_event(decision: dict) -> dict:
    """No Outlook COM automation yet (see module docstring) -- if Obsidian
    is connected, the event is recorded as a structured note instead (the
    real, working implementation on Windows today); otherwise this is
    genuinely unavailable."""
    title = decision.get("title") or decision.get("query") or "New event"
    when = " ".join(filter(None, [decision.get("date"), decision.get("time")]))
    if _write_obsidian_note("calendar_event", {}, [f"**{title}**" + (f" — {when}" if when else ""), ""]):
        return {"ok": True, "spoken": f"Added {title} to your Jarvis notes in Obsidian" + (f" for {when}" if when else "") + ".", "session": None}
    return {"ok": False, "spoken": "Calendar events via Jarvis need Obsidian connected on Windows (Settings -> Obsidian) -- Outlook automation isn't available yet.", "session": None}


def _action_reminder(decision: dict) -> dict:
    """Same Obsidian-backed approach as _action_calendar_event."""
    title = decision.get("title") or decision.get("query") or "New reminder"
    if _write_obsidian_note("reminder", {}, [f"**{title}**", ""]):
        return {"ok": True, "spoken": f"Added a reminder to your Jarvis notes in Obsidian: {title}.", "session": None}
    return {"ok": False, "spoken": "Reminders via Jarvis need Obsidian connected on Windows (Settings -> Obsidian) -- Outlook automation isn't available yet.", "session": None}


def _action_email_draft(decision: dict, session: dict) -> dict:
    """No Outlook COM automation yet (see module docstring) -- if Obsidian
    is connected, the draft is written as a note (subject/body/recipient)
    for you to send manually from your own email client; otherwise this is
    genuinely unavailable. Unlike macOS's Mail.app flow, there's no
    multi-turn "read it back / send it" session here -- the note is the
    final artifact."""
    recipient_name = decision.get("recipient_name") or ""
    to_email = _lookup_email_for_name(recipient_name) if recipient_name else ""
    context_keyword = decision.get("referenced_person") or decision.get("referenced_topic")
    context_text = find_context(context_keyword, decision.get("referenced_time_range")) if context_keyword else None

    _, complete = get_completer()
    compose_prompt = (
        f"Draft a short, natural email to {recipient_name} about: {decision.get('query') or ''}\n"
        + (f"\nRelevant context from past recordings related to \"{context_keyword}\":\n{context_text}\n" if context_text else "")
        + '\nReturn ONLY JSON: {"subject": "...", "body": "..."}'
    )
    try:
        raw = complete(compose_prompt)
    except Exception as e:
        log.warning("email compose failed: %s", e)
        return {"ok": False, "spoken": "I couldn't draft that email just now.", "session": None}
    subject, body = _parse_subject_body(raw, recipient_name, decision.get("query") or "")

    note_body = [
        f"**To:** {recipient_name}" + (f" <{to_email}>" if to_email else " (no address found)"),
        f"**Subject:** {subject}", "", body, "",
    ]
    if _write_obsidian_note("email_draft", {}, note_body):
        return {"ok": True, "spoken": f"Drafted an email to {recipient_name} about {subject} -- saved to your Jarvis notes in Obsidian to send from your own email client.", "session": None}
    return {"ok": False, "spoken": "Email drafting via Jarvis needs Obsidian connected on Windows (Settings -> Obsidian) -- Outlook automation isn't available yet.", "session": None}


def _dispatch_gui_automation(target_app: str, text: str) -> dict:
    """Not yet implemented on Windows -- see module docstring. A real
    version would need a Windows UI-automation equivalent of AppleScript's
    System Events keystroke approach (e.g. pywinauto)."""
    return {"ok": False, "spoken": f"Sending to {target_app} isn't available on Windows yet.", "session": None}


def _recent_journal_context(time_range=None) -> str:
    """Fallback source for "write a post based on my journal" with no named
    person -- most recent journal-type recording(s), optionally windowed by
    time_range."""
    start, end = _parse_time_range(time_range)
    texts = []
    for record in storage.list_recordings():
        if (record.get("summary") or {}).get("type") != "journal":
            continue
        date = (record.get("created_at") or "")[:10]
        if start and date < start:
            continue
        if end and date > end:
            continue
        text = record.get("transcript") or (record.get("summary") or {}).get("summary") or ""
        if text:
            texts.append((date, text))
        if len(texts) >= 5:
            break
    texts.sort(reverse=True)
    return "\n\n".join(t for _, t in texts)


def _action_social_post(decision: dict, record: dict) -> dict:
    """"Write me a post based on my journal / conversation with X" --
    generates via the same providers.base pipeline as the existing
    Notion-checkbox trigger (see poller.generate_social_posts), attached to
    THIS Jarvis command recording. Dashboard-first: always stored via
    storage.set_social_posts regardless of whether a backend is configured,
    then immediately tries to push to Notion Publications DB / Obsidian if
    one already is -- otherwise it just sits on the /social dashboard with
    a "Sync now" button once the user connects one (see app.py's
    /social/sync-now route)."""
    import poller

    context_keyword = decision.get("referenced_person") or decision.get("referenced_topic")
    if context_keyword:
        source_text = find_context(context_keyword, decision.get("referenced_time_range"))
        source_label = f"past recordings related to \"{context_keyword}\""
    else:
        source_text = _recent_journal_context(decision.get("referenced_time_range"))
        source_label = "your recent journal entries"

    if not source_text:
        return {"ok": False, "spoken": f"I couldn't find {source_label} to base a post on.", "session": None}

    try:
        posts = poller.generate_social_posts(source_text, meeting=None)
    except Exception as e:
        log.warning("social post generation failed: %s", e)
        return {"ok": False, "spoken": "I couldn't generate a post from that just now.", "session": None}
    if not posts:
        return {"ok": False, "spoken": "There wasn't enough there to turn into a real post.", "session": None}

    storage.set_social_posts(record["content_hash"], posts)
    push_result = poller.push_social_posts_now(record["content_hash"])
    if push_result.get("pushed"):
        spoken = f"Drafted a post based on {source_label} and saved it to your notes -- check the Social Posts page to review and publish."
    else:
        spoken = f"Drafted a post based on {source_label} -- it's on your Clicky dashboard's Social Posts page. Connect Notion or Obsidian in Settings to sync it there too."
    return {"ok": True, "spoken": spoken, "session": None}


def _action_qa(decision: dict, session: dict) -> dict:
    target_app = decision.get("target_app")
    if target_app:
        return _dispatch_gui_automation(target_app, decision.get("query") or "")

    # referenced_person and referenced_topic are both routed through the
    # same general find_context() -- a topic/project name works exactly
    # like a person's name for retrieval purposes (both are just a keyword
    # matched against names/dates/transcripts/summaries across everything
    # the pipeline has written, see find_context's docstring). This gives
    # Jarvis access to the whole recordings corpus by name, date,
    # conversation, or project -- not just person-referenced context.
    context_keyword = decision.get("referenced_person") or decision.get("referenced_topic")
    context_text = None
    if context_keyword:
        context_text = find_context(context_keyword, decision.get("referenced_time_range"))
        if context_text and not decision.get("query"):
            # Pure recall, e.g. "remind me what we talked about with Paul
            # last week" or "what happened with the Clicky project last
            # month" -- speak the retrieved context back directly, no
            # further LLM call needed.
            return {"ok": True, "spoken": context_text, "session": None}

    prior = ""
    if session and session.get("kind") == "qa":
        prior = "\n".join(session.get("context", {}).get("turns", []))
    prompt = decision.get("query") or ""
    if context_text:
        prompt = f"Context from past recordings related to \"{context_keyword}\":\n{context_text}\n\nQuestion: {prompt}"
    if prior:
        prompt = f"Prior conversation:\n{prior}\n\n{prompt}"

    _, complete = get_completer()
    try:
        answer = complete(prompt).strip()
    except Exception as e:
        log.warning("qa completion failed: %s", e)
        return {"ok": False, "spoken": "I couldn't reach the language model just now.", "session": None}

    turns = (session.get("context", {}).get("turns") if session else []) or []
    turns = turns + [f"Q: {prompt}", f"A: {answer}"]
    new_session = {"kind": "qa", "opened_at": datetime.now(timezone.utc).isoformat(), "context": {"turns": turns[-6:]}}
    return {"ok": True, "spoken": answer, "session": new_session}


def _action_code_task(decision: dict) -> dict:
    """Always the real Claude Code CLI -- never GUI automation, since only
    the CLI has real file/repo access. cwd is set via subprocess's own
    cwd= (there is no --cwd flag); per-project session continuity via
    --resume, using the id persisted from a prior call's JSON output."""
    repo_path = settings.get_all().get("jarvis_repo_path")
    if not repo_path or not os.path.isdir(repo_path):
        return {"ok": False, "spoken": "No Jarvis project folder is configured yet -- set it in Settings.", "session": None}
    prompt = decision.get("query") or ""

    sessions = _load_code_sessions()
    existing = sessions.get(repo_path)
    cmd = ["claude"]
    if existing:
        cmd += ["--resume", existing]
    cmd += ["-p", prompt, "--output-format", "json"]

    try:
        result = subprocess.run(cmd, cwd=repo_path, timeout=600, capture_output=True, text=True, check=True, shell=True)
    except FileNotFoundError:
        return {"ok": False, "spoken": "The Claude CLI isn't installed on this machine yet.", "session": None}
    except subprocess.CalledProcessError as e:
        log.warning("claude CLI failed: %s", (e.stderr or "")[:500])
        return {"ok": False, "spoken": "Claude Code ran into an error working on that.", "session": None}
    except subprocess.TimeoutExpired:
        return {"ok": False, "spoken": "That coding task is taking a while -- check it directly.", "session": None}

    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        data = {}
    session_id = data.get("session_id")
    if session_id:
        sessions[repo_path] = session_id
        _save_code_sessions(sessions)
    return {"ok": True, "spoken": "Claude Code finished working on that.", "session": None}


# --- speech I/O --------------------------------------------------------------

def speak(text: str) -> bytes:
    """Not yet implemented on Windows -- see module docstring. macOS's
    `say --data-format=LEI16@16000` gives an exact 16kHz/16-bit match for
    the firmware's codec for free; Windows SAPI (via a PowerShell
    System.Speech call) doesn't offer that same sample-rate control as
    directly, so this needs its own research pass (resample after
    synthesis, or find an SAPI voice/output format that already matches)
    rather than shipping a mismatched-sample-rate reply that would play at
    the wrong speed/pitch on the device."""
    raise NotImplementedError("Jarvis spoken replies aren't implemented on Windows yet")


def send_audio_reply(wav_bytes: bytes):
    """Uploads a spoken reply for on-device playback via the WiFi-only
    /jarvis/audio endpoint (see wifi_sync.cpp). Skipped (logged, not
    raised) if WiFi isn't currently reachable -- BLE can't handle a payload
    this size (see the firmware's own doc on this)."""
    import poller
    base_url = poller.wifi_base_url_if_reachable()
    if not base_url:
        log.info("Jarvis reply ready but WiFi isn't reachable -- skipping playback")
        return
    import device_client
    try:
        device_client.send_jarvis_audio(wav_bytes, base_url)
    except Exception as e:
        log.warning("Jarvis audio reply upload failed: %s", e)


# --- top-level entry point ----------------------------------------------------

def process_command(record: dict, transcript: str) -> dict:
    """Called from poller.process_once() for kind=="command" recordings
    instead of the memo summarize()/Notion/Obsidian pipeline. Returns a dict
    describing what happened (transcript + decided action + result) for the
    dashboard to show in place of a summary."""
    global _session
    decision = decide_action(transcript, _session if _session.get("kind") else None)

    if not decision.get("continues_session"):
        _session = {}

    action_type = decision.get("action_type")
    if action_type == "open_app":
        result = _action_open_app(decision)
    elif action_type == "calendar_event":
        result = _action_calendar_event(decision)
    elif action_type == "reminder":
        result = _action_reminder(decision)
    elif action_type == "email_draft":
        result = _action_email_draft(decision, _session)
    elif action_type == "social_post":
        result = _action_social_post(decision, record)
    elif action_type == "qa":
        result = _action_qa(decision, _session)
    elif action_type == "code_task":
        result = _action_code_task(decision)
    else:
        result = {"ok": False, "spoken": "I didn't understand that command."}
    result.setdefault("session", None)

    _session = result.get("session") or {}

    _log_to_obsidian(action_type, decision, result)

    try:
        wav_bytes = speak(result.get("spoken") or "")
        send_audio_reply(wav_bytes)
    except Exception as e:
        log.warning("Jarvis spoken reply failed (action still executed): %s", e)

    try:
        import analytics
        analytics.track_event("jarvis_commands")
    except Exception:
        pass

    return {
        "transcript": transcript,
        "action_type": action_type,
        "ok": result.get("ok", False),
        "spoken": result.get("spoken"),
    }
