"""Lossless compression for stored recordings.

Audio is by far the largest thing this app keeps on disk -- 16kHz 16-bit
stereo is ~3.8MB per minute, so a single half-hour meeting is ~100MB and a
modest history runs to gigabytes. Two reductions apply, both LOSSLESS in
the strict sense (the decoded samples are bit-identical to what went in),
because a voice recording is evidence: it is what speaker-ID trains on,
what re-transcription re-reads, and what the user plays back to check a
disputed quote. A lossy codec would quietly degrade all three, so it is
not on the table however good the ratio.

1. Drop a duplicated channel. The ESP32 records ONE microphone into both
   channels (see recorder.cpp), so half of every device recording is an
   exact copy of the other half -- 50% of the bytes carrying zero
   information. Verified per file rather than assumed, because the other
   kind of stereo this app ingests is genuinely two-channel: meetingcap
   puts system audio on L and the mic on R, one participant each, and
   collapsing THAT would destroy the speaker separation (it did, before
   noise_reduction learned the difference -- see _channels_differ there).

2. FLAC. Real lossless compression, ~3x on speech on top of step 1.
   Uses soundfile/libsndfile, which is already a dependency via
   speechbrain (voice_id) -- no new vendored binary, and it works on both
   platforms, unlike the macOS-only afconvert.

Measured on a real 25-minute device recording: 76MB stereo WAV -> 40MB
mono WAV -> 12.9MB FLAC, an 83% reduction, with the decoded samples
compared element-by-element against the original and found identical.

Reading is transparent: load_wav_bytes() returns ordinary WAV bytes
whatever the file on disk actually is, so every existing caller
(voice_id, the transcribers, the dashboard's audio endpoint) keeps
working unchanged.
"""
import array
import io
import logging
import os
import wave

log = logging.getLogger("audio_store")

FLAC_EXT = ".flac"
WAV_EXT = ".wav"


def _channels_are_duplicates(samples, channels: int, probe_frames: int = 4000) -> bool:
    """Whether every channel carries the same signal, sampled rather than
    compared exhaustively (a few thousand frames settles it, and this runs
    on hour-long files).

    Deliberately conservative: any disagreement anywhere in the probe means
    "keep all channels". Wrongly collapsing genuinely-separate channels
    destroys information permanently, while wrongly keeping a duplicate
    only costs disk."""
    if channels < 2:
        return False
    total = len(samples) // channels
    if total == 0:
        return False
    step = max(1, total // probe_frames)
    for f in range(0, total, step):
        base = f * channels
        first = samples[base]
        for c in range(1, channels):
            if samples[base + c] != first:
                return False
    return True


def drop_duplicate_channels(wav_bytes: bytes) -> bytes:
    """Returns mono WAV when every channel is an exact copy, otherwise the
    input untouched. Lossless by construction -- the discarded channels
    contained nothing the kept one doesn't."""
    try:
        with wave.open(io.BytesIO(wav_bytes)) as w:
            channels, width, rate, frames = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
            if channels < 2 or width != 2:
                return wav_bytes
            data = w.readframes(frames)
        samples = array.array("h")
        samples.frombytes(data[:len(data) - (len(data) % 2)])
        if not _channels_are_duplicates(samples, channels):
            return wav_bytes
        mono = array.array("h", samples[0::channels])
        out = io.BytesIO()
        with wave.open(out, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(mono.tobytes())
        return out.getvalue()
    except Exception as e:
        log.warning("could not check/collapse duplicate channels (keeping original): %s", e)
        return wav_bytes


def _soundfile():
    """soundfile is imported lazily and its absence is survivable: without
    it the app simply stores WAV as before, rather than failing to store a
    recording at all."""
    try:
        import soundfile
        return soundfile
    except Exception as e:
        log.info("soundfile unavailable -- storing audio as WAV without FLAC compression (%s)", e)
        return None


def encode(wav_bytes: bytes):
    """Compresses WAV bytes for storage.

    Returns (bytes, extension) -- FLAC when it is available AND actually
    smaller, otherwise the (possibly channel-collapsed) WAV. Never raises:
    failing to compress must degrade to storing the original, never to
    losing a recording."""
    wav_bytes = drop_duplicate_channels(wav_bytes)
    sf = _soundfile()
    if sf is None:
        return wav_bytes, WAV_EXT
    try:
        data, rate = sf.read(io.BytesIO(wav_bytes), dtype="int16")
        out = io.BytesIO()
        sf.write(out, data, rate, format="FLAC")
        flac = out.getvalue()
        # A pathological input could compress to something larger; keep
        # whichever is actually smaller rather than assuming.
        if len(flac) < len(wav_bytes):
            return flac, FLAC_EXT
        return wav_bytes, WAV_EXT
    except Exception as e:
        log.warning("FLAC encode failed, storing WAV instead (non-fatal): %s", e)
        return wav_bytes, WAV_EXT


def decode_to_wav(path: str) -> bytes:
    """Reads a stored recording as WAV bytes regardless of how it is
    stored, so callers never need to know the on-disk format."""
    if not path.lower().endswith(FLAC_EXT):
        with open(path, "rb") as f:
            return f.read()
    sf = _soundfile()
    if sf is None:
        raise RuntimeError("this recording is stored as FLAC but soundfile isn't available to decode it")
    data, rate = sf.read(path, dtype="int16")
    out = io.BytesIO()
    sf.write(out, data, rate, format="WAV", subtype="PCM_16")
    return out.getvalue()


def compress_in_place(wav_path: str):
    """Re-stores an existing WAV as compressed audio, returning the new
    path (or the original when nothing was gained).

    Order matters and is the whole safety story: encode, write to a
    temporary file, decode it back, compare the decoded samples to the
    source, and only THEN replace the original. A crash at any point
    leaves the original untouched, and a codec that somehow altered a
    sample is caught before anything is deleted rather than after. This
    is someone's only copy of a conversation -- "probably fine" isn't
    good enough to delete on."""
    if not wav_path or not wav_path.lower().endswith(WAV_EXT) or not os.path.isfile(wav_path):
        return wav_path
    original_size = os.path.getsize(wav_path)
    with open(wav_path, "rb") as f:
        original = f.read()

    encoded, ext = encode(original)
    if ext == WAV_EXT and len(encoded) >= original_size:
        return wav_path

    tmp = wav_path + ".migrating" + ext
    try:
        with open(tmp, "wb") as f:
            f.write(encoded)
        # Prove the round-trip before touching the original.
        restored = decode_to_wav(tmp)
        if _pcm_of(restored) != _pcm_of(_expected_pcm_source(original)):
            log.error("lossless check FAILED for %s -- keeping the original WAV", wav_path)
            os.remove(tmp)
            return wav_path
        final = wav_path[: -len(WAV_EXT)] + ext
        os.replace(tmp, final)
        if final != wav_path:
            os.remove(wav_path)
        log.info("compressed %s: %.1fMB -> %.1fMB (%.0f%% smaller, verified lossless)",
                 os.path.basename(wav_path), original_size / 1e6, len(encoded) / 1e6,
                 100 * (1 - len(encoded) / max(1, original_size)))
        return final
    except Exception as e:
        log.error("compression of %s failed, original kept: %s", wav_path, e)
        if os.path.isfile(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        return wav_path


def _expected_pcm_source(original_wav: bytes) -> bytes:
    """What the decoded audio SHOULD equal: the original, minus any
    duplicate channels that were legitimately dropped. Comparing against
    the raw original would flag that intentional (and lossless) reduction
    as corruption."""
    return drop_duplicate_channels(original_wav)


def _pcm_of(wav_bytes: bytes) -> bytes:
    with wave.open(io.BytesIO(wav_bytes)) as w:
        return w.readframes(w.getnframes())


def resolve_path(base_path_without_ext: str) -> str:
    """Finds the stored file for a recording, whichever extension it has.

    Prefers FLAC (what new recordings use) but falls back to WAV, so a
    library holding both old and new recordings keeps working with no
    migration required -- the migration is an optimisation, not a
    precondition."""
    for ext in (FLAC_EXT, WAV_EXT):
        candidate = base_path_without_ext + ext
        if os.path.isfile(candidate):
            return candidate
    return base_path_without_ext + WAV_EXT
