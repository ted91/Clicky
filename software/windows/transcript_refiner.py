"""After-recording transcript for live recordings: who spoke when, then what they said.

Each speaker track ("you" = microphone, "others" = computer audio) is
  1. split into speaker turns by voice (speaker_diarization.py), then
  2. each turn is transcribed on its own with Parakeet (the live model, but on whole turns instead of
     live windows -- full context, no clipped words, and the language is picked per turn, so meetings
     that switch between German and English stay correct).
Tested against Whisper large-v3-turbo on the same audio: equally accurate per turn and ~15x faster
(~0.2x the recording length including diarization); Whisper on whole tracks picked one language per
track and turned German into English.

Speaker names:
  * call voices (computer audio) -> Speaker 1, Speaker 2, ... in order of first appearance;
  * microphone during an online call -> its main voice is the owner, other clear voices get a number;
  * microphone with no call (people in the room) -> every voice gets a number, the owner too -- a
    voice on the mic isn't necessarily the owner. Speakers are renamed on the dashboard as before.
Mic lines that are only the call echoing out of the speakers are dropped.

Flow: live_recorder.ingest() saves the tracks and parks the recording in status "refining" (the
poller only takes pending/failed) -> this worker replaces the live transcript and sets status back to
"pending" -> the normal pipeline (summary, Notion/Obsidian) runs on the result. Live segments are kept
as prefilled_transcription["live_segments"]; on any failure the live transcript is used unchanged.
Recordings still "refining" at startup are picked up again (start_worker()).
"""
import logging
import queue
import re
import threading
import wave
from datetime import datetime

import numpy as np

log = logging.getLogger("transcript_refiner")

RATE = 16000
ENGINE = "parakeet-per-speaker-turn"
_SILENCE_PEAK = 1e-3
_MAX_PIECE_S = 20.0            # longer turns are split at pauses before decoding
_PAD_S = 0.2

_q = queue.Queue()
_worker = None


def is_enabled() -> bool:
    import settings
    import live_transcriber
    return bool(settings.get_all().get("refine_transcripts", True)) and live_transcriber.models_available()


def enqueue(content_hash: str):
    start_worker()
    _q.put(content_hash)


def start_worker():
    """Starts the background worker once, and re-queues recordings left "refining" by a restart."""
    global _worker
    if _worker is not None:
        return
    _worker = threading.Thread(target=_run, daemon=True, name="transcript-refiner")
    _worker.start()
    try:
        import storage
        for rec in storage.list_recordings():
            if rec.get("status") == "refining":
                _q.put(rec["content_hash"])
    except Exception as e:
        log.warning("couldn't scan for unfinished refinements: %s", e)


def _read_track(path: str) -> np.ndarray:
    with wave.open(path) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768


def _run():
    import storage
    seen = set()
    while True:
        content_hash = _q.get()
        if content_hash in seen:
            continue
        seen.add(content_hash)
        try:
            refine(content_hash)
        except Exception as e:
            log.exception("refinement failed for %s, keeping the live transcript", content_hash)
            storage.update_fields(content_hash, status="pending", refine_error=str(e))


# --- transcription -------------------------------------------------------------------------------

def _decode(audio: np.ndarray) -> str:
    import live_transcriber
    words = live_transcriber._transcribe(audio)
    return " ".join(w for w, _ in words).strip()


def _transcribe_turn(audio: np.ndarray, start: float, end: float) -> list:
    """[(start_s, end_s, text)] for one speaker turn; long turns are split at pauses."""
    a = audio[int(max(0.0, start - _PAD_S) * RATE):int((end + _PAD_S) * RATE)]
    if a.size < RATE // 5:
        return []
    if end - start <= _MAX_PIECE_S:
        text = _decode(a)
        return [(start, end, text)] if text else []
    from providers.parakeet_provider import _speech_segments
    base = max(0.0, start - _PAD_S)
    out = []
    for s0, samples in _speech_segments(a):
        text = _decode(samples)
        if text:
            s = base + s0 / RATE
            out.append((s, s + len(samples) / RATE, text))
    return out


def _vad_turns(audio: np.ndarray) -> list:
    """Speech regions without speaker information (when the diarization models are missing)."""
    from providers.parakeet_provider import _speech_segments
    return [(s / RATE, s / RATE + len(x) / RATE, None) for s, x in _speech_segments(audio)]


# Parakeet writes a German "ja" as "Yeah" when it stands alone -- put it back for German speakers.
_GERMAN_HINTS = {"und", "ich", "wir", "das", "ist", "nicht", "der", "die", "es", "auch", "aber", "noch",
                 "mit", "für", "auf", "dann", "genau", "schon", "haben", "wird", "sind", "kann", "also"}
_YEAH = re.compile(r"\b(Yeah|yeah|Yes|yes)\b")


def _looks_german(text: str) -> bool:
    words = [w.strip(".,!?;:").lower() for w in text.split()]
    return any(w in _GERMAN_HINTS for w in words) or bool(re.search(r"[äöüß]", text.lower()))


def _fix_german_yeah(segments):
    by_voice = {}
    for s in segments:
        by_voice.setdefault((s["track"], s.get("cluster")), []).append(s)
    for segs in by_voice.values():
        words = [w.strip(".,!?;:").lower() for s in segs for w in s["text"].split()]
        german = sum(w in _GERMAN_HINTS for w in words)
        if words and german >= 3 and german / len(words) > 0.15:
            for s in segs:
                s["text"] = _YEAH.sub(lambda m: "Ja" if m.group(0)[0].isupper() else "ja", s["text"])


# --- speakers ------------------------------------------------------------------------------------

def _assign_speakers(segments, talk, in_call, owner_track="you"):
    """Sets speaker_id on every segment; returns {new id: "Speaker N"}. talk: {(track, cluster): seconds}."""
    import speaker_diarization as sd
    owner_keys = set()
    if in_call:
        mic = {k: v for k, v in talk.items() if k[0] == owner_track}
        if mic:
            main = max(mic, key=mic.get)
            owner_keys = {k for k, v in mic.items() if k == main or v < sd.MIC_GUEST_MIN_S}
    ids, names = {}, {}
    for s in sorted(segments, key=lambda s: s["start"]):
        key = (s["track"], s.get("cluster"))
        if key in owner_keys:
            s["speaker_id"] = owner_track
            continue
        if key not in ids:
            ids[key] = f"s{len(ids) + 1}"
            names[ids[key]] = f"Speaker {len(ids)}"
        s["speaker_id"] = ids[key]
    return names


def refine(content_hash: str):
    import settings
    import storage
    rec = storage.get_recording(content_hash)
    if not rec or rec.get("status") != "refining":
        return
    tracks = rec.get("source_tracks") or {}
    live = rec.get("prefilled_transcription") or {}
    live_segments = live.get("live_segments") or live.get("segments") or []
    audios = {}
    for sid, path in tracks.items():
        try:
            a = _read_track(path)
        except (OSError, wave.Error):
            continue
        if a.size and float(np.max(np.abs(a))) >= _SILENCE_PEAK:
            audios[sid] = a                        # muted mic / no computer audio: nothing to hear
    in_call = rec.get("in_call")
    if in_call is None:                            # recorded before call detection was stored
        in_call = "others" in audios
    log.info("transcribing %s by speaker turn (%d track(s), %s)", rec["name"], len(audios),
             "online call" if in_call else "in-person")

    try:
        import speaker_diarization as sd
        diarize = sd.diarize if sd.models_available() else None
    except Exception:
        diarize = None
    segments, talk = [], {}
    for sid, audio in audios.items():
        turns = diarize(audio) if diarize else _vad_turns(audio)
        for start, end, cluster in turns:
            talk[(sid, cluster)] = talk.get((sid, cluster), 0.0) + (end - start)
            for s, e, text in _transcribe_turn(audio, start, end):
                segments.append({"speaker_id": sid, "track": sid, "cluster": cluster, "text": text,
                                 "start": round(s, 2), "end": round(e, 2)})
    segments.sort(key=lambda s: s["start"])

    if not segments:
        log.info("no speech found in %s, keeping the live transcript", rec["name"])
        storage.update_fields(content_hash, status="pending", refine_note="no speech found; live transcript kept")
        return

    _fix_german_yeah(segments)
    if diarize:
        dropped = sd.drop_echo(segments, owner_track="you", call_track="others")
        if dropped:
            log.info("dropped %d microphone line(s) that were the call echoing from the speakers", dropped)
    owner = (settings.get_all().get("owner_name") or "").strip() or "You"
    if diarize:
        speaker_names = {"you": owner, **_assign_speakers(segments, talk, in_call)}
    else:
        speaker_names = {"you": owner if in_call else "Microphone (room)", "others": "Others"}
    _retranslate(segments, live_segments)
    for s in segments:
        s.pop("cluster", None)

    refined = {"text": " ".join(s["text"] for s in segments), "segments": segments,
               "stt_provider": ENGINE, "live_segments": live_segments}
    storage.update_fields(content_hash, prefilled_transcription=refined, status="pending",
                          speaker_names=speaker_names, refined_at=datetime.now().isoformat(timespec="seconds"))
    log.info("transcribed %s: %d lines, speakers %s", rec["name"], len(segments), sorted(set(speaker_names.values())))


def _retranslate(segments, live_segments):
    """Translate the new lines for the tracks (and language) that were translated live."""
    langs = {s.get("track", s["speaker_id"]): s["translation_lang"] for s in live_segments if s.get("translation_lang")}
    if not langs:
        return
    try:
        import live_translator
        tr = live_translator.get_translator()
    except Exception as e:
        log.warning("re-translation unavailable: %s", e)
        return
    for seg in segments:
        lang = langs.get(seg["track"])
        if not lang or (lang == "de" and not _looks_german(seg["text"])):
            continue                                   # an English line in a German meeting needs no translation
        try:
            out = tr.update(seg["text"].split(), [], True, lang)
            seg["translation"] = " ".join(out["sentences"]).strip()
            seg["translation_lang"] = lang
        except Exception as e:
            log.warning("re-translation failed for one line: %s", e)
