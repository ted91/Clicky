"""Shared shape every provider module implements. Not enforced via ABC/
Protocol machinery on purpose — each provider file just needs module-level
transcribe()/summarize() functions matching these signatures; see
providers/__init__.py for how they're selected.

def transcribe(wav_bytes: bytes) -> dict:
    # {"text": str, "segments": [{"speaker_id": str, "text": str, "start": float, "end": float}] | None}
    # segments is None for providers that don't support diarization
    # (openai, local) — only Mistral's Voxtral currently does.
    ...
def summarize(transcript: str) -> dict:
    # {
    #   "summary": str,
    #   "action_items": [{"text": str, "owner": str | None, "due_date": str | None,
    #     "comm_type": "email" | None, "comm_recipient": str | None,
    #     "email_subject": str | None, "email_body": str | None}],
    #     -- email_subject/email_body are only ever populated when comm_type
    #     == "email": a professionally-composed, ready-to-send draft
    #     addressed to comm_recipient about this one action item only (see
    #     SUMMARY_JSON_INSTRUCTIONS) -- consumed by poller._build_email_drafts
    #     and notion_sync.push_tasks instead of them templating the text
    #     themselves.
    #   "calendar_events": [{"title": str, "date": str | None, "time": str | None}],
    #   "stakeholders": [{"name": str, "note": str | None}],
    #   "follow_ups": [{"text": str, "owner": str | None}],
    #   "speaker_names": {speaker_id: name} for any "Speaker X:" label confidently
    #     identifiable from the transcript itself (self-introduction, or another
    #     speaker addressing them by name) -- omits anything not confidently named
    #   "type": "journal" | "actionable" -- self-reflective monologue/diary entry
    #     vs. a note with real tasks/events/other people involved
    # }
    ...
"""

import datetime as _datetime

SUMMARY_JSON_INSTRUCTIONS = """\
You turn a raw voice-memo/meeting transcript into structured notes. If the
transcript has "Speaker X:" labels, use them to attribute action items,
stakeholders, and follow-ups to the right person where the transcript makes
that clear.
Return ONLY valid JSON (no markdown fences, no commentary) matching exactly:
{
  "summary": "a bird's-eye view of the whole transcript",
  "action_items": [{"text": "...", "owner": "name or null", "due_date": "YYYY-MM-DD or null", "comm_type": "email" or null, "comm_recipient": "name or null", "email_subject": "... or null", "email_body": "... or null"}],
  "calendar_events": [{"title": "...", "date": "YYYY-MM-DD or null", "time": "HH:MM or null"}],
  "stakeholders": [{"name": "...", "note": "their role or why they matter here, or null"}],
  "follow_ups": [{"text": "an open question or pending decision, not yet a concrete action item", "owner": "name or null"}],
  "speaker_names": {"<the exact speaker label from the transcript, e.g. speaker_1>": "their real name"},
  "type": "journal" or "actionable",
  "excluded_background_note": "one honest sentence, or null"
}
If a list has nothing to report, return an empty list for it — never omit a
key. Infer owner/due_date/date/time/stakeholders only when clearly stated or
strongly implied in the transcript — use null rather than guessing.
Any line prefixed "[background, likely a different conversation]" is a
different, more distant conversation the mic also picked up at a noisy
venue — never let it contribute to "summary", "action_items",
"stakeholders", or "follow_ups"; write as if those lines weren't there.
Separately, even without that prefix, a chunk can still be a spliced-in
unrelated exchange rather than part of the real conversation -- distinct
from a genuine multi-topic meeting (which stays fully in-scope, see
below): the tell is that it doesn't connect to what's said immediately
before or after it, as if a different, unrelated conversation was briefly
picked up. Exclude that the same way. If (and only if) you excluded
anything by either rule, set "excluded_background_note" to one short,
honest sentence saying so (e.g. "Some unrelated background conversation
was picked up and excluded from this summary."); otherwise null.
Size "summary" to how much is actually in the transcript, not to a fixed
sentence count — a short voice memo still gets 2-4 sentences, but a long or
multi-topic recording (a meeting, a webinar, a call covering several
distinct subjects) needs several sentences or short paragraphs that name
every distinct topic/decision/theme actually discussed, in the order raised.
Someone should be able to read only this summary and know everything of
substance that happened, without opening the full transcript. Never
compress a long, dense discussion down to one or two generic sentences —
that's a sign you skipped topics, not a sign of good summarizing.
Be equally thorough with "action_items" and "follow_ups": scan the whole
transcript for every concrete task, commitment, decision, or open question,
including ones only implied by a speaker agreeing to do something or
someone being assigned a next step — not just the ones stated as an
explicit imperative. A transcript with substantive discussion essentially
never has zero action items; if you're about to return an empty
"action_items" list for a long or multi-topic transcript, re-scan it once
more before concluding there truly isn't a single task, decision, or
commitment in it.
Resolve any relative date ("July 17", "next Friday", "in two weeks")
against the "Today's date is ..." line below, not your own sense of the
current date — when only a month/day is given with no year, use the
nearest upcoming occurrence of that date on or after today, never a past
or arbitrary year.
For an action item, set "comm_type" to "email" ONLY when the speaker
explicitly says they need to email/message/write to a specific person
(e.g. "I need to email Vijay about the budget", "send Priya the notes") --
not for a vague task that merely involves another person ("follow up with
Vijay" without saying how). When comm_type is "email", set "comm_recipient"
to that person's name exactly as said (e.g. "Vijay") so the app can look up
their address — never invent an email address yourself, only the name.
Leave both null for every other action item.
When comm_type is "email", also write "email_subject" and "email_body": a
professional, concise, ready-to-send email in the speaker's own voice,
addressed to comm_recipient (e.g. "Hi Vijay,"), covering ONLY this one
action item's content — never bundle other action items or other people's
business into it, even if the transcript has several "email so-and-so"
items. Sign off in the speaker's own name if confidently known from the
transcript (see "speaker_names" below), otherwise omit a signature line.
Leave both null for every action item that isn't comm_type "email".
For "speaker_names": only include a speaker if the transcript makes their
identity unambiguous — they introduce themselves, or another speaker
addresses/refers to them by name in a way that clearly points back to
them. The transcript may mix languages or switch mid-sentence (e.g.
Spanglish, Hinglish) — recognize self-identification by its *meaning* in
whatever language or code-switched form it appears (e.g. English "I'm
Jeremy"/"This is Sanjit", Spanish "Soy Sanchit", Hindi "Main Sanchit hoon"),
not just the specific English phrasing. Do not guess from context alone,
and never invent a name. Return {} if no speaker can be confidently named.
For "type": use "journal" for a self-reflective monologue or diary-style
entry — the speaker thinking out loud, reflecting on their day, feelings,
or ideas, with no other person meaningfully participating and nothing
actionable. Use "actionable" for anything with real tasks, deadlines,
calendar events, or other people substantively involved (a meeting,
a call, planning with someone). Mentioning another person in passing
("grabbed coffee with Jeremy") without any task/event attached still
counts as "journal".

Transcript:
"""


def build_summary_prompt(transcript: str, deepgram_insights: dict = None, meeting: dict = None) -> str:
    """Builds the LLM prompt. Two optional context sources get inserted
    before the transcript, each as its own labeled block:

    - deepgram_insights: Deepgram Audio Intelligence (entities/topics/
      intents/sentiment) -- lets the LLM use already-detected facts rather
      than re-deriving them from raw text.
    - meeting: calendar event metadata (title + attendee name/email list,
      see google_client.current_or_next_event) -- steers "speaker_names"
      guesses toward real attendee names instead of guessing from the
      transcript alone, and toward the meeting's actual title/agenda.

    Always also anchors the LLM to today's actual date (see the
    "Today's date is ..." block below) -- without this, a relative date
    like "July 17" has nothing to resolve against but the model's own
    training-time sense of "now", which silently produces the wrong year.
    """
    now = _datetime.datetime.now()
    blocks = [f"[Today's date is {now:%Y-%m-%d} ({now:%A}).]"]

    if deepgram_insights:
        lines = ["[Deepgram Audio Intelligence — use this as a reliable reference:]"]
        sentiment = deepgram_insights.get("sentiment")
        if sentiment:
            score = deepgram_insights.get("sentiment_score", "")
            lines.append(f"Overall sentiment: {sentiment}" + (f" (score {score})" if score else ""))
        topics = deepgram_insights.get("topics") or []
        if topics:
            lines.append(f"Detected topics: {', '.join(topics[:8])}")
        intents = deepgram_insights.get("intents") or []
        if intents:
            lines.append(f"Detected intents: {', '.join(intents[:8])}")
        entities = deepgram_insights.get("entities") or []
        if entities:
            persons = [e["value"] for e in entities if e.get("label") in ("PER", "PERSON")]
            orgs = [e["value"] for e in entities if e.get("label") in ("ORG", "ORGANIZATION")]
            dates = [e["value"] for e in entities if e.get("label") in ("DATE",)]
            locs = [e["value"] for e in entities if e.get("label") in ("LOC", "LOCATION", "GPE")]
            if persons:
                lines.append(f"People mentioned: {', '.join(persons[:10])}")
            if orgs:
                lines.append(f"Organizations: {', '.join(orgs[:6])}")
            if dates:
                lines.append(f"Dates/times mentioned: {', '.join(dates[:6])}")
            if locs:
                lines.append(f"Locations: {', '.join(locs[:6])}")
        if len(lines) > 1:
            blocks.append("\n".join(lines))

    if meeting:
        lines = ["[Calendar context — this recording is a meeting:]"]
        if meeting.get("title"):
            lines.append(f"Meeting title: {meeting['title']}")
        attendees = meeting.get("attendees") or []
        if attendees:
            names = ", ".join(f"{a.get('name', '')} <{a.get('email', '')}>" for a in attendees if a.get("name"))
            lines.append(f"Calendar attendees: {names}")
            lines.append("When guessing \"speaker_names\", prefer matching a speaker's self-introduction or "
                         "how others address them against this attendee list over guessing a name from context alone.")
        if len(lines) > 1:
            blocks.append("\n".join(lines))

    # SUMMARY_JSON_INSTRUCTIONS ends with "Transcript:\n" -- strip that
    # trailing header so the context blocks can be inserted before the
    # transcript section, then re-add it.
    instructions = SUMMARY_JSON_INSTRUCTIONS.rstrip()
    if instructions.endswith("Transcript:"):
        instructions = instructions[:-len("Transcript:")].rstrip()
    return instructions + "\n\n" + "\n\n".join(blocks) + "\n\nTranscript:\n" + transcript


PERSON_KNOWLEDGE_INSTRUCTIONS = """\
You maintain a short running profile of a person, built up across many
voice memos and meetings over time. Merge the new information below into
the existing knowledge:
- PRESERVE every still-valid fact from the existing knowledge -- never
  drop something just because the new memo doesn't mention it.
- Integrate the new facts. If a new fact directly contradicts an old one,
  keep the newer fact.
- Output 2-6 plain sentences. No headers, no bullets, no JSON, no
  preamble like "Here is the updated profile" -- just the profile text
  itself, ready to be shown on the person's page.
"""


SOCIAL_POST_JSON_INSTRUCTIONS = """\
You are structuring a raw spoken transcript (a journal/self-talk entry or a
conversation) into a written post for the speaker to publish themselves.
This is a STRUCTURING pass, not a rewrite: keep the same essence, facts,
opinions, and the speaker's own voice/vocabulary/phrasing exactly as heard
in the transcript. Raw speech is often repetitive, circular, or out of
order -- fix ordering, cut filler words and false starts, add paragraph
breaks -- but never introduce a more "polished" or generic tone than the
speaker actually used, never invent details, and never change what they
actually said. If the speaker is casual and rambling, the post should read
as a coherent version of that same casual voice, not a corporate rewrite.

long_form_body is for Substack/Medium -- treat it as INFORMATIVE, SHARED
WRITING (a personal essay, a note, a reflection someone reads because the
content itself is worth reading), never as MARKETING COPY. Concretely,
this means:
  - Never write about the act of posting itself -- no "I wonder if I
    should share this on social media", no "thinking about putting this
    out there", no meta-commentary about publishing or audience reach.
    The speaker was talking about their actual subject, not about whether
    to post; write about the subject.
  - Never end with an engagement-bait question aimed at the reader
    ("What do you think -- would you try this?", "Let me know your
    thoughts!"). If the transcript itself raises a genuine open question
    the speaker was wrestling with, that's fine to include -- the
    difference is a real unresolved thought vs. a fished-for-comments
    hook.
  - No marketing language -- "game-changer", "exciting news", "I'd love
    to see where this goes", generic hype adjectives. Write plainly, the
    way a person explains something they know or noticed.
Give the post real shape even so -- "structuring" means more than tidying
punctuation. Every long_form_body should read as three loose parts, in the
speaker's own words and tone:
  1. An opening line that states the actual subject plainly -- not a
     generic label like "Update:" or "Thoughts:", and not a hook designed
     to bait clicks, just a clear, natural first sentence (often the
     speaker's own first substantive line, lightly sharpened).
  2. A body developing the thought -- if the speaker only said one or two
     sentences worth of substance, it's fine for this to stay brief, but
     lightly draw out what's *implied* in their own words (e.g. why
     they're thinking about it, what prompted it) rather than just
     echoing the sentence back unchanged. Never invent a fact, detail, or
     reason the speaker didn't actually say or clearly imply.
  3. A closing line that lands the thought -- a genuine reflection or a
     forward-looking note grounded in what was actually said, not a
     question fishing for reader engagement.
This applies to a two-sentence recording the same as a long one; a post
that's just the transcript sentence with a period moved is under-
structured even if technically accurate.

linkedin_teaser is different -- it's a short pointer driving traffic to the
long-form post, so a direct, punchy framing of what the post is about is
fine there (still no invented hype, just economy of words).

Return ONLY a JSON object with these exact keys:
{
  "long_form_title": "a short, natural title in the speaker's own phrasing -- not clickbait",
  "long_form_body": "the full structured post (opening/body/closing per above), informative not marketing, markdown paragraphs, no title heading (title is separate)",
  "linkedin_teaser": "2-4 sentences max, same voice, ending with the placeholder token {{LONG_FORM_URL}} on its own -- do not invent a URL",
  "claims_to_verify": ["list of specific factual assertions the speaker made that are checkable -- names, dates, statistics, historical claims -- empty list if none"]
}

Do not include commentary outside the JSON. If the transcript is truly
empty of any real reflection or narrative (pure logistics, e.g. just "call
me back at 3pm"), set long_form_body to an empty string and
claims_to_verify to [] -- don't manufacture content where none exists.

Transcript:
"""


def build_social_post_prompt(transcript: str, summary: dict = None, meeting: dict = None) -> str:
    """Follow-up LLM call (separate from build_summary_prompt) that turns a
    journal/narrative transcript into a structured long-form post plus a
    short LinkedIn teaser -- see SOCIAL_POST_JSON_INSTRUCTIONS for the full
    "structure, don't rewrite" framing. Only worth calling for journal-type
    or otherwise narrative recordings (a pure task-list meeting summary has
    nothing to turn into a story) -- the caller decides that, this just
    builds the prompt.

    Deliberately generates from the raw transcript alone, not the
    already-condensed `summary` -- a summary is itself a lossy paraphrase
    (built for a Notes page, not for republishing), so structuring a post
    from it compounds two rounds of paraphrasing away from what the
    speaker actually said. `summary` is accepted but unused (kept so
    existing callers don't need updating); only `meeting` (for date/title
    context) still shapes the prompt.
    """
    now = _datetime.datetime.now()
    blocks = [f"[Today's date is {now:%Y-%m-%d} ({now:%A}).]"]
    if meeting and meeting.get("title"):
        blocks.append(f"[This was recorded during: {meeting['title']}]")

    instructions = SOCIAL_POST_JSON_INSTRUCTIONS.rstrip()
    if instructions.endswith("Transcript:"):
        instructions = instructions[:-len("Transcript:")].rstrip()
    return instructions + "\n\n" + "\n\n".join(blocks) + "\n\nTranscript:\n" + transcript


JOURNAL_WRITEUP_JSON_INSTRUCTIONS = """\
You are turning a raw spoken journal/self-reflection transcript into a rich
journal-entry write-up -- NOT a meeting-notes summary. A journal entry has
no action items owed to other people, no stakeholders, no calendar
logistics -- it's one person's own thinking, and the write-up should
reflect that shape: depth of reflection, not meeting bureaucracy.

Produce these parts, all grounded strictly in what the speaker actually
said (never invent a detail, reason, or conclusion they didn't state or
clearly imply):
  - "title": a short, natural title in the speaker's own phrasing.
  - "reflection": an in-depth synthesis of the entry, several sentences to
    a few short paragraphs depending on how much the speaker actually said
    -- more than a one-line summary. Capture not just WHAT they said but
    the throughline of their thinking: what they're working through, why
    it's on their mind, how their thoughts developed over the entry. Write
    it as a coherent reflection in the speaker's own voice/tone (casual
    stays casual), not a corporate-sounding recap.
  - "key_learnings": realizations, insights, or things the speaker
    concluded/figured out during the entry -- empty list if the entry was
    purely descriptive with no real realization in it. Don't manufacture
    a lesson that isn't there.
  - "action_items": things the speaker said THEY intend to do (personal
    follow-through, not owed to anyone else) -- empty list if none.
  - "notable_points": specific facts, ideas, names, or details worth being
    able to find again later (a book mentioned, a number, a decision) --
    empty list if nothing like that came up.

Return ONLY a JSON object with exactly these keys: "title", "reflection",
"key_learnings", "action_items", "notable_points" (the latter three are
always arrays of short strings, [] if empty -- never omit a key).

Transcript:
"""


def build_journal_writeup_prompt(transcript: str) -> str:
    """Dedicated follow-up call for journal-classified recordings (see
    poller._enforce_journal_rule / providers.base "type" field) -- builds a
    journal-specific structure (reflection/key_learnings/action_items/
    notable_points) instead of reusing the meeting-oriented Summary/Action
    items/Stakeholders/Calendar events page layout that push_recording's
    _build_blocks uses, which read as empty meeting boilerplate on a
    self-reflective entry (empty Stakeholders, empty Calendar events, etc).
    See notion_sync._build_journal_blocks, which renders this output."""
    return JOURNAL_WRITEUP_JSON_INSTRUCTIONS + transcript


def parse_journal_writeup_json(raw_text: str) -> dict:
    """Same fenced-JSON tolerance as parse_summary_json/parse_social_post_json.
    Falls back to putting the raw text in "reflection" on parse failure so a
    malformed response still degrades to something readable on the Journal
    page, rather than losing the entry's content entirely."""
    import json

    text = raw_text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()

    try:
        data = json.loads(text)
        return {
            "title": data.get("title", "") or "",
            "reflection": data.get("reflection", "") or "",
            "key_learnings": _denull(data.get("key_learnings", []) or []),
            "action_items": _denull(data.get("action_items", []) or []),
            "notable_points": _denull(data.get("notable_points", []) or []),
        }
    except (json.JSONDecodeError, AttributeError):
        return {"title": "", "reflection": raw_text.strip(), "key_learnings": [], "action_items": [], "notable_points": []}


def build_person_knowledge_prompt(person_name: str, existing_knowledge: str, new_context: str) -> str:
    """Builds the merge prompt for a person's rolling Knowledge paragraph
    (see notion_sync.push_people). existing_knowledge may be empty on the
    person's first mention; new_context is this recording's facts about
    them (stakeholder note, recording summary, related action items,
    recording date)."""
    parts = [PERSON_KNOWLEDGE_INSTRUCTIONS, f"Person: {person_name}"]
    parts.append("Existing knowledge:\n" + (existing_knowledge.strip() or "(none yet -- this is their first mention)"))
    parts.append("New information from the latest recording:\n" + new_context.strip())
    return "\n\n".join(parts)


IMPORTANCE_SENSITIVITY_GUIDANCE = {
    "low": "Only mark SKIP for obvious noise -- automated receipts, newsletters, "
           "marketing, \"you have a new follower\"-style app pings. Anything with "
           "real human content, even routine, is IMPORTANT.",
    "medium": "Mark SKIP for routine/automated content (newsletters, receipts, "
              "social media digests, FYI-only messages with no action needed). "
              "Mark IMPORTANT anything that looks like it needs a response, a "
              "decision, or is time-sensitive.",
    "high": "Mark IMPORTANT only if the recipient would genuinely want to be "
            "interrupted for this right now -- something urgent, time-critical, "
            "or from someone clearly important to them. Mark SKIP everything else, "
            "including most routine work/personal messages that could wait.",
}


def build_importance_prompt(title: str, body: str, sensitivity: str) -> str:
    """Builds a one-word-verdict prompt for notifications.py's AI-pager
    importance filter (Gmail/Mac notification sources only -- see
    notifications.py's _passes_importance_filter). Deliberately NOT a JSON
    prompt like SUMMARY_JSON_INSTRUCTIONS -- a single word is cheaper and
    faster to parse for something called once per notification, and the
    caller's own parsing is a plain string comparison, no JSON decode
    needed."""
    guidance = IMPORTANCE_SENSITIVITY_GUIDANCE.get(sensitivity, IMPORTANCE_SENSITIVITY_GUIDANCE["medium"])
    return (
        "You triage a single notification for a physical e-paper pager device -- "
        "the recipient gets physically interrupted (a click + a screen update) for "
        "anything marked IMPORTANT, so only genuinely worthwhile notifications "
        "should pass.\n\n"
        f"Sensitivity level: {sensitivity}. {guidance}\n\n"
        f"Notification title/sender: {title}\n"
        f"Notification body: {body}\n\n"
        "Respond with EXACTLY one word, nothing else: IMPORTANT or SKIP."
    )


def build_recording_type_prompt(transcript: str) -> str:
    """Dedicated, single-purpose classification call -- run BEFORE the main
    summarize() call (see poller.process_once), not left as one field
    competing for attention inside SUMMARY_JSON_INSTRUCTIONS' much larger
    structured-output prompt. Same "one word, cheaper and more reliable
    than a buried field" reasoning as build_importance_prompt above.

    This becomes a THIRD independent signal feeding
    poller._enforce_journal_rule, alongside the existing diarized-speaker-
    count check and the speaker_names-count backstop -- not a replacement
    for either. All three are OR'd together (any one forces "actionable")
    because the known failure mode is real conversations being missed
    (classified "journal" when they shouldn't be), not the reverse, so
    more independent ways to catch that is the right shape."""
    return (
        "Classify this transcript as JOURNAL or CONVERSATION, judging from "
        "the content and phrasing itself -- not from how many distinct "
        "voices were technically detected in the audio (that detection can "
        "fail to separate two real speakers, so your judgment must hold up "
        "even if the transcript below reads as coming from a single voice).\n\n"
        "JOURNAL: one person's self-directed reflection or diary entry -- "
        "\"I\" statements about their own day, thoughts, or plans, with no "
        "genuine second party actually participating.\n\n"
        "CONVERSATION: any real back-and-forth -- direct address (\"you\", "
        "someone's name used to address them), a question that gets "
        "answered by someone else, differing viewpoints being exchanged, "
        "or turn-taking dialogue of any kind. If in doubt because the "
        "transcript shows any sign of a second party, prefer CONVERSATION.\n\n"
        "Transcript:\n\"\"\"\n" + transcript + "\n\"\"\"\n\n"
        "Respond with EXACTLY one word, nothing else: JOURNAL or CONVERSATION."
    )


def merge_consecutive_segments(segments):
    """Combines runs of consecutive segments from the same speaker into one
    segment, concatenating their text. Diarization naturally splits even a
    single uninterrupted turn into many short segments (roughly
    sentence-by-sentence, sometimes shorter) -- left unmerged, both the
    transcript display and the LLM prompt see the same speaker's turn as a
    dozen choppy one-liners instead of one coherent block, which reads as
    "chunky" and gives the LLM more fragmented context to reason over than
    the conversation actually had. Keeps the earliest start / latest end
    timestamp across the merged run. Returns [] for empty/None input,
    otherwise a new list -- never mutates the input segments (storage.json
    keeps the raw per-segment data, e.g. for speaker rename UI)."""
    if not segments:
        return []
    merged = []
    for seg in segments:
        speaker_id = seg.get("speaker_id")
        text = (seg.get("text") or "").strip()
        if merged and merged[-1]["speaker_id"] == speaker_id:
            if text:
                merged[-1]["text"] = (merged[-1]["text"] + " " + text).strip()
            merged[-1]["end"] = seg.get("end", merged[-1]["end"])
            # A run from the same speaker is almost always at the same
            # distance from the mic -- if any constituent segment was
            # classified "background" (see audio_analysis.py), treat the
            # whole merged block that way rather than silently dropping
            # the classification on merge.
            if seg.get("loudness_class") == "background":
                merged[-1]["loudness_class"] = "background"
        else:
            merged.append({
                "speaker_id": speaker_id,
                "text": text,
                "start": seg.get("start"),
                "end": seg.get("end"),
                "loudness_class": seg.get("loudness_class"),
            })
    return merged


def format_transcript_with_speakers(text: str, segments, speaker_names: dict = None):
    """Turns diarized segments into a "Speaker X: ..." transcript for the
    summarization prompt — falls back to the plain text when a provider
    didn't return segments (no diarization support). Consecutive segments
    from the same speaker are merged first (see merge_consecutive_segments)
    so the LLM sees each turn as one coherent block, not fragmented
    one-liners.

    speaker_names, when given, resolves each segment's raw speaker_id to
    its confirmed display name instead of the generic "Speaker X" label.
    Used to re-summarize after a rename (dashboard or Notion) — passing
    the resolved name means the LLM's own prose ("Sanchit Gupta mentioned
    ...") picks up the correction too, not just the transcript block,
    since the summary/stakeholders text it writes is otherwise frozen at
    whatever label existed at the original processing time.

    A segment classified "background" by audio_analysis.annotate_segment_loudness
    (a quieter, more distant conversation picked up alongside the real
    one — see that module's docstring for what this can and can't detect)
    gets a `[background, likely a different conversation]` prefix instead
    of being dropped — SUMMARY_JSON_INSTRUCTIONS tells the model to ignore
    prefixed lines when writing the summary, but the full transcript still
    shows everything the mic actually heard."""
    if not segments:
        return text
    speaker_names = speaker_names or {}
    lines = []
    for seg in merge_consecutive_segments(segments):
        speaker_id = seg.get("speaker_id") or "Unknown"
        speaker = speaker_names.get(speaker_id) or f"Speaker {speaker_id}"
        seg_text = (seg.get("text") or "").strip()
        if not seg_text:
            continue
        prefix = "[background, likely a different conversation] " if seg.get("loudness_class") == "background" else ""
        lines.append(f"{prefix}{speaker}: {seg_text}")
    return "\n".join(lines) if lines else text


def _denull(value):
    """LLMs sometimes emit the literal string "null" (or "none"/"n/a")
    instead of JSON null for an unfilled optional field, despite the
    prompt saying to use null -- caught in the wild via a "speaker_names"
    guess that came back as {"speaker_1": "null"} instead of omitting the
    key. Recursively normalizes those to real None/absent, since every
    caller downstream (notion_sync.py, poller.py) treats a non-empty
    string as real data with `if value:` checks and can't tell a
    hallucinated "null" from an actual name."""
    if isinstance(value, str):
        return None if value.strip().lower() in ("null", "none", "n/a", "") else value
    if isinstance(value, dict):
        cleaned = {k: _denull(v) for k, v in value.items()}
        return {k: v for k, v in cleaned.items() if v is not None}
    if isinstance(value, list):
        return [_denull(v) for v in value]
    return value


def parse_summary_json(raw_text: str) -> dict:
    """LLMs sometimes wrap JSON in ```json fences despite instructions not
    to; strip those before parsing. Falls back to a summary-only shape if
    parsing still fails, so a single malformed response doesn't crash the
    poller — the raw text is preserved as the summary for visibility.
    """
    import json

    text = raw_text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()

    try:
        data = json.loads(text)
        record_type = _denull(data.get("type"))
        return {
            "summary": data.get("summary", "") or "",
            "action_items": _denull(data.get("action_items", []) or []),
            "calendar_events": _denull(data.get("calendar_events", []) or []),
            "stakeholders": _denull(data.get("stakeholders", []) or []),
            "follow_ups": _denull(data.get("follow_ups", []) or []),
            "speaker_names": _denull(data.get("speaker_names", {}) or {}),
            "type": record_type if record_type in ("journal", "actionable") else "actionable",
            "excluded_background_note": _denull(data.get("excluded_background_note")),
        }
    except (json.JSONDecodeError, AttributeError):
        return {"summary": raw_text.strip(), "action_items": [], "calendar_events": [],
                "stakeholders": [], "follow_ups": [], "speaker_names": {}, "type": "actionable",
                "excluded_background_note": None}


def parse_social_post_json(raw_text: str) -> dict:
    """Same fenced-JSON tolerance as parse_summary_json. Falls back to an
    empty long_form_body (not raw_text) on parse failure -- unlike the
    summary parser, there's no safe way to show malformed output to the
    user as a "post" without risking it getting approved/published as-is,
    so a parse failure here means "nothing generated," not "show the mess."
    """
    import json

    text = raw_text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()

    try:
        data = json.loads(text)
        return {
            "long_form_title": data.get("long_form_title", "") or "",
            "long_form_body": data.get("long_form_body", "") or "",
            "linkedin_teaser": data.get("linkedin_teaser", "") or "",
            "claims_to_verify": _denull(data.get("claims_to_verify", []) or []),
        }
    except (json.JSONDecodeError, AttributeError):
        return {"long_form_title": "", "long_form_body": "", "linkedin_teaser": "", "claims_to_verify": []}
