"""Executes a single action item on the user's behalf, once they've
explicitly assigned it to the agent from the dashboard.

Two responsibilities, deliberately kept separate:

  gather_context()  -- assemble everything this app already knows that
                       bears on the item. This is what makes the output
                       worth anything; an agent with a good prompt and no
                       evidence just writes plausible fiction.
  run_item()        -- hand that context to the LLM and store the result.

What this module does NOT do is commit anything. It stops at a stored
artifact in "awaiting_approval"; sending the email / filing the document /
booking the event all happen in poller.approve_agent_result, only after the
user approves. That split is the whole safety model: everything up to here
is reversible and private, and the irreversible step needs a human.

Every context source is individually optional and individually guarded --
Mail.app may be un-permissioned, the RAG index may be disabled or still
building, Notion may be unconfigured. A missing source degrades the answer
(and shows up in the result's "gaps"); it must never fail the run, because
a half-context answer the user can review still beats an error card.
"""
import logging
import re

import settings
import storage
from providers import get_completer
from providers.base import build_agent_execution_prompt, parse_agent_result

log = logging.getLogger("agent_runner")

# Caps on each evidence source. These exist because the whole payload has
# to fit one LLM context window alongside the instructions, and because
# relevance falls off fast -- the 12th semantic hit is rarely what makes an
# answer correct, but it does crowd out the transcript that is.
MAX_RAG_HITS = 8
MAX_RAG_CHARS = 700
MAX_TRANSCRIPT_CHARS = 6000
MAX_EMAIL_HITS = 5
# Per-email body budget. Smaller than apple_mail.MAX_BODY_CHARS because
# several emails share the payload with the transcript and RAG hits, and
# the decisive content of a reply is almost always near the top.
MAX_EMAIL_BODY_CHARS = 600


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + " …[truncated]"


def _people_in_item(item: dict, record: dict) -> list:
    """Who this item is about -- the owner plus any confirmed speaker whose
    name appears in the item text. Used to pull prior correspondence: an
    email thread with the person an action item concerns is usually the
    single most relevant thing outside the conversation itself."""
    names = []
    owner = (item.get("owner") or "").strip()
    if owner:
        names.append(owner)
    text = (item.get("text") or "").lower()
    for name in (record.get("speaker_names") or {}).values():
        name = (name or "").strip()
        if not name or name in names:
            continue
        # Whole-word match on the first name, so "Ben" doesn't match
        # "benchmark" and a mentioned-but-unrelated speaker isn't dragged in.
        first = name.split()[0].lower()
        if len(first) >= 3 and re.search(rf"\b{re.escape(first)}\b", text):
            names.append(name)
    owner_name = (settings.get_all().get("owner_name") or "").strip().lower()
    return [n for n in names if n.strip().lower() != owner_name]


def _source_transcript(record: dict) -> str:
    parts = [f"[The conversation this action item came from — {record.get('created_at', '')}]"]
    summary = (record.get("summary") or {}).get("summary")
    if summary:
        parts.append(f"Summary: {summary}")
    transcript = record.get("transcript")
    if transcript:
        parts.append("Transcript:\n" + _clip(transcript, MAX_TRANSCRIPT_CHARS))
    return "\n".join(parts)


def _related_material(item: dict, record: dict) -> str:
    """Semantically-related past recordings, Obsidian notes and Notion
    pages, via the existing embedding index. Excludes chunks from THIS
    recording -- they're already included verbatim above, and letting them
    win the similarity ranking would crowd out genuinely new material."""
    try:
        import rag_index
        if not rag_index.is_enabled():
            return ""
        query = (item.get("text") or "").strip()
        if not query:
            return ""
        hits = rag_index.search(query, top_k=MAX_RAG_HITS + 4)
    except Exception as e:
        log.warning("agent: related-material lookup failed: %s", e)
        return ""

    own_id = record.get("content_hash")
    lines = []
    for h in hits:
        if own_id and own_id in str(h.get("source", "")):
            continue
        label = " · ".join(x for x in (h.get("date"), h.get("speaker"), h.get("source")) if x)
        lines.append(f"- [{label}] {_clip(h.get('text', ''), MAX_RAG_CHARS)}")
        if len(lines) >= MAX_RAG_HITS:
            break
    if not lines:
        return ""
    return "[Related material from past recordings and notes:]\n" + "\n".join(lines)


def _known_facts() -> str:
    try:
        import memory_store
        return memory_store.facts_context()
    except Exception as e:
        log.warning("agent: memory facts unavailable: %s", e)
        return ""


def _prior_correspondence(names: list) -> str:
    """Recent email with the people this item concerns. macOS-only and
    strictly best-effort: Automation permission may not be granted, and a
    full-store Mail.app sweep can take seconds per name."""
    import sys
    if sys.platform != "darwin" or not names:
        return ""
    try:
        import apple_mail
    except ImportError:
        return ""

    lines = []
    for name in names[:2]:  # two people is plenty; each costs a full sweep
        try:
            # with_body=True: subjects alone tell the agent that a thread
            # exists but not what was agreed in it, which is usually the
            # whole reason the action item exists ("Ben said he'd send the
            # spec" needs the reply that did or didn't contain it).
            results = apple_mail.search_messages(name, max_results=MAX_EMAIL_HITS,
                                                 fuzzy=True, with_body=True)
        except Exception as e:
            log.warning("agent: mail lookup failed for %r: %s", name, e)
            continue
        for m in results:
            subj = (m.get("subject") or "(no subject)").strip()
            frm = (m.get("from") or "").strip()
            body = _clip(m.get("body") or "", MAX_EMAIL_BODY_CHARS)
            lines.append(f"- {subj} — from {frm}" + (f"\n    {body}" if body else ""))
    if not lines:
        return ""
    return "[Recent email involving the people in this item:]\n" + "\n".join(lines)


def gather_context(item: dict, record: dict) -> tuple:
    """Assembles the evidence payload. Returns (context_text, sources_used)
    -- sources_used is surfaced on the result card so the user can see what
    the agent actually had to work with, which is the difference between
    "the agent is wrong" and "the agent was starved"."""
    people = _people_in_item(item, record)
    blocks = [
        ("conversation", _source_transcript(record)),
        ("memory", _known_facts()),
        ("related recordings & notes", _related_material(item, record)),
        ("email history", _prior_correspondence(people)),
    ]
    used = [name for name, text in blocks if text.strip()]
    context = "\n\n".join(text for _, text in blocks if text.strip())
    return context, used


def run_item(content_hash: str, item_index: int) -> dict:
    """Runs one queued action item end-to-end and stores the artifact.

    Re-reads the record rather than trusting anything passed in: the poller
    queues work on one pass and executes on a later one, and the summary
    can be edited or re-generated in between. Returns the stored result, or
    a dict with "error" -- the item is left in "failed" with the message
    visible on the card, so it can be retried rather than vanishing."""
    record = storage.get_recording(content_hash)
    if not record:
        return {"error": "recording not found"}
    items = (record.get("summary") or {}).get("action_items") or []
    if item_index < 0 or item_index >= len(items):
        storage.set_action_item_agent_status(content_hash, item_index, "failed",
                                             error="action item no longer exists")
        return {"error": "action item no longer exists"}

    item = items[item_index]
    agent = item.get("agent") or {}
    action = agent.get("action")
    from providers.base import AGENT_ACTIONS
    if action not in AGENT_ACTIONS:
        storage.set_action_item_agent_status(content_hash, item_index, "failed",
                                             error=f"no runnable action type ({action!r})")
        return {"error": "no runnable action type"}

    storage.set_action_item_agent_status(content_hash, item_index, "running")
    try:
        context, sources = gather_context(item, record)
        if not context.strip():
            # Nothing to reason from. Fail loudly instead of letting the
            # model produce a confident answer out of thin air.
            storage.set_action_item_agent_status(
                content_hash, item_index, "failed",
                error="no context available to work from (is the search index built?)")
            return {"error": "no context available"}

        owner_name = (settings.get_all().get("owner_name") or "").strip()
        prompt = build_agent_execution_prompt(item, action, context, owner_name)
        _, complete = get_completer()
        raw = complete(prompt)
        result = parse_agent_result(raw)
        result["action"] = action
        result["sources_used"] = sources
        storage.set_action_item_agent_result(content_hash, item_index, result)
        log.info("agent completed %s for %s item %d (confidence=%s, sources=%s)",
                 action, record.get("name"), item_index, result.get("confidence"),
                 ", ".join(sources) or "none")
        return result
    except Exception as e:
        log.error("agent run failed for %s item %d: %s", record.get("name"), item_index, e)
        storage.set_action_item_agent_status(content_hash, item_index, "failed", error=str(e)[:300])
        return {"error": str(e)}
