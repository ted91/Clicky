"""Volume-based segment classification -- a mitigation for crosstalk at a
noisy conference/venue, NOT true speaker separation. This device has a
single omnidirectional mic (mono content just duplicated into a stereo WAV
container, see firmware/src/recorder.cpp) -- there is no way to separate
two people talking *simultaneously* into one mic; that needs a microphone
array this hardware doesn't have. What IS a real, measurable signal: a
*quieter, more distant* conversation (a different table nearby) sits
noticeably lower in amplitude than whoever the device owner is actually
talking to. That's the one case this module targets -- not overlapping
speech at similar volume, which no software fix here can untangle.

Works entirely on already-downloaded WAV bytes + the diarized segments'
existing start/end timestamps (see providers/base.py's segment shape) --
no firmware change needed.
"""
import array
import logging
import math
import wave
import io
import statistics

log = logging.getLogger("audio_analysis")

# A clearly-audible-but-distant conversation typically sits well below the
# primary speaker at normal on-the-table device distance. This is a
# starting point, not a measured constant -- no real noisy-conference
# recording was available to tune it against; expect to adjust after real
# use (see settings.py's filter_background_conversations toggle, which
# lets this be disabled outright if it misfires).
BACKGROUND_THRESHOLD_DB = 12.0


_TYPECODE_FOR_WIDTH = {1: "b", 2: "h", 4: "i"}  # signed 8/16/32-bit PCM


def _first_channel_samples(frame_bytes: bytes, sample_width: int, n_channels: int):
    """De-interleaves to a plain array of the first channel's samples. Both
    channels are the same physical mic duplicated (see module docstring),
    not real stereo, so only one is needed -- avoids a dependency on the
    `audioop` module, which was removed in Python 3.13 (PEP 594)."""
    typecode = _TYPECODE_FOR_WIDTH.get(sample_width)
    if typecode is None:
        raise ValueError(f"unsupported sample width: {sample_width}")
    samples = array.array(typecode)
    samples.frombytes(frame_bytes[: len(frame_bytes) - (len(frame_bytes) % (sample_width * n_channels))])
    return samples[0::n_channels] if n_channels > 1 else samples


def _rms_db(samples, sample_width: int) -> float:
    """RMS level in dBFS (0 = full-scale, negative = quieter). Silence (or
    no samples) returns a very negative floor rather than -inf, since -inf
    can't be compared/averaged sensibly downstream."""
    if not samples:
        return -120.0
    sum_squares = sum(s * s for s in samples)
    rms = math.sqrt(sum_squares / len(samples))
    if rms <= 0:
        return -120.0
    max_amplitude = float(1 << (8 * sample_width - 1))
    return 20 * math.log10(rms / max_amplitude)


# A recording whose LOUDEST moment never reaches this is dead air -- a
# recording that started by accident, a mic that failed, a button pressed in
# a pocket. Real room tone on a live mic sits comfortably above it, and
# speech is 30-40 dB above that again, so this is deliberately nowhere near
# "quiet speech": it is the floor for "nothing was captured at all".
#
# Erring low on purpose. Being wrong in the "keep it" direction costs a few
# MB of SD card and one useless dashboard card; being wrong in the "delete
# it" direction destroys a recording irrecoverably, since discard_if_silent
# deletes from the card as well (see poller). The asymmetry is the whole
# reason this is -60 and not a friendlier-looking -45.
SILENCE_THRESHOLD_DB = -60.0

# Loudness is measured over short windows and the PEAK window decides,
# NOT the whole-file average. Averaging is the obvious implementation and
# it is wrong in the one case that matters most: a 30-minute recording
# holding ten seconds of real speech averages out to near-silence and would
# be deleted. Peak-of-windows means any single second of real audio anywhere
# in the file saves the whole recording.
SILENCE_WINDOW_SECONDS = 1.0


def peak_window_db(wav_bytes: bytes) -> float:
    """dBFS of the loudest ~1s window. -120.0 for digital silence.

    Raises on unreadable/unsupported audio rather than guessing -- callers
    deciding whether to DELETE something must treat "couldn't measure it" as
    "keep it", which a raised exception makes explicit and a sentinel value
    would quietly blur.
    """
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        sample_width = wf.getsampwidth()
        n_channels = wf.getnchannels()
        framerate = wf.getframerate() or 1
        n_frames = wf.getnframes()
        if not n_frames:
            return -120.0
        window_frames = max(1, int(framerate * SILENCE_WINDOW_SECONDS))
        peak = -120.0
        remaining = n_frames
        while remaining > 0:
            chunk = wf.readframes(min(window_frames, remaining))
            if not chunk:
                break
            remaining -= min(window_frames, remaining)
            samples = _first_channel_samples(chunk, sample_width, n_channels)
            level = _rms_db(samples, sample_width)
            if level > peak:
                peak = level
        return peak


def is_effectively_silent(wav_bytes: bytes, threshold_db: float = SILENCE_THRESHOLD_DB):
    """(is_silent, peak_db) -- True only when NO window reached threshold_db.

    Fails safe: any unreadable/odd audio returns (False, ...) so the caller
    keeps it. This decides whether a recording gets deleted off the SD card,
    so "I don't know" must never mean "delete".
    """
    try:
        peak = peak_window_db(wav_bytes)
    except Exception as e:
        log.warning("could not measure loudness (keeping the recording): %s", e)
        return False, None
    return peak < threshold_db, peak


def _channel_samples(frame_bytes: bytes, sample_width: int, n_channels: int, channel_index: int):
    """De-interleaves to a specific channel's samples -- unlike
    _first_channel_samples, this doesn't assume both channels are the same
    duplicated mono mic. Used by voice_id.py's owner-voice heuristic, which
    needs the mic-only channel specifically (meeting recordings are real
    stereo: left=system audio/other participants, right=mic=device owner,
    see meetingcap/main.swift), not just "channel 0"."""
    typecode = _TYPECODE_FOR_WIDTH.get(sample_width)
    if typecode is None:
        raise ValueError(f"unsupported sample width: {sample_width}")
    samples = array.array(typecode)
    samples.frombytes(frame_bytes[: len(frame_bytes) - (len(frame_bytes) % (sample_width * n_channels))])
    return samples[channel_index::n_channels] if n_channels > 1 else samples


def extract_segment_pcm(wav_bytes: bytes, start: float, end: float, channel_index: int = 0):
    """Slices raw WAV bytes to [start, end) seconds and returns
    (mono_samples, sample_width, frame_rate) for the given channel, or None
    on any error/empty range (fail open, same posture as
    annotate_segment_loudness). Shared low-level slicing primitive --
    both annotate_segment_loudness's RMS classification and voice_id.py's
    speaker-audio extraction/owner-channel heuristic build on this same
    byte-offset math instead of duplicating it."""
    try:
        with wave.open(io.BytesIO(wav_bytes)) as w:
            sample_width = w.getsampwidth()
            frame_rate = w.getframerate()
            n_channels = w.getnchannels()
            frames = w.readframes(w.getnframes())
    except Exception as e:
        log.warning("extract_segment_pcm: could not read WAV: %s", e)
        return None

    bytes_per_frame = sample_width * n_channels
    total_frames = len(frames) // bytes_per_frame if bytes_per_frame else 0
    start_frame = max(0, int(start * frame_rate))
    end_frame = min(total_frames, int(end * frame_rate))
    if end_frame <= start_frame:
        return None
    chunk = frames[start_frame * bytes_per_frame: end_frame * bytes_per_frame]
    try:
        samples = _channel_samples(chunk, sample_width, n_channels, channel_index)
    except Exception as e:
        log.warning("extract_segment_pcm: failed to slice channel: %s", e)
        return None
    return samples, sample_width, frame_rate


def annotate_segment_loudness(wav_bytes: bytes, segments: list) -> list:
    """Adds `rms_db` and `loudness_class` ("primary"|"background") to each
    segment dict, classifying relative to the recording's own dominant
    level -- the median RMS across segments belonging to whichever
    speaker(s) account for the most total talk time (a reasonable proxy
    for "the person the device owner is actually talking to", since a
    background conversation is rarely the majority of the recording).
    Fails open on any error (malformed WAV, out-of-range timestamps,
    empty segments) -- returns segments unmodified rather than raising,
    since wrongly excluding real content from the actual conversation is
    worse than occasionally keeping genuine background noise. Never
    mutates the input list; returns a new one."""
    if not segments:
        return segments
    try:
        with wave.open(io.BytesIO(wav_bytes)) as w:
            sample_width = w.getsampwidth()
            frame_rate = w.getframerate()
            n_channels = w.getnchannels()
            frames = w.readframes(w.getnframes())
    except Exception as e:
        log.warning("annotate_segment_loudness: could not read WAV, skipping (fail open): %s", e)
        return segments

    bytes_per_frame = sample_width * n_channels
    total_frames = len(frames) // bytes_per_frame if bytes_per_frame else 0

    annotated = []
    for seg in segments:
        seg = dict(seg)
        try:
            start_frame = max(0, int(seg.get("start", 0) * frame_rate))
            end_frame = min(total_frames, int(seg.get("end", 0) * frame_rate))
            if end_frame <= start_frame:
                seg["rms_db"] = None
                annotated.append(seg)
                continue
            chunk = frames[start_frame * bytes_per_frame: end_frame * bytes_per_frame]
            mono_samples = _first_channel_samples(chunk, sample_width, n_channels)
            seg["rms_db"] = _rms_db(mono_samples, sample_width)
        except Exception as e:
            log.warning("annotate_segment_loudness: failed on one segment, leaving unclassified (fail open): %s", e)
            seg["rms_db"] = None
        annotated.append(seg)

    # Talk-time per speaker, to find the dominant one(s) as the loudness baseline.
    talk_time = {}
    for seg in annotated:
        sid = seg.get("speaker_id")
        talk_time[sid] = talk_time.get(sid, 0.0) + max(0.0, seg.get("end", 0) - seg.get("start", 0))
    if not talk_time:
        for seg in annotated:
            seg["loudness_class"] = "primary"
        return annotated
    max_talk_time = max(talk_time.values())
    # Any speaker within 60% of the max talk time counts as "dominant" --
    # a real conversation is often two roughly-balanced speakers, not one.
    dominant_speakers = {sid for sid, t in talk_time.items() if t >= 0.6 * max_talk_time}

    baseline_levels = [seg["rms_db"] for seg in annotated
                       if seg.get("speaker_id") in dominant_speakers and seg.get("rms_db") is not None]
    if not baseline_levels:
        for seg in annotated:
            seg["loudness_class"] = "primary"
        return annotated
    baseline_db = statistics.median(baseline_levels)

    for seg in annotated:
        if seg.get("rms_db") is None:
            seg["loudness_class"] = "primary"  # fail open -- unclassifiable, don't exclude
        elif seg["rms_db"] < baseline_db - BACKGROUND_THRESHOLD_DB:
            seg["loudness_class"] = "background"
        else:
            seg["loudness_class"] = "primary"

    return annotated
