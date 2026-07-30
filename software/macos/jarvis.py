"""Jarvis: BOOT-captured voice commands (cmd_*.wav) are routed here instead
of the memo pipeline (see poller.process_once's kind=="command" branch).

Scope, per explicit user direction: the cloud LLM (get_completer(), whatever
provider is configured -- Mistral today) is the answer engine for "qa" and
also handles the small routing/composition steps any voice assistant needs
(decide_action's dispatch call, email drafting text, multi-source recall
summarization) -- but no Jarvis ACTION executes via a cloud API. Calendar/
Reminders/Email dispatch to local Mac apps via AppleScript; genuine coding
tasks always go through the real Claude Code CLI (never GUI automation --
only the CLI has real file/repo access); "qa" only falls back to GUI
automation when the user explicitly names a different assistant/app.
"""
import io
import json
import logging
import os
import subprocess
import tempfile
import wave
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
    """Thin, name-preserving alias -- existing call sites (email_draft's
    "based on the conversation with Paul" flow) read more clearly with a
    person-specific name even though find_context is the general
    implementation underneath."""
    return find_context(person_name, time_range)


def _lookup_email_for_name(name: str) -> str:
    import poller
    return poller._lookup_email_for_name(name)


# --- local-app action execution ---------------------------------------------

def _escape_applescript(text: str) -> str:
    return (text or "").replace("\\", "\\\\").replace('"', '\\"')


def _osascript(script: str, timeout: int = 15, capture: bool = False):
    return subprocess.run(
        ["osascript", "-e", script], timeout=timeout, check=True, capture_output=True, text=True,
    )


def _action_open_app(decision: dict) -> dict:
    app_name = decision.get("app_name") or decision.get("query")
    if not app_name:
        return {"ok": False, "spoken": "I didn't catch which app to open."}
    try:
        subprocess.run(["open", "-a", app_name], check=True, timeout=10)
        return {"ok": True, "spoken": f"Opened {app_name}."}
    except Exception as e:
        log.warning("open_app failed for %r: %s", app_name, e)
        return {"ok": False, "spoken": f"Couldn't open {app_name}."}


def _action_calendar_event(decision: dict) -> dict:
    title = decision.get("title") or decision.get("query") or "New event"
    date_str = decision.get("date")
    time_str = decision.get("time") or "09:00"
    if not date_str:
        return {"ok": False, "spoken": "I didn't catch a date for that event."}
    try:
        start_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    except ValueError:
        return {"ok": False, "spoken": "I couldn't parse the date and time for that event."}
    end_dt = start_dt + timedelta(hours=1)
    fmt = "%A, %B %-d, %Y at %-I:%M:%S %p"
    script = (
        'tell application "Calendar"\n'
        "  tell calendar 1\n"
        f'    make new event with properties {{summary:"{_escape_applescript(title)}", '
        f'start date:date "{start_dt.strftime(fmt)}", end date:date "{end_dt.strftime(fmt)}"}}\n'
        "  end tell\n"
        "end tell"
    )
    try:
        _osascript(script)
        return {"ok": True, "spoken": f"Added {title} to your calendar for {date_str} at {time_str}."}
    except Exception as e:
        log.warning("calendar_event failed: %s", e)
        return {"ok": False, "spoken": "I couldn't add that to Calendar -- it may need Automation permission granted in System Settings."}


def _action_reminder(decision: dict) -> dict:
    title = decision.get("title") or decision.get("query") or "New reminder"
    script = (
        'tell application "Reminders"\n'
        f'  make new reminder with properties {{name:"{_escape_applescript(title)}"}}\n'
        "end tell"
    )
    try:
        _osascript(script)
        return {"ok": True, "spoken": f"Added a reminder: {title}."}
    except Exception as e:
        log.warning("reminder failed: %s", e)
        return {"ok": False, "spoken": "I couldn't add that reminder -- it may need Automation permission granted in System Settings."}


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


def _mail_create_draft(subject: str, body: str, to_email: str) -> str:
    """Composes a real draft in Mail.app (visible, never sent by this call)
    and returns its AppleScript message id, so a later 'send it' turn can
    look it up again. No Gmail API, no google_client involvement."""
    script = (
        'tell application "Mail"\n'
        f'  set newMsg to make new outgoing message with properties {{subject:"{_escape_applescript(subject)}", '
        f'content:"{_escape_applescript(body)}", visible:true}}\n'
        "  tell newMsg\n"
        f'    make new to recipient with properties {{address:"{_escape_applescript(to_email)}"}}\n'
        "  end tell\n"
        "  return id of newMsg\n"
        "end tell"
    )
    result = _osascript(script, capture=True)
    return result.stdout.strip()


def _mail_send_draft(message_id: str):
    script = (
        'tell application "Mail"\n'
        f"  send (first outgoing message whose id is {message_id})\n"
        "end tell"
    )
    _osascript(script)


def _mail_delete_draft(message_id: str):
    script = (
        'tell application "Mail"\n'
        f"  delete (first outgoing message whose id is {message_id})\n"
        "end tell"
    )
    _osascript(script)


def _action_email_draft(decision: dict, session: dict) -> dict:
    if session and session.get("kind") == "email_draft":
        return _continue_email_draft(decision, session)

    recipient_name = decision.get("recipient_name") or ""
    to_email = _lookup_email_for_name(recipient_name) if recipient_name else ""
    if not to_email:
        return {"ok": False, "spoken": f"I couldn't find an email address for {recipient_name or 'that person'}.", "session": None}

    context_keyword = decision.get("referenced_person") or decision.get("referenced_topic")
    context_text = None
    if context_keyword:
        context_text = find_context(context_keyword, decision.get("referenced_time_range"))

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

    try:
        message_id = _mail_create_draft(subject, body, to_email)
    except Exception as e:
        log.warning("email draft creation failed: %s", e)
        return {"ok": False, "spoken": "I couldn't create that draft in Mail -- it may need Automation permission granted in System Settings.", "session": None}

    new_session = {
        "kind": "email_draft",
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "context": {"message_id": message_id, "subject": subject, "body": body, "to": to_email, "recipient_name": recipient_name},
    }
    return {"ok": True, "spoken": f"Drafted an email to {recipient_name} about {subject} in Mail. Want me to read it back, or say send it.", "session": new_session}


def _continue_email_draft(decision: dict, session: dict) -> dict:
    ctx = session["context"]
    query = (decision.get("query") or "").strip().lower()
    if "send" in query or "approve" in query:
        try:
            _mail_send_draft(ctx["message_id"])
        except Exception as e:
            log.warning("email send failed: %s", e)
            return {"ok": False, "spoken": "I couldn't send that -- check the draft in Mail.", "session": session}
        return {"ok": True, "spoken": "Sent.", "session": None}
    if "read" in query or query in ("yes", "yeah", "sure", "yep"):
        return {"ok": True, "spoken": f"Subject: {ctx['subject']}. {ctx['body']}", "session": session}

    # Otherwise treat as an edit instruction -- revise via the LLM, then
    # recreate the draft with the new content (AppleScript has no reliable
    # "edit content of an existing outgoing message" across Mail versions)
    # and clean up the old one.
    _, complete = get_completer()
    revise_prompt = (
        f"Current email draft -- subject: {ctx['subject']}\nbody: {ctx['body']}\n\n"
        f"Apply this change: {decision.get('query')}\n"
        'Return ONLY JSON: {"subject": "...", "body": "..."}'
    )
    try:
        raw = complete(revise_prompt)
    except Exception as e:
        log.warning("email revise failed: %s", e)
        return {"ok": False, "spoken": "I couldn't apply that change.", "session": session}
    subject, body = _parse_subject_body(raw, ctx.get("recipient_name", ""), decision.get("query") or "")

    try:
        _mail_delete_draft(ctx["message_id"])
    except Exception as e:
        log.debug("could not clean up previous draft: %s", e)
    try:
        message_id = _mail_create_draft(subject, body, ctx["to"])
    except Exception as e:
        log.warning("email draft revision failed: %s", e)
        return {"ok": False, "spoken": "I couldn't apply that change.", "session": session}

    new_session = dict(session)
    new_session["context"] = {**ctx, "message_id": message_id, "subject": subject, "body": body}
    return {"ok": True, "spoken": f"Updated -- subject is now {subject}. Say send it when ready.", "session": new_session}


def _dispatch_gui_automation(target_app: str, text: str) -> dict:
    """Only reached when the user explicitly named a different assistant/
    app for 'qa' -- fire-and-forget: activates the app and types the
    prompt in, but doesn't read its reply back (Phase 2 work)."""
    try:
        subprocess.run(["open", "-a", target_app], check=True, timeout=10)
    except Exception as e:
        log.warning("could not open %r for GUI automation: %s", target_app, e)
        return {"ok": False, "spoken": f"I couldn't open {target_app}.", "session": None}
    import time as _time
    _time.sleep(1.5)  # let the app come to the foreground before typing into it
    script = (
        f'set the clipboard to "{_escape_applescript(text)}"\n'
        'tell application "System Events"\n'
        '  keystroke "v" using command down\n'
        "  delay 0.3\n"
        "  key code 36\n"
        "end tell"
    )
    try:
        _osascript(script)
    except Exception as e:
        log.warning("GUI automation into %r failed: %s", target_app, e)
        return {"ok": False, "spoken": f"I couldn't type that into {target_app} -- it may need Accessibility permission granted in System Settings.", "session": None}
    return {"ok": True, "spoken": f"Sent to {target_app}.", "session": None}


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
        result = subprocess.run(cmd, cwd=repo_path, timeout=600, capture_output=True, text=True, check=True)
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

def _mono_wav_to_stereo(mono_wav: bytes) -> bytes:
    """Duplicates each sample into interleaved L+R channels -- avoids the
    audioop module (removed in Python 3.13) for a conversion this simple.
    The firmware's codec is opened at 16kHz/stereo/16-bit (see audio_bsp.c);
    `say` only ever produces mono, so this has to happen before upload or
    playback would be garbled/wrong-speed (see recorder_play_wav's own
    comment on this)."""
    with wave.open(io.BytesIO(mono_wav), "rb") as src:
        params = src.getparams()
        frames = src.readframes(params.nframes)
    sampwidth = params.sampwidth
    stereo = bytearray(len(frames) * 2)
    for i in range(0, len(frames), sampwidth):
        sample = frames[i:i + sampwidth]
        stereo[i * 2:i * 2 + sampwidth] = sample
        stereo[i * 2 + sampwidth:i * 2 + sampwidth * 2] = sample
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(2)
        out.setsampwidth(sampwidth)
        out.setframerate(params.framerate)
        out.writeframes(bytes(stereo))
    return buf.getvalue()


def speak(text: str) -> bytes:
    """Local macOS TTS -- `say --data-format=LEI16@16000 --file-format=WAVE`
    produces 16-bit/16kHz linear PCM mono WAV (confirmed via `man say`),
    fully offline/free. Converted to stereo before returning (see
    _mono_wav_to_stereo)."""
    if not text:
        text = "Done."
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        subprocess.run(
            ["say", "--data-format=LEI16@16000", "--file-format=WAVE", "-o", tmp_path, text],
            timeout=30, check=True, capture_output=True,
        )
        with open(tmp_path, "rb") as f:
            mono_wav = f.read()
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
    return _mono_wav_to_stereo(mono_wav)


def send_audio_reply(wav_bytes: bytes):
    """Uploads a spoken reply for on-device playback via the WiFi-only
    /jarvis/audio endpoint (see wifi_sync.cpp). Skipped (logged, not
    raised) if WiFi isn't currently reachable -- BLE can't handle a payload
    this size (see the firmware's own doc on this); in practice WiFi is
    already on for the whole Jarvis round-trip anyway, since the recording
    sync itself opens the same radio session."""
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


# --- Obsidian logging ---------------------------------------------------------

def _obsidian_jarvis_dir():
    vault_path = settings.get_all().get("obsidian_vault_path")
    if not vault_path or not os.path.isdir(vault_path):
        return None
    jarvis_dir = os.path.join(vault_path, "Jarvis")
    os.makedirs(jarvis_dir, exist_ok=True)
    return jarvis_dir


def _log_to_obsidian(action_type: str, decision: dict, result: dict):
    """Writes a structured note for every Jarvis command to the Obsidian
    vault (if one is configured) -- a persistent, local, cross-platform
    record of everything Jarvis did, independent of whatever native-app
    automation did or didn't succeed. On Windows (no AppleScript/Outlook
    automation -- see the Windows jarvis.py's action stubs), this note IS
    the actual calendar_event/reminder/email_draft output, not just a log
    of it -- see that module's docstring. Best-effort: never raises, a
    logging failure must never take down the command that just ran."""
    jarvis_dir = _obsidian_jarvis_dir()
    if not jarvis_dir:
        return
    try:
        now = datetime.now()
        fname = f"{now:%Y-%m-%d-%H%M%S}-{action_type}.md"
        path = os.path.join(jarvis_dir, fname)
        lines = [
            "---",
            f"date: {now:%Y-%m-%d %H:%M:%S}",
            f"action_type: {action_type}",
            f"ok: {'true' if result.get('ok') else 'false'}",
            "---",
            "",
            f"**Heard:** {decision.get('query') or ''}",
            "",
        ]
        if result.get("spoken"):
            lines += [f"**Replied:** {result['spoken']}", ""]
        # Action-specific structured details, so this note is genuinely
        # usable as a record of what was drafted/scheduled -- not just a
        # transcript, which the memo pipeline already keeps separately.
        if action_type == "email_draft" and decision.get("recipient_name"):
            lines += [f"**To:** {decision['recipient_name']}", ""]
        if action_type in ("calendar_event", "reminder") and decision.get("title"):
            when = " ".join(filter(None, [decision.get("date"), decision.get("time")]))
            lines += [f"**{decision.get('title')}**" + (f" — {when}" if when else ""), ""]
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    except Exception as e:
        log.warning("Obsidian logging for Jarvis command failed (non-fatal): %s", e)


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
