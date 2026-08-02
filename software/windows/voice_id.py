"""Local speaker-voiceprint recognition -- SpeechBrain's ECAPA-TDNN
(speechbrain/spkrec-ecapa-voxceleb), entirely on-device: embeddings never
leave the machine, unlike every other STT/LLM provider this app talks to.

This is a real, deliberate departure from the app's usual "avoid heavy pip
dependencies" posture (see noise_reduction.py's vendored-dylib approach) --
torch + speechbrain add several hundred MB to the packaged app and a slower
PyInstaller build. Chosen anyway for accuracy/simplicity over a lighter
onnxruntime alternative (see the session's plan doc). Model weights
(~80MB) download from HuggingFace on first real use and cache under
paths.APP_DATA_DIR -- unlike RNNoise's fully-offline vendored binary, this
needs network access once. Gated behind settings.get_all()["voice_id_enabled"]
(default ON -- see is_enabled() below; users can still opt out).

Enrollment writes a running-average embedding per person (see
enroll_or_update) rather than keeping every sample -- simpler storage, and
a slowly-updating average is more robust to one bad/noisy clip than
whichever embedding happened to be computed last.

Recognition is exposed as a SUGGESTION only (see poller.process_once's
speaker_name_suggestions field) -- this module itself has no concept of
"confirmed", callers decide what to do with a match.
"""
import json
import logging
import math
import os
import re
import threading
from datetime import datetime, timezone

import paths
import settings

log = logging.getLogger("voice_id")

_PATH = paths.VOICEPRINTS_PATH
_lock = threading.Lock()

MODEL_SOURCE = "speechbrain/spkrec-ecapa-voxceleb"
EXPECTED_SAMPLE_RATE = 16000  # this device's recordings are already 16kHz mono -- no resampling needed
MIN_ENROLL_SECONDS = 2.0  # SpeechBrain's own short-utterance floor -- below this an embedding is unreliable
DEFAULT_MATCH_THRESHOLD = 0.75  # judgment-call default, not a measured/tuned value -- see module docstring

# Jarvis BOOT commands are short (often 1-5 words) and produce noisier
# embeddings than a multi-minute meeting/memo recording -- real command
# clips scored 0.66-0.79 against a genuinely correct match in testing, so
# DEFAULT_MATCH_THRESHOLD's 0.75 missed most of them. A command is also a
# structurally different, lower-risk case than the memo/meeting "who said
# this" suggestion (see match_candidates' docstring): there's exactly one
# person who could plausibly be talking to their own device, so a looser
# bar for auto-applying the top candidate is appropriate here specifically,
# not a case for lowering DEFAULT_MATCH_THRESHOLD everywhere.
COMMAND_MATCH_THRESHOLD = 0.6

OWNER_KEY = "__owner__"  # reserved -- can't collide with a real person's name (see enrollment path A)

_model = None
_model_lock = threading.Lock()


def _load() -> dict:
    if not os.path.exists(_PATH):
        return {}
    with open(_PATH, "r") as f:
        return json.load(f)


def _save(data: dict):
    tmp_path = _PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, _PATH)


def is_enabled() -> bool:
    # Default ON (unlike this feature's initial opt-in design) -- user
    # asked for it on by default once tested; explicit False in settings
    # still turns it off.
    return bool(settings.get_all().get("voice_id_enabled", True))


def normalize_person_key(name_or_email: str) -> str:
    """Canonical storage key for a person's voiceprint -- an email as-is
    (lowercased/stripped), or a normalized name otherwise. Not the same
    object as notion_sync/obsidian_sync's People-page identity resolution
    (those query live Notion/vault state), but the same "email beats name"
    priority those already use, applied to a local-only lookup key."""
    key = (name_or_email or "").strip().lower()
    if "@" in key:
        return key
    key = re.sub(r"[^\w\s-]", "", key)
    key = re.sub(r"[\s_]+", "-", key)
    return key or "unknown"


def _load_model():
    """Lazy singleton -- only loaded (and only downloads model weights) the
    first time voice ID is actually used, so users who never enable this
    feature pay zero startup/memory/network cost."""
    global _model
    with _model_lock:
        if _model is None:
            from speechbrain.inference.speaker import EncoderClassifier
            _model = EncoderClassifier.from_hparams(
                source=MODEL_SOURCE,
                savedir=os.path.join(paths.APP_DATA_DIR, "voice_id_model"),
            )
        return _model


def compute_embedding(samples, sample_width: int, sample_rate: int = EXPECTED_SAMPLE_RATE):
    """samples: an array.array of signed PCM ints (see
    audio_analysis.extract_segment_pcm). Returns a plain list[float]
    (JSON-storable) embedding, or None if there's not enough audio
    (MIN_ENROLL_SECONDS) or the model fails to load/run."""
    if not samples or len(samples) < MIN_ENROLL_SECONDS * sample_rate:
        return None
    try:
        import torch
        model = _load_model()
        max_amplitude = float(1 << (8 * sample_width - 1))
        floats = [s / max_amplitude for s in samples]
        tensor = torch.tensor(floats, dtype=torch.float32).unsqueeze(0)
        with torch.no_grad():
            embedding = model.encode_batch(tensor)
        return embedding.squeeze().tolist()
    except Exception as e:
        log.warning("failed to compute voice embedding: %s", e)
        return None


def cosine_similarity(a: list, b: list) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def get_voiceprint(person_key: str) -> dict:
    with _lock:
        return _load().get(person_key)


def list_voiceprints() -> dict:
    with _lock:
        return _load()


def delete_voiceprint(person_key: str):
    with _lock:
        data = _load()
        data.pop(person_key, None)
        _save(data)


def delete_all_voiceprints():
    with _lock:
        _save({})


def enroll_or_update(person_key: str, embedding: list, display_name: str = None):
    """Folds a new embedding into person_key's running-average voiceprint
    (weighted by prior sample_count) -- see module docstring for why an
    average, not a growing list of every sample. `display_name` is stored
    alongside the (normalized, not always human-readable) key so callers
    can show something nicer than e.g. "john-doe" or the reserved
    OWNER_KEY -- defaults to whatever was already stored, or the key
    itself for a brand-new enrollment. This is also how a corrected
    suggestion "retrains" itself: calling this again under the right key
    after a wrong match just keeps averaging under whichever key the
    caller passes -- a prior wrong-key enrollment isn't retroactively
    removed (acceptable fast-follow gap, not a blocker, per the plan)."""
    if not embedding:
        return
    with _lock:
        data = _load()
        existing = data.get(person_key)
        display = display_name or (existing or {}).get("display_name") or person_key
        if existing and existing.get("embedding") and len(existing["embedding"]) == len(embedding):
            n = existing.get("sample_count", 1)
            merged = [(e * n + new) / (n + 1) for e, new in zip(existing["embedding"], embedding)]
            data[person_key] = {"embedding": merged, "sample_count": n + 1, "display_name": display,
                                 "updated_at": datetime.now(timezone.utc).isoformat()}
        else:
            data[person_key] = {"embedding": embedding, "sample_count": 1, "display_name": display,
                                 "updated_at": datetime.now(timezone.utc).isoformat()}
        _save(data)


def match(embedding: list, threshold: float = DEFAULT_MATCH_THRESHOLD):
    """Best cosine match across all enrolled voiceprints above threshold.
    Returns (person_key, display_name, score) or None. Callers treat this
    as a SUGGESTION, never a silent confirmed identity (see module
    docstring)."""
    if not embedding:
        return None
    best_key, best_score = None, threshold
    for person_key, entry in list_voiceprints().items():
        score = cosine_similarity(embedding, entry.get("embedding") or [])
        if score > best_score:
            best_key, best_score = person_key, score
    if best_key is None:
        return None
    display = list_voiceprints().get(best_key, {}).get("display_name") or best_key
    return best_key, display, best_score


# Below DEFAULT_MATCH_THRESHOLD but still worth mentioning as "might be
# one of these" -- see match_candidates. Below this, a score is close
# enough to a random/unrelated voiceprint that surfacing it would be more
# confusing than helpful.
MIN_CANDIDATE_SCORE = 0.5


def match_candidates(embedding: list, top_n: int = 3, min_score: float = MIN_CANDIDATE_SCORE):
    """For the ambiguous case where nothing clears match()'s confident
    threshold but a few enrolled voices are still plausibly close --
    returns up to top_n (person_key, display_name, score) tuples above
    min_score, sorted best-first, so the user can be offered "might be one
    of these" instead of an empty field. Never auto-applies anything --
    same suggestion-only posture as match()."""
    if not embedding:
        return []
    scored = []
    for person_key, entry in list_voiceprints().items():
        score = cosine_similarity(embedding, entry.get("embedding") or [])
        if score >= min_score:
            display = entry.get("display_name") or person_key
            scored.append((person_key, display, score))
    scored.sort(key=lambda t: t[2], reverse=True)
    return scored[:top_n]


def embedding_for_speaker(wav_bytes: bytes, segments: list, speaker_id: str, channel_index: int = 0):
    """Concatenates every segment belonging to speaker_id (channel_index --
    0 for a normal mono/duplicated-mono recording, 1 for a meeting
    recording's mic-only channel, see guess_owner_speaker_id) into one PCM
    buffer and computes its embedding. Returns None if there's not enough
    total audio (MIN_ENROLL_SECONDS) or the WAV/segments don't line up."""
    import audio_analysis

    all_samples = None
    sample_width = None
    for seg in segments:
        if seg.get("speaker_id") != speaker_id:
            continue
        result = audio_analysis.extract_segment_pcm(wav_bytes, seg.get("start", 0), seg.get("end", 0), channel_index)
        if not result:
            continue
        samples, sample_width, _rate = result
        if all_samples is None:
            all_samples = list(samples)
        else:
            all_samples.extend(samples)
    if not all_samples or sample_width is None:
        return None
    return compute_embedding(all_samples, sample_width)


def guess_owner_speaker_id(wav_bytes: bytes, segments: list):
    """Meeting recordings only: meetingcap records real stereo -- left
    (channel 0) = system audio (other participants), right (channel 1) =
    mic (the device owner), see meetingcap/main.swift. For each speaker_id,
    compares average RMS on the mic channel vs. the system channel across
    that speaker's segments; whichever speaker_id's audio is confidently
    louder on the mic channel is almost certainly the owner talking.
    Returns that speaker_id, or None if no speaker clearly dominates the
    mic channel (ambiguous -- don't guess)."""
    import audio_analysis

    mic_minus_system = {}
    for seg in segments:
        sid = seg.get("speaker_id")
        if sid is None:
            continue
        mic = audio_analysis.extract_segment_pcm(wav_bytes, seg.get("start", 0), seg.get("end", 0), channel_index=1)
        system = audio_analysis.extract_segment_pcm(wav_bytes, seg.get("start", 0), seg.get("end", 0), channel_index=0)
        if not mic or not system:
            continue
        mic_db = audio_analysis._rms_db(mic[0], mic[1])
        system_db = audio_analysis._rms_db(system[0], system[1])
        mic_minus_system.setdefault(sid, []).append(mic_db - system_db)

    if not mic_minus_system:
        return None
    averages = {sid: sum(vals) / len(vals) for sid, vals in mic_minus_system.items()}
    best_sid = max(averages, key=averages.get)
    # Require the mic channel to be clearly dominant (>6dB), not just
    # marginally louder -- ambiguous cases should fall through to the
    # existing rename/guess flow rather than risk a wrong silent enrollment.
    if averages[best_sid] < 6.0:
        return None
    return best_sid
