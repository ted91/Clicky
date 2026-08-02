"""Deepgram: Nova-3 for transcription + diarization + Audio Intelligence.

STT-only -- Deepgram has no chat/completion API, so this can only ever be
picked as STT_PROVIDER, never LLM_PROVIDER.

In addition to transcription and diarization we request four Audio
Intelligence features in the same API call (no extra cost or latency since
they run server-side alongside transcription):
  - summarize: Deepgram's own short summary of the whole recording
  - intents:   what the speaker is trying to accomplish in each segment
  - topics:    subject-matter tags for segments (e.g. "project planning")
  - detect_entities: named entities -- PERSON, ORG, DATE, LOCATION, etc.
(sentiment was dropped per explicit user request -- not useful enough to
keep taking up a database column/dashboard badge.)

These are returned as `deepgram_insights` in the transcribe() result dict and
stored in the recording record. poller.py passes them to the LLM summarization
prompt so the resulting summary/stakeholders/action-items are more accurate.

Uses Deepgram's REST API directly via `requests` (already a core dependency)
rather than their SDK, to avoid another install for a handful of endpoint calls.

FUTURE FEATURE (not yet built) -- voice-based speaker recognition:
Deepgram also offers voice fingerprinting/enrollment (train a short voice
sample against a known identity, then recognize that person automatically
in future recordings, independent of session-local diarization labels).
Planned approach when this gets built:
  - New known person (no enrolled voiceprint yet): don't auto-train. Wait
    for the user to name the speaker (dashboard or Notion "Speaker N"
    property, see poller.resync_after_rename) -- only THEN enroll their
    voice from that recording's audio, now that we have a confirmed
    name-to-voice mapping instead of guessing.
  - Meeting recordings (Phase B, google_client.py): once Google Calendar
    attendees are known and a segment is confidently attributed (e.g. self-
    introduction, or matched via meeting_recorder's stereo mic channel =
    the user), that's enough labeled audio to enroll a voiceprint without
    waiting on a manual rename -- richer signal than a solo device memo.
  - Once a person has an enrolled voiceprint, future recordings (device or
    meeting) can be pre-tagged with their name directly from voice
    matching, before/instead of relying on diarization + LLM guessing.
  - Also worth checking whether Google Meet's own live captions expose a
    per-utterance speaker-name signal in real time (separate from the
    Calendar API used today) -- if accessible, that would be a second,
    likely more accurate source of ground-truth speaker labels for meeting
    recordings specifically, complementary to voice enrollment.
This needs its own design pass (storage schema for voiceprints, enrollment
UX, privacy implications of storing biometric voice data) before building --
tracked here so the idea isn't lost, not started yet.
"""
import requests

import config

API_BASE = "https://api.deepgram.com/v1/listen"

# Minimum confidence to include a topic/intent/entity in insights.
# Lower = more recall but more noise.
_MIN_CONFIDENCE = 0.6


def _parse_insights(results: dict) -> dict:
    """Extracts Audio Intelligence features from a Deepgram response."""
    insights = {}

    # Deepgram's own summary of the whole recording -- distinct from the
    # LLM-generated summary this app writes separately (providers.base's
    # build_summary_prompt); kept alongside it as Deepgram's own take,
    # cheap since it's the same API call.
    summary_data = results.get("summary") or {}
    if summary_data.get("short"):
        insights["summary"] = summary_data["short"].strip()

    # Topics -- deduplicated, sorted by confidence
    topics_data = results.get("topics") or {}
    topic_set = {}
    for seg in topics_data.get("segments") or []:
        for t in seg.get("topics") or []:
            name = t.get("topic", "").strip()
            score = t.get("confidence_score", 0.0)
            if name and score >= _MIN_CONFIDENCE:
                topic_set[name] = max(topic_set.get(name, 0.0), score)
    if topic_set:
        insights["topics"] = sorted(topic_set, key=lambda k: -topic_set[k])

    # Intents -- deduplicated, sorted by confidence
    intents_data = results.get("intents") or {}
    intent_set = {}
    for seg in intents_data.get("segments") or []:
        for i in seg.get("intents") or []:
            name = i.get("intent", "").strip()
            score = i.get("confidence_score", 0.0)
            if name and score >= _MIN_CONFIDENCE:
                intent_set[name] = max(intent_set.get(name, 0.0), score)
    if intent_set:
        insights["intents"] = sorted(intent_set, key=lambda k: -intent_set[k])

    # Named entities from channels[0].alternatives[0].entities
    try:
        raw_entities = (
            results.get("channels", [{}])[0]
            .get("alternatives", [{}])[0]
            .get("entities", [])
        ) or []
    except (IndexError, AttributeError):
        raw_entities = []

    entity_list = []
    seen = set()
    for e in raw_entities:
        label = e.get("label", "")
        value = (e.get("value") or "").strip()
        confidence = e.get("confidence", 0.0)
        key = (label, value.lower())
        if value and confidence >= _MIN_CONFIDENCE and key not in seen:
            entity_list.append({"label": label, "value": value})
            seen.add(key)
    if entity_list:
        insights["entities"] = entity_list

    return insights


def transcribe(wav_bytes: bytes) -> dict:
    if not config.DEEPGRAM_API_KEY:
        raise RuntimeError("DEEPGRAM_API_KEY is not set in .env")
    resp = requests.post(
        API_BASE,
        headers={
            "Authorization": f"Token {config.DEEPGRAM_API_KEY}",
            "Content-Type": "audio/wav",
        },
        params={
            "model": config.DEEPGRAM_STT_MODEL,
            "diarize": "true",
            "utterances": "true",
            "punctuate": "true",
            "smart_format": "true",
            # Audio Intelligence features -- all run server-side alongside
            # transcription so there's no extra round-trip.
            "summarize": "true",
            "intents": "true",
            "topics": "true",
            "detect_entities": "true",
        },
        data=wav_bytes,
        timeout=120,
    )
    if not resp.ok:
        raise RuntimeError(f"Deepgram API error {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    results = data.get("results", {})

    utterances = results.get("utterances") or []
    segments = None
    if utterances:
        # Deepgram's speaker id is a bare int (0, 1, ...); prefixed to match
        # the "speaker_N" shape the rest of the pipeline expects from every
        # other diarizing provider (see providers/base.py's speaker_slot_index,
        # notion_sync's Speaker-N property mapping -- both parse a trailing
        # number off this exact string).
        segments = [
            {
                "speaker_id": f"speaker_{u['speaker'] + 1}",
                "text": u.get("transcript", ""),
                "start": u.get("start", 0.0),
                "end": u.get("end", 0.0),
            }
            for u in utterances
        ]

    text = " ".join(u.get("transcript", "") for u in utterances) if utterances else (
        results.get("channels", [{}])[0].get("alternatives", [{}])[0].get("transcript", "")
    )

    insights = _parse_insights(results)

    return {"text": text, "segments": segments, "deepgram_insights": insights or None}


def summarize(transcript: str, deepgram_insights: dict = None, meeting: dict = None) -> dict:
    raise NotImplementedError(
        "Deepgram has no chat/completion API — set LLM_PROVIDER to mistral, "
        "openai, anthropic, or local instead."
    )


def complete(prompt: str) -> str:
    raise NotImplementedError(
        "Deepgram has no chat/completion API — set LLM_PROVIDER to mistral, "
        "openai, anthropic, or local instead."
    )
