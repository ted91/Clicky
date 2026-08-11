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
import array

import requests

import config


def _has_distinct_channels(wav_bytes: bytes, probe_frames: int = 4000) -> bool:
    """Whether this WAV carries genuinely different audio per channel.

    True only for meetingcap's one-participant-per-channel capture (system
    audio L / mic R), which is what makes `multichannel` the right call.
    False for the ESP32's single mic recorded into both channels, where
    multichannel would just transcribe the same audio twice and invent a
    second speaker who doesn't exist.

    Mirrors noise_reduction._channels_differ deliberately rather than
    importing it: this module is a provider and shouldn't depend on the
    app's audio pipeline, and both need to survive the other being
    changed. Any failure answers False -- the previous single-channel
    behaviour -- since guessing wrong toward multichannel would corrupt
    attribution rather than merely fail to improve it."""
    try:
        if len(wav_bytes) < 44 or wav_bytes[0:4] != b"RIFF" or wav_bytes[8:12] != b"WAVE":
            return False
        channels = int.from_bytes(wav_bytes[22:24], "little")
        bits = int.from_bytes(wav_bytes[34:36], "little")
        if channels < 2 or bits != 16:
            return False
        data = wav_bytes[44:]
        data = data[:len(data) - (len(data) % 2)]
        samples = array.array("h")
        samples.frombytes(data)
        total_frames = len(samples) // channels
        if total_frames == 0:
            return False
        step = max(1, total_frames // probe_frames)
        for f in range(0, total_frames, step):
            base = f * channels
            first = samples[base]
            for c in range(1, channels):
                if samples[base + c] != first:
                    return True
        return False
    except Exception:
        return False

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
            # multichannel is set only when the audio genuinely carries one
            # participant per channel (meetingcap puts system audio on L
            # and the mic on R). Then each channel IS a speaker: attribution
            # becomes a fact about which track the words came from rather
            # than an acoustic guess, which is strictly better than
            # diarizing a mix -- and diarization on a two-person mono mix
            # was measurably getting turns wrong (a one-word answer landing
            # on whoever asked the question).
            **({"multichannel": "true"} if _has_distinct_channels(wav_bytes) else {}),
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
    # Sorted here, once, rather than on `segments` alone: with multichannel
    # Deepgram groups utterances per channel, and the flat `text` below is
    # built from this same list -- sorting only the segments would leave
    # the transcript text reading as one speaker's entire side followed by
    # the other's.
    utterances = sorted(utterances, key=lambda u: u.get("start", 0.0))
    segments = None
    if utterances:
        # Deepgram's speaker id is a bare int (0, 1, ...); prefixed to match
        # the "speaker_N" shape the rest of the pipeline expects from every
        # other diarizing provider (see providers/base.py's speaker_slot_index,
        # notion_sync's Speaker-N property mapping -- both parse a trailing
        # number off this exact string).
        # With multichannel, the CHANNEL is the speaker -- meetingcap puts
        # exactly one participant on each (system audio L / mic R), so
        # channel 0/1 maps to speaker_1/speaker_2 directly. Prefer it over
        # Deepgram's acoustic `speaker` field, which is a guess made from
        # the audio; the channel is a fact about how it was recorded.
        # Falls back to `speaker` whenever the response has no channel
        # information (every non-multichannel request, and any provider
        # response shape that omits it).
        segments = [
            {
                "speaker_id": f"speaker_{(u['channel'] if u.get('channel') is not None else u.get('speaker', 0)) + 1}",
                "text": u.get("transcript", ""),
                "start": u.get("start", 0.0),
                "end": u.get("end", 0.0),
            }
            for u in utterances
        ]
        # Utterances arrive grouped per channel when multichannel is on;
        # the rest of the pipeline (merge_consecutive_segments, the
        # transcript display, loudness annotation) assumes chronological
        # order, so interleave them back into real conversation order.
        segments.sort(key=lambda s: s["start"])

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
