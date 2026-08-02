"""Backlog #5: continuous-conversation merging -- deciding whether a
just-finished recording is a continuation of the immediately-preceding one
(paused mid-thought, someone interrupted) rather than a fresh, unrelated
recording. See poller.merge_continuations_once for where this is called.

Two independent gates, both cheap-before-expensive ordered:
  1. Temporal + type gate (this module, no LLM call) -- gap window and
     summary["type"] agreement. A generous gap window is safe specifically
     BECAUSE the topic check below is the real gate, not the gap alone
     (explicit user direction: "even 10 mins will be ok" given the topic
     match backstop).
  2. Topical continuity (one LLM call) -- see providers.base.
     build_continuation_prompt's docstring for why this is a dedicated
     classifier rather than rag_index embedding similarity.
"""
from datetime import datetime, timezone

DEFAULT_MERGE_GAP_MINUTES = 10

# How much of each transcript's boundary to feed the continuity classifier
# -- the signal lives at the boundary, and this keeps the call cheap
# regardless of how long either recording actually is.
_BOUNDARY_WORDS = 150


def _tail(text: str, n: int = _BOUNDARY_WORDS) -> str:
    words = (text or "").split()
    return " ".join(words[-n:])


def _head(text: str, n: int = _BOUNDARY_WORDS) -> str:
    words = (text or "").split()
    return " ".join(words[:n])


def _parse_created_at(record: dict) -> datetime:
    return datetime.fromisoformat(record["created_at"])


def find_merge_candidate(new_record: dict, all_records: list, gap_minutes: int = DEFAULT_MERGE_GAP_MINUTES):
    """Finds the immediately-preceding recording `new_record` might be a
    continuation of, applying the cheap gap+type gates only (no LLM call
    here -- see the module docstring). Returns that candidate record, or
    None if nothing qualifies.

    "Immediately preceding" always resolves to a CANONICAL record (one
    with no merged_into of its own) -- a record already merged into
    something else is skipped so a chain of 3+ continuations always merges
    into the original chain head, never nests (B3 merges into A directly,
    not into B2)."""
    if new_record.get("kind") != "memo" or new_record.get("status") != "done":
        return None
    new_created = _parse_created_at(new_record)
    new_type = (new_record.get("summary") or {}).get("type")

    best = None
    best_created = None
    for r in all_records:
        if r["content_hash"] == new_record["content_hash"]:
            continue
        if r.get("kind") != "memo" or r.get("merged_into"):
            continue
        if r.get("status") != "done":
            # Candidate hasn't finished processing (transcript/summary
            # don't exist yet to compare or re-summarize) -- skip this
            # pass; the next poll cycle naturally retries once it's done.
            continue
        r_created = _parse_created_at(r)
        if r_created >= new_created:
            continue
        gap = (new_created - r_created).total_seconds()
        if gap > gap_minutes * 60:
            continue
        if (r.get("summary") or {}).get("type") != new_type:
            # A journal absorbing conversation content (or vice versa)
            # breaks the type-specific routing (_enforce_journal_rule,
            # journal-only Notion database push) -- block before ever
            # spending an LLM call on the continuity question.
            continue
        if best_created is None or r_created > best_created:
            best, best_created = r, r_created
    return best


def build_merge_check_prompt_args(preceding: dict, new_record: dict):
    """Returns (tail_of_preceding, head_of_new, gap_seconds) for
    providers.base.build_continuation_prompt -- split out so callers
    (poller.py, and this module's own tests) can build the prompt without
    duplicating the boundary-trimming logic."""
    gap_seconds = int((_parse_created_at(new_record) - _parse_created_at(preceding)).total_seconds())
    return _tail(preceding.get("transcript")), _head(new_record.get("transcript")), gap_seconds
