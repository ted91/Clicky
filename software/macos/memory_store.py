"""Jarvis's "memory" -- a small, always-injected list of durable facts/
preferences, distinct from rag_index's on-demand semantic search. Kept
deliberately small (a plain JSON list, no embeddings) so the WHOLE list can
be injected into every decide_action prompt unconditionally, rather than
retrieved by similarity like rag_index's chunks -- the point of memory is
that it's always known, not found when relevant.

Explicit-only for now (per this session's plan): only written when the
user actually says "save this"/"remember that" (see jarvis.py's
_action_save_snippet) -- no automatic/implicit memory writing, since an LLM
guessing what's "worth remembering" from a normal conversation is much
easier to get wrong than an explicit, on-request write.
"""
import json
import logging
import os
import threading
from datetime import datetime, timezone

import paths

log = logging.getLogger("memory_store")

_PATH = os.path.join(paths.APP_DATA_DIR, "jarvis_memory.json")
_lock = threading.Lock()

# A judgment-call cap, not a measured value -- keeps the always-injected
# block small enough to never meaningfully compete with the rest of a
# prompt's context budget. Oldest facts drop off first once exceeded.
MAX_FACTS = 50


def _load() -> list:
    if not os.path.exists(_PATH):
        return []
    with open(_PATH, "r") as f:
        return json.load(f)


def _save(facts: list):
    tmp_path = _PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(facts, f, indent=2)
    os.replace(tmp_path, _PATH)


def add_fact(text: str):
    text = (text or "").strip()
    if not text:
        return
    with _lock:
        facts = _load()
        facts.append({"text": text, "created_at": datetime.now(timezone.utc).isoformat()})
        facts = facts[-MAX_FACTS:]
        _save(facts)


def list_facts() -> list:
    with _lock:
        return _load()


def delete_fact(index: int):
    with _lock:
        facts = _load()
        if 0 <= index < len(facts):
            facts.pop(index)
            _save(facts)


def facts_context() -> str:
    """Formatted for direct injection into a prompt -- empty string (not
    None) when there are no facts, so callers can always safely
    concatenate this in without a null check."""
    facts = list_facts()
    if not facts:
        return ""
    lines = [f"- {f['text']}" for f in facts]
    return "[Known facts/preferences about the user -- always true, not retrieved on demand:]\n" + "\n".join(lines)
