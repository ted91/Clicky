"""The card registry: every dashboard card, and the agent that fills it.

A "card" is one panel on a recording (Action items, Stakeholders,
Correspondence, ...). Every card exists for every conversation; only the
ones with something to show are displayed initially, and the user can add
any of the rest -- at which point THAT CARD'S AGENT RUNS to populate it.

This module is the single place that knowledge lives. Adding a new card in
future should be one entry in CARDS below plus a renderer branch, not edits
threaded through storage/poller/app/templates. Before this existed, the
card list was duplicated in storage.py (labels), poller.py (an if/elif of
which ones had agents) and app.js -- three places to keep in sync and three
places to forget.

Three things define a card:

    label        what the user sees, in the add menu and the panel heading.
    has_content  "is this card relevant to this recording" -- decides
                 whether it shows up on its own, before any user action.
    populate     the agent. None means display-only (Transcript is the raw
                 source; Speakers is diarization output that already ran).

Import discipline: everything heavy (storage, providers, poller) is
imported lazily INSIDE the functions, never at module scope. storage
validates card ids by importing this module, so a top-level `import
storage` here would be a cycle. The lazy-import-inside-a-function pattern
is used throughout poller.py already.


ADDING A NEW CARD
-----------------
Two edits, and only one of them is here:

1. THIS FILE -- add a CARDS entry:

       "risks": Card(
           label="Risks",
           has_content=lambda r: _summary_list(r, "risks"),
           populate=_extract("risks"),        # or None for display-only
           summary_field="risks",
       ),

   If it uses _extract, also add a CARD_EXTRACTION_SPECS entry in
   providers/base.py describing the field's JSON shape and what counts as
   one -- that is what the extraction prompt is built from.

2. THE RENDERER -- one entry in app.js's `cardInner` map plus the matching
   block in templates/index.html, because each card's markup is genuinely
   different (checkboxes, inline forms, a scrollable list) and a single
   generic renderer would be worse than the duplication.

Everything else picks it up for free:
  - storage.set_card_hidden validates against CARDS
  - the add menu, visibility and empty-state come from visible_cards /
    addable_cards / empty_cards below
  - poller.refresh_requested_cards_once dispatches via CARDS[id].populate
  - app.js reads the labels from the page's #card-registry JSON, which is
    generated from CARD_LABELS -- no second list to keep in sync
  - both PyInstaller specs already list this module in hiddenimports
"""
import logging

log = logging.getLogger("card_agents")


class Card:
    """One registry entry. Plain class rather than a dataclass to stay
    consistent with the rest of this codebase, which uses neither."""

    def __init__(self, label, has_content, populate=None, summary_field=None):
        self.label = label
        self.has_content = has_content
        self.populate = populate
        # Which summary key this card's extraction writes back to, if any --
        # lets the generic extractor below stay data-driven.
        self.summary_field = summary_field

    def __repr__(self):
        return f"<Card {self.label!r} agent={'yes' if self.populate else 'no'}>"


def _summary_list(record, *fields):
    """True if any of the named summary lists has anything in it. Several
    cards are backed by more than one field (Action items also carries
    calendar events and drafts)."""
    summary = record.get("summary") or {}
    for f in fields:
        if summary.get(f):
            return True
    return False


def _extract(card_id):
    """Builds the populate function for a summary-derived card.

    Runs one targeted LLM pass for that single field and merges the result
    into the stored summary. Merges rather than replaces: the user may have
    edited this recording, and a card refresh must not quietly discard
    that. Existing entries are kept and only genuinely new ones appended.
    """
    async def populate(record):
        import asyncio
        import storage
        from providers import get_completer
        from providers.base import build_card_extraction_prompt, parse_card_extraction

        content_hash = record["content_hash"]
        transcript = (record.get("transcript") or "").strip()
        if not transcript:
            log.info("cannot extract %s for %s: no transcript", card_id, record.get("name"))
            return

        summary = record.get("summary") or {}
        prompt = build_card_extraction_prompt(card_id, transcript, summary)
        _, complete = get_completer()
        raw = await asyncio.to_thread(complete, prompt)
        found = parse_card_extraction(card_id, raw)
        if not found:
            log.info("extraction for %s on %s found nothing", card_id, record.get("name"))
            return

        field = CARDS[card_id].summary_field
        added = storage.merge_summary_list(content_hash, field, found)
        log.info("extraction for %s on %s added %d item(s)", card_id, record.get("name"), added)

    return populate


async def _populate_correspondence(record):
    """Delegates to the sweep poller already owns -- it bypasses the 15-minute
    throttle for exactly this case (the user's click IS the request) and
    shares the fuzzy-name matching and relevance filtering with the periodic
    sweep, so the two can't drift apart."""
    import poller
    await poller._refresh_correspondence_for(record)


# The registry. Order here is the order cards render in.
CARDS = {
    "action_items": Card(
        label="Action items",
        # Calendar events and drafts render INSIDE this card (both are
        # derived from action items: a due-dated item becomes a calendar
        # entry, an email-type item becomes a draft), so any of the three
        # having content makes the card relevant.
        has_content=lambda r: _summary_list(r, "action_items", "calendar_events")
                              or bool((r.get("drafts") or {}).get("items")),
        populate=_extract("action_items"),
        summary_field="action_items",
    ),
    "follow_ups": Card(
        label="Follow-ups",
        has_content=lambda r: _summary_list(r, "follow_ups"),
        populate=_extract("follow_ups"),
        summary_field="follow_ups",
    ),
    "stakeholders": Card(
        label="Stakeholders",
        has_content=lambda r: _summary_list(r, "stakeholders"),
        populate=_extract("stakeholders"),
        summary_field="stakeholders",
    ),
    "organizations": Card(
        label="Organizations",
        has_content=lambda r: _summary_list(r, "organizations"),
        populate=_extract("organizations"),
        summary_field="organizations",
    ),
    "speakers": Card(
        label="Speakers",
        has_content=lambda r: bool(r.get("speaker_names") or r.get("segments")),
        populate=None,  # diarization already ran during processing
    ),
    "correspondence": Card(
        label="Correspondence",
        has_content=lambda r: bool(r.get("correspondence")),
        populate=_populate_correspondence,
    ),
    "transcript": Card(
        label="Transcript",
        has_content=lambda r: bool(r.get("transcript")),
        populate=None,  # the raw source; nothing to go and fetch
    ),
}

CARD_IDS = tuple(CARDS)
CARD_LABELS = {cid: c.label for cid, c in CARDS.items()}


def has_agent(card_id: str) -> bool:
    card = CARDS.get(card_id)
    return bool(card and card.populate)


def visible_cards(record: dict) -> list:
    """The cards that should render for this recording, in registry order.

    A card shows when it has content OR the user explicitly added it (an
    added card must appear even while empty -- otherwise clicking "add"
    looks like it did nothing, which is precisely the bug this design
    replaced). Removal always wins over both."""
    hidden = set(record.get("cards_hidden") or [])
    added = set(record.get("cards_added") or [])
    out = []
    for cid, card in CARDS.items():
        if cid in hidden:
            continue
        # _safe_has_content: a malformed record must not blank the page.
        if _safe_has_content(card, record) or cid in added:
            out.append(cid)
    return out


def empty_cards(record: dict) -> list:
    """Visible cards that have nothing to show yet -- i.e. ones the user
    explicitly added whose agent hasn't produced anything (or found
    nothing). They render as a placeholder panel so the add click has a
    visible result.

    Exists as its own list so the Jinja template can render these without
    restructuring its eight existing per-card blocks: each of those already
    gates on "does this field have content", and this fills the gap for
    everything visible that doesn't."""
    return [cid for cid in visible_cards(record)
            if not _safe_has_content(CARDS[cid], record)]


def _safe_has_content(card, record):
    try:
        return bool(card.has_content(record))
    except Exception:
        return False


def addable_cards(record: dict) -> list:
    """Everything not currently on screen -- whether the user removed it or
    it simply never had content. This is the difference from the previous
    design, which only ever offered back cards that had been explicitly
    removed, so a card that was empty from the start could never be added."""
    visible = set(visible_cards(record))
    return [cid for cid in CARDS if cid not in visible]
