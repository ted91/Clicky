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
import re
import subprocess
import tempfile
import wave
from datetime import datetime, timedelta, timezone

import memory_store
import rag_index
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
            "target_app": None, "query": raw_text.strip(), "direct_answer": None, "snippet_text": None,
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
        "direct_answer": data.get("direct_answer") or None,
        "snippet_text": data.get("snippet_text") or None,
    }


def decide_action(transcript: str, session: dict = None) -> dict:
    """The one dispatcher call that has to exist to route any transcript at
    all -- decides action type AND session continuity in one shot, per the
    user's explicit choice that continuation be context-based rather than a
    fixed timeout."""
    session = session or {}
    facts_block = memory_store.facts_context()
    context_block = ""
    if session:
        context_block = "\n\n[An action flow is currently open: " + json.dumps(session.get("context", {})) + \
                         " -- decide whether this command continues it.]"
    prompt = (
        "You route one spoken voice command for a personal-assistant device to exactly one action "
        "type. Return ONLY JSON (no markdown fences, no commentary) matching exactly:\n"
        '{"action_type": "open_app" | "calendar_event" | "reminder" | "email_draft" | "social_post" | "qa" | "code_task" | "save_snippet" | "unknown", '
        '"continues_session": true or false, '
        '"app_name": "app name or null", '
        '"title": "event/reminder title or null", '
        '"date": "YYYY-MM-DD or null", '
        '"time": "HH:MM or null", '
        '"recipient_name": "person name or null (email_draft recipient)", '
        '"referenced_person": "person name or null (if this references a past conversation with someone)", '
        '"referenced_topic": "a SPECIFIC, NAMED project/topic or null -- e.g. \\"the Clicky project\\", '
        '\\"my trip planning\\". Must be null for a generic, self-referential phrase with no specific '
        'subject named, such as \\"my journal\\", \\"our previous conversation\\", \\"previous journals\\", '
        '\\"past entries\\", or \\"what we talked about\\" -- these mean \\"my journal history in general\\", '
        'not a named topic, and forcing one of these phrases into this field breaks retrieval (there is no '
        'recording literally titled or about \\"previous journals\\"). Leave this null for those cases; the '
        'fallback path already searches recent journal entries generically.", '
        '"referenced_time_range": "e.g. \\"last week\\"/\\"yesterday\\"/\\"last month\\", or null", '
        '"target_app": "an assistant/app name the user explicitly named (e.g. ChatGPT, Cursor), or null", '
        '"query": "the actual question/instruction text to act on", '
        '"direct_answer": "ONLY for action_type=qa with no target_app -- your own terse spoken answer to '
        'the question RIGHT NOW, in the fewest words possible (a number/fact gets just the value and unit, '
        'e.g. \\"18 degrees\\", never a full sentence; no pleasantries, no follow-up offers). null for '
        'anything needing retrieval from past recordings, a named assistant, or any non-qa action_type." '
        '"snippet_text": "ONLY for action_type=save_snippet -- the actual content being saved, e.g. what was '
        'just said or referenced (\\"save this\\"/\\"remember that\\"/\\"save that snippet\\"). null otherwise."}\n'
        "action_type meanings: open_app = launch a named app. calendar_event = add a calendar event. "
        "reminder = add a reminder. email_draft = compose an email (recipient_name is who the email goes "
        "TO -- separate from referenced_person, who the command's CONTEXT comes from, e.g. \"based on the "
        "conversation with Paul, email Jeremie about...\" has recipient_name=Jeremie, referenced_person=Paul). "
        "social_post = a request to write/draft a social/blog post based on the speaker's journal or a "
        "past conversation with someone (referenced_person is who that conversation was with, or null for "
        "\"my journal\"/their own recent reflections). "
        "qa = a general question, a pure recall request (\"remind me what we discussed with X\"), or a "
        "request to continue a chat with a named assistant (target_app). code_task = build/fix/write code "
        "or continue a coding project. save_snippet = an explicit instruction to save/remember a piece of "
        "content just said or referenced (\"save this\", \"remember that\", \"save that snippet\") -- not a "
        "general note-taking request, only this specific save/remember phrasing. unknown = doesn't clearly "
        "fit any of the above.\n"
        f"Resolve relative dates against today's actual date: {datetime.now():%Y-%m-%d}."
        + context_block +
        (f"\n\n{facts_block}" if facts_block else "") +
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


def search_memory(query: str, time_range: str = None, speaker: str = None):
    """Real semantic retrieval across everything the pipeline indexes --
    recordings, Notion Notes/Journal, Obsidian vault (see rag_index.py) --
    via embedding similarity rather than literal substring matching, so a
    query that's phrased differently from the source recording still
    matches on meaning. `query` can be a person's name, a project name, a
    topic, a full question, or any other phrase. date_range/speaker are
    cheap exact pre-filters (see rag_index.search), not part of the
    similarity ranking itself. Returns None if nothing scores above
    rag_index.MIN_SCORE.

    This exists so Jarvis's decisions (who "Paul" is, what "the budget
    thing" refers to, what an email should actually say) are correct --
    the retrieved text is consumed internally to inform an action/answer,
    not read back verbatim unless the user explicitly asks (e.g. the
    email-draft "read it back" flow)."""
    if not query:
        return None
    start, end = _parse_time_range(time_range)
    results = rag_index.search(query, date_start=start, date_end=end, speaker=speaker)
    if not results:
        return None

    if len(results) == 1:
        return results[0]["text"]

    combined = "\n\n".join(f"[{r.get('date') or 'unknown date'}] {r['text']}" for r in results)
    _, complete = get_completer()
    prompt = (
        f"Summarize these notes related to \"{query}\" into one concise paragraph, "
        "suitable to be acted on:\n\n" + combined
    )
    try:
        return complete(prompt).strip()
    except Exception as e:
        log.debug("context summarization failed, returning raw combined text: %s", e)
        return combined


# Old name, kept as a thin alias so existing call sites (_action_email_draft,
# _action_social_post, _action_qa, find_person_context) don't all need
# touching -- new code should call search_memory() directly.
def find_context(keyword: str, time_range: str = None):
    return search_memory(keyword, time_range)


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
        return {"ok": False, "spoken": "Not done -- didn't catch which app."}
    try:
        subprocess.run(["open", "-a", app_name], check=True, timeout=10)
        return {"ok": True, "spoken": "Done."}
    except Exception as e:
        log.warning("open_app failed for %r: %s", app_name, e)
        return {"ok": False, "spoken": f"Not done -- couldn't open {app_name}."}


def _action_calendar_event(decision: dict) -> dict:
    title = decision.get("title") or decision.get("query") or "New event"
    date_str = decision.get("date")
    time_str = decision.get("time") or "09:00"
    needs_date = not date_str
    if needs_date:
        # Missing info is not a hard stop -- default to today rather than
        # dropping the action, and flag the guess so the user can correct
        # it (mirrors the email_draft "act now, flag what's missing"
        # pattern). Same reasoning applies to any action field that's
        # fuzzy-but-not-blocking.
        date_str = datetime.now().strftime("%Y-%m-%d")
        _write_obsidian_note(
            "calendar_event_needs_info", {"ok": "true"},
            [f"**Needs:** a date for \"{title}\" (none was given -- defaulted to today, {date_str})", ""],
        )
    try:
        start_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    except ValueError:
        return {"ok": False, "spoken": "Not done -- couldn't parse the date."}
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
        spoken = "Done -- no date given, used today." if needs_date else "Done."
        return {"ok": True, "spoken": spoken}
    except Exception as e:
        log.warning("calendar_event failed: %s", e)
        return {"ok": False, "spoken": "Not done -- Calendar needs Automation permission in System Settings."}


def _action_reminder(decision: dict) -> dict:
    title = decision.get("title") or decision.get("query") or "New reminder"
    script = (
        'tell application "Reminders"\n'
        f'  make new reminder with properties {{name:"{_escape_applescript(title)}"}}\n'
        "end tell"
    )
    try:
        _osascript(script)
        return {"ok": True, "spoken": "Done."}
    except Exception as e:
        log.warning("reminder failed: %s", e)
        return {"ok": False, "spoken": "Not done -- Reminders needs Automation permission in System Settings."}


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


def _mail_create_draft(subject: str, body: str, to_email: str = "") -> str:
    """Composes a real draft in Mail.app (visible, never sent by this call)
    and returns its AppleScript message id, so a later 'send it' turn can
    look it up again. No Gmail API, no google_client involvement.

    to_email may be blank -- e.g. when the recipient couldn't be resolved,
    we still want a usable draft saved (subject/body filled in, To: left for
    the user to fill in) rather than no draft at all."""
    recipient_block = (
        "  tell newMsg\n"
        f'    make new to recipient with properties {{address:"{_escape_applescript(to_email)}"}}\n'
        "  end tell\n"
    ) if to_email else ""
    script = (
        'tell application "Mail"\n'
        f'  set newMsg to make new outgoing message with properties {{subject:"{_escape_applescript(subject)}", '
        f'content:"{_escape_applescript(body)}", visible:true}}\n'
        + recipient_block +
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


_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _action_email_draft(decision: dict, session: dict) -> dict:
    if session and session.get("kind") == "email_draft":
        return _continue_email_draft(decision, session)

    recipient_name = decision.get("recipient_name") or ""
    # The spoken command sometimes already IS a literal email address (e.g.
    # "email sanchit.gupta01@gmail.com about...") -- confirmed live that
    # decide_action still puts that into recipient_name (it's the correct
    # field, just not a "name"), and routing it through
    # _lookup_email_for_name (a Notion People NAME lookup) fails since an
    # email address is never a stored person's Name. Recognize this case
    # directly and skip the lookup entirely.
    if _EMAIL_RE.match(recipient_name.strip()):
        to_email = recipient_name.strip()
    else:
        to_email = _lookup_email_for_name(recipient_name) if recipient_name else ""

    needs_recipient = not to_email
    if needs_recipient:
        # Missing info is not a hard stop -- act on what we do know (draft
        # the actual email) and flag only the missing piece, rather than
        # producing nothing at all. Logs a "needs info" note (Obsidian, if
        # connected) with what WAS understood.
        _write_obsidian_note(
            "email_draft_needs_info", {"ok": "true"},
            [f"**Needs:** email address for \"{recipient_name or 'unknown recipient'}\"",
             f"**About:** {decision.get('query') or ''}", ""],
        )

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
        return {"ok": False, "spoken": "Not done -- couldn't draft it.", "session": None}
    subject, body = _parse_subject_body(raw, recipient_name, decision.get("query") or "")

    try:
        message_id = _mail_create_draft(subject, body, to_email)
    except Exception as e:
        log.warning("email draft creation failed: %s", e)
        return {"ok": False, "spoken": "Not done -- Mail needs Automation permission in System Settings.", "session": None}

    new_session = {
        "kind": "email_draft",
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "context": {"message_id": message_id, "subject": subject, "body": body, "to": to_email, "recipient_name": recipient_name},
    }
    spoken = "Done -- email address is missing, check the draft." if needs_recipient else "Done."
    return {"ok": True, "spoken": spoken, "session": new_session}


def _continue_email_draft(decision: dict, session: dict) -> dict:
    ctx = session["context"]
    query = (decision.get("query") or "").strip().lower()
    if "send" in query or "approve" in query:
        try:
            _mail_send_draft(ctx["message_id"])
        except Exception as e:
            log.warning("email send failed: %s", e)
            return {"ok": False, "spoken": "Not done -- check the draft in Mail.", "session": session}
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
        return {"ok": False, "spoken": "Not done.", "session": session}
    subject, body = _parse_subject_body(raw, ctx.get("recipient_name", ""), decision.get("query") or "")

    try:
        _mail_delete_draft(ctx["message_id"])
    except Exception as e:
        log.debug("could not clean up previous draft: %s", e)
    try:
        message_id = _mail_create_draft(subject, body, ctx["to"])
    except Exception as e:
        log.warning("email draft revision failed: %s", e)
        return {"ok": False, "spoken": "Not done.", "session": session}

    new_session = dict(session)
    new_session["context"] = {**ctx, "message_id": message_id, "subject": subject, "body": body}
    return {"ok": True, "spoken": "Done.", "session": new_session}


def _dispatch_gui_automation(target_app: str, text: str) -> dict:
    """Only reached when the user explicitly named a different assistant/
    app for 'qa' -- fire-and-forget: activates the app and types the
    prompt in, but doesn't read its reply back (Phase 2 work)."""
    try:
        subprocess.run(["open", "-a", target_app], check=True, timeout=10)
    except Exception as e:
        log.warning("could not open %r for GUI automation: %s", target_app, e)
        return {"ok": False, "spoken": f"Not done -- couldn't open {target_app}.", "session": None}
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
        return {"ok": False, "spoken": f"Not done -- {target_app} needs Accessibility permission in System Settings.", "session": None}
    return {"ok": True, "spoken": "Done.", "session": None}


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
        return {"ok": False, "spoken": f"Not done -- couldn't find {source_label}.", "session": None}

    try:
        posts = poller.generate_social_posts(source_text, meeting=None)
    except Exception as e:
        log.warning("social post generation failed: %s", e)
        return {"ok": False, "spoken": "Not done -- generation failed.", "session": None}
    if not posts:
        return {"ok": False, "spoken": "Not done -- not enough material.", "session": None}

    storage.set_social_posts(record["content_hash"], posts)
    poller.push_social_posts_now(record["content_hash"])
    return {"ok": True, "spoken": "Done -- check the Social Posts page.", "session": None}


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
    # decide_action() already answered this in its own single completion call
    # when it's a contextless factual/chat question (see its prompt's
    # "direct_answer" field) -- skip the second, sequential Mistral
    # round-trip entirely for that common case. Only applies with no prior
    # multi-turn session and no context lookup needed, both of which
    # decide_action can't have accounted for on its own.
    if decision.get("direct_answer") and not context_keyword and not (session and session.get("kind") == "qa"):
        return {"ok": True, "spoken": decision["direct_answer"], "session": None}

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
    prompt += ("\n\nAnswer in the fewest words possible for a spoken reply -- a fact/number gets just the "
               "value and unit, no filler, no pleasantries, no follow-up offers unless asked for detail.")

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


def _action_save_snippet(decision: dict) -> dict:
    """"Save this" / "remember that" / "save that snippet" -- writes the
    referenced content as its own Obsidian note (reusing _write_obsidian_note,
    same as every other Jarvis action) and, if configured, its own Notion
    page (reusing notion_sync.push_command via process_command's normal
    Notion push -- no separate call needed here, _log_to_obsidian/process_command
    already handle logging every action_type generically; this handler's job
    is only to produce a good `spoken` result and note body)."""
    snippet = decision.get("snippet_text") or decision.get("query") or ""
    if not snippet.strip():
        return {"ok": False, "spoken": "Not done -- I didn't catch what to save.", "session": None}
    snippet = snippet.strip()

    # Memory (memory_store) always gets this -- small, always-injected into
    # every future decide_action call (unlike rag_index, which is only
    # searched on demand). The Obsidian/Notion note below is a separate,
    # human-readable record of the same save, not a duplicate mechanism.
    try:
        memory_store.add_fact(snippet)
    except Exception as e:
        log.warning("memory_store write failed (non-fatal): %s", e)

    saved = _write_obsidian_note("save_snippet", {}, [snippet, ""])
    if saved:
        return {"ok": True, "spoken": "Saved.", "session": None}
    # Obsidian isn't configured -- still a real success, since memory_store
    # (above) has no such dependency and just worked.
    return {"ok": True, "spoken": "Saved.", "session": None}


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


def send_ack():
    """Quick tactile "done" click -- sent right after the decided action
    finishes executing, before the slower (and separately fallible) full
    spoken TTS reply. Best-effort: skipped silently if WiFi isn't reachable,
    same as send_audio_reply() -- there's no BLE equivalent for this yet."""
    import poller
    base_url = poller.wifi_base_url_if_reachable()
    if not base_url:
        return
    import device_client
    try:
        device_client.send_jarvis_ack(base_url)
    except Exception as e:
        log.debug("Jarvis ack click failed (non-fatal): %s", e)


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

def _write_obsidian_note(action_type: str, frontmatter_extra: dict, body_lines: list) -> bool:
    """General-purpose note writer -- used both by _log_to_obsidian's
    per-command audit trail and by individual actions (e.g. email_draft's
    "needs info" case) that want to write a specific, immediately-useful
    note rather than a generic log line. Returns False (no-op) if no vault
    is configured, so callers can fall back to a spoken-only message.

    Routed through obsidian_sync's shared _vault_subfolder/_write_note
    (same helpers Tasks/People/Calendar/Journal/Publications notes use --
    a "Jarvis" subfolder is exactly the same pattern as "Journal") instead
    of Jarvis hand-rolling its own vault path resolution and raw frontmatter
    string-building, which used to risk producing invalid YAML for any
    value containing a colon/quote -- yaml.safe_dump handles that
    correctly."""
    import obsidian_sync
    try:
        jarvis_dir = obsidian_sync._vault_subfolder("Jarvis")
    except RuntimeError:
        return False
    try:
        now = datetime.now()
        fname = f"{now:%Y-%m-%d-%H%M%S}-{action_type}.md"
        frontmatter = {"date": now.strftime("%Y-%m-%d %H:%M:%S"), "action_type": action_type, **frontmatter_extra}
        obsidian_sync._write_note(jarvis_dir, fname, frontmatter, "\n".join(body_lines))
        return True
    except Exception as e:
        log.warning("Obsidian note write failed: %s", e)
        return False


def _log_to_obsidian(action_type: str, decision: dict, result: dict):
    """Writes a structured note for every Jarvis command to the Obsidian
    vault (if one is configured) -- a persistent, local, cross-platform
    record of everything Jarvis did, independent of whatever native-app
    automation did or didn't succeed. On Windows (no AppleScript/Outlook
    automation -- see the Windows jarvis.py's action stubs), this note IS
    the actual calendar_event/reminder/email_draft output, not just a log
    of it -- see that module's docstring. Best-effort: never raises, a
    logging failure must never take down the command that just ran."""
    body_lines = [f"**Heard:** {decision.get('query') or ''}", ""]
    if result.get("spoken"):
        body_lines += [f"**Replied:** {result['spoken']}", ""]
    # Action-specific structured details, so this note is genuinely
    # usable as a record of what was drafted/scheduled -- not just a
    # transcript, which the memo pipeline already keeps separately.
    if action_type == "email_draft" and decision.get("recipient_name"):
        body_lines += [f"**To:** {decision['recipient_name']}", ""]
    if action_type in ("calendar_event", "reminder") and decision.get("title"):
        when = " ".join(filter(None, [decision.get("date"), decision.get("time")]))
        body_lines += [f"**{decision.get('title')}**" + (f" — {when}" if when else ""), ""]
    _write_obsidian_note(action_type, {"ok": "true" if result.get("ok") else "false"}, body_lines)


# --- top-level entry point ----------------------------------------------------

def process_command(record: dict, transcript: str, suppress_reply: bool = False) -> dict:
    """Called from poller.process_once() for kind=="command" recordings
    instead of the memo summarize()/Notion/Obsidian pipeline. Returns a dict
    describing what happened (transcript + decided action + result) for the
    dashboard to show in place of a summary.

    suppress_reply=True is used when this command is part of an offline
    backlog (recorded while the Mac was unreachable, only now being
    executed) -- per the user's explicit request, backlogged actions still
    execute for real, but get one short batch summary from the caller
    instead of each getting its own descriptive spoken reply. A qa command
    is a special case: a stale answer to an old question is worse than no
    answer, so a backlogged qa is never even asked here -- skip execution
    entirely and just record that it went unanswered."""
    global _session
    decision = decide_action(transcript, _session if _session.get("kind") else None)

    if not decision.get("continues_session"):
        _session = {}

    action_type, result = _dispatch_decision(decision, record, suppress_reply)

    if not suppress_reply:
        try:
            send_ack()
        except Exception as e:
            log.debug("Jarvis ack click failed (non-fatal): %s", e)

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


def _dispatch_decision(decision: dict, record: dict, suppress_reply: bool = False) -> tuple:
    """The actual action_type routing, factored out of process_command() so
    execute_decided_action() (a decision that already arrived pre-classified
    -- e.g. from an on-device live agent, instead of decide_action() here)
    can reuse the exact same dispatch/logging without duplicating it.
    Returns (action_type, result). Mutates the module-level _session, same
    as before this was factored out."""
    global _session
    action_type = decision.get("action_type")
    if suppress_reply and action_type == "qa":
        # Never answer a question late -- a stale answer to an old question
        # is worse than no answer. Still logged/visible (Obsidian/Notion),
        # just never spoken.
        result = {"ok": False, "spoken": None, "session": None}
    elif action_type == "open_app":
        result = _action_open_app(decision)
    elif action_type == "calendar_event":
        result = _action_calendar_event(decision)
    elif action_type == "reminder":
        result = _action_reminder(decision)
    elif action_type == "email_draft":
        result = _action_email_draft(decision, _session)
    elif action_type == "social_post":
        result = _action_social_post(decision, record or {})
    elif action_type == "qa":
        result = _action_qa(decision, _session)
    elif action_type == "code_task":
        result = _action_code_task(decision)
    elif action_type == "save_snippet":
        result = _action_save_snippet(decision)
    else:
        result = {"ok": False, "spoken": "I didn't understand that command."}
    result.setdefault("session", None)

    _session = result.get("session") or {}

    _log_to_obsidian(action_type, decision, result)
    return action_type, result


def execute_decided_action(decision: dict) -> dict:
    """Executes an action that was already classified elsewhere -- e.g. an
    on-device live agent's function-calling decided "calendar_event,
    title=X, date=Y" in real time and forwarded just the structured fields
    here, instead of a raw transcript for decide_action() to classify.
    Skips decide_action() entirely (classification already happened) and
    skips this app's own spoken reply (the device/live-agent side owns
    that for a live forwarded action) -- only executes the action and
    reports back ok/spoken so the caller (the HTTP route) can decide what,
    if anything, to relay to the device.

    No `record` is available here (this isn't a stored recording -- there
    was no full-command WAV synced through the normal pipeline), so
    social_post's use of `record["content_hash"]` isn't reachable via this
    path; decide_action's own prompt already scopes social_post to the
    normal recorded-command flow, but forwarding a social_post decision
    here would fail -- documented, not silently swallowed."""
    action_type, result = _dispatch_decision(decision, record=None, suppress_reply=False)

    if settings.get_all().get("notion_jarvis_database_id"):
        try:
            import notion_sync
            fake_record = {"name": "live-command", "created_at": datetime.now(timezone.utc).isoformat()}
            jarvis_result = {"action_type": action_type, "transcript": decision.get("query") or "",
                              "ok": result.get("ok", False), "spoken": result.get("spoken")}
            notion_sync.push_command(fake_record, jarvis_result)
        except Exception as e:
            log.warning("Notion push for live-forwarded Jarvis command failed (non-fatal): %s", e)

    try:
        import analytics
        analytics.track_event("jarvis_commands")
    except Exception:
        pass

    return {"action_type": action_type, "ok": result.get("ok", False), "spoken": result.get("spoken")}
