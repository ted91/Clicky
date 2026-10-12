"""Lightweight audio post-processing applied once, when a recording is
first ingested (see storage.add_pending) -- pure stdlib (array module),
no numpy/audioop dependency (audioop was removed in Python 3.13, which
this project targets).
"""
import array
import io
import sys
import wave

# What the device itself produces and what noise_reduction.denoise_wav() assumes (see recorder.cpp).
DEVICE_SAMPLE_RATE = 16000

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


def to_device_wav(data: bytes) -> bytes:
    """Converts an uploaded audio/video file (WAV, MP3, M4A, MP4, OGG, FLAC, ...) to the same
    16 kHz mono 16-bit PCM WAV, with a canonical 44-byte header, that the device sends -- the
    format storage.add_pending()'s denoise/normalize steps expect. Raises ValueError with a
    user-facing message if the file can't be read as audio."""
    pcm = _decode_with_pyav(data)
    if pcm is None:
        pcm = _decode_wav_stdlib(data)
    if not pcm:
        raise ValueError("That file doesn't contain any audio Clicky could read.")
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(DEVICE_SAMPLE_RATE)
        w.writeframes(pcm)
    return out.getvalue()


def is_silent_wav(wav_bytes: bytes, threshold: int = 33) -> bool:
    """True when every sample of a 16-bit WAV is within +-threshold (~ -60 dBFS): a muted mic, not speech."""
    import numpy as np
    body = wav_bytes[44:len(wav_bytes) - (len(wav_bytes) - 44) % 2]
    samples = np.frombuffer(body, dtype="<i2")
    return samples.size == 0 or int(np.abs(samples.astype(np.int32)).max()) <= threshold


def _decode_with_pyav(data: bytes):
    """Any container/codec FFmpeg knows, resampled to 16 kHz mono s16. Returns None if PyAV
    isn't installed (caller falls back to plain-WAV decoding)."""
    try:
        import av
    except ImportError:
        return None
    pcm = bytearray()
    has_audio = False
    try:
        with av.open(io.BytesIO(data)) as container:
            stream = next((s for s in container.streams if s.type == "audio"), None)
            if stream is not None:
                has_audio = True
                resampler = av.AudioResampler(format="s16", layout="mono", rate=DEVICE_SAMPLE_RATE)
                for frame in container.decode(stream):
                    for out in resampler.resample(frame):
                        pcm += out.to_ndarray().tobytes()
                for out in resampler.resample(None):
                    pcm += out.to_ndarray().tobytes()
    except Exception:  # av.error.* (some subclass ValueError) -- FFmpeg's own text isn't meaningful to a user
        raise ValueError("That file isn't an audio or video file Clicky can read.")
    if not has_audio:
        raise ValueError("That file has no audio track.")
    return bytes(pcm)


def _decode_wav_stdlib(data: bytes) -> bytes:
    """Fallback without PyAV: plain 16-bit PCM WAV only, downmixed and resampled with numpy/scipy."""
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            channels, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
            frames = w.readframes(w.getnframes())
    except (wave.Error, EOFError):
        raise ValueError("Only WAV files can be read on this install (MP3/M4A support needs the PyAV package).")
    if width != 2:
        raise ValueError("Only 16-bit WAV files can be read on this install.")
    import numpy as np
    samples = np.frombuffer(frames, dtype="<i2").astype(np.float32)
    if channels > 1:
        samples = samples[: len(samples) - len(samples) % channels].reshape(-1, channels).mean(axis=1)
    if rate != DEVICE_SAMPLE_RATE:
        from math import gcd
        from scipy.signal import resample_poly
        g = gcd(rate, DEVICE_SAMPLE_RATE)
        samples = resample_poly(samples, DEVICE_SAMPLE_RATE // g, rate // g)
    return np.clip(np.round(samples), -32768, 32767).astype("<i2").tobytes()
