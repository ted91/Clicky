"""Lightweight audio post-processing applied once, when a recording is
first ingested (see storage.add_pending) -- pure stdlib (array module),
no numpy/audioop dependency (audioop was removed in Python 3.13, which
this project targets).
"""
import array
import sys

# Cap how far a very quiet recording can be pushed -- without this, a
# near-silent file (e.g. a mic placed too far away) would have its noise
# floor amplified into something loud and unpleasant rather than usefully
# louder speech. 20x (~26 dB) is a generous but bounded boost.
_MAX_GAIN = 20.0


def normalize_wav(wav_bytes: bytes, target_peak: float = 0.85) -> bytes:
    """Peak-normalizes 16-bit PCM audio so quiet recordings come out
    louder and more consistent, without clipping. Finds the loudest sample
    in the file and scales every sample by a factor that brings that peak
    up to `target_peak` of the full int16 range -- deliberately short of
    the ceiling (32767), leaving headroom rather than maximizing loudness
    at the cost of clipping. No-ops (returns the input unchanged) if the
    recording is already at or above the target level, or is pure
    silence (nothing to scale against). Works on mono or stereo -- treats
    the data as a flat sample stream, so channel layout doesn't matter for
    a single global scale factor.

    Applied once at ingestion (storage.add_pending) rather than at
    transcription time, so the boosted level is what you hear on playback
    too, not just what the STT provider sees.
    """
    if len(wav_bytes) < 44:
        return wav_bytes  # not a real WAV file, let the caller's own validation handle it

    header = wav_bytes[:44]
    data = wav_bytes[44:]
    if len(data) % 2 != 0:
        data = data[:-1]  # drop a stray trailing byte -- shouldn't happen, but array.frombytes requires even length

    samples = array.array("h")  # signed 16-bit
    samples.frombytes(data)
    if sys.byteorder == "big":
        samples.byteswap()  # WAV PCM is little-endian; array uses native byte order internally

    peak = max((abs(s) for s in samples), default=0)
    if peak == 0:
        return wav_bytes  # pure silence -- nothing to scale against

    target = int(32767 * target_peak)
    if peak >= target:
        return wav_bytes  # already loud enough, leave it alone

    gain = min(target / peak, _MAX_GAIN)
    for i in range(len(samples)):
        v = int(samples[i] * gain)
        samples[i] = 32767 if v > 32767 else (-32768 if v < -32768 else v)

    if sys.byteorder == "big":
        samples.byteswap()
    return header + samples.tobytes()
