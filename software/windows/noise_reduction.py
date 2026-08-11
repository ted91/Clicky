"""Background noise suppression for recorded audio, using RNNoise -- a
real-time RNN-based denoiser that distinguishes "voice-like" from
"non-voice" signal. It preserves speech regardless of how many people are
talking (RNNoise has no concept of speaker identity/count, only "does this
sound like a voice"), but it will NOT distinguish a real person talking to
the device from a TV/speaker playing dialogue in the background -- both
look like speech to it. That's a fundamentally different (and much harder)
problem than noise suppression; out of scope here by design, not an
oversight.

Deliberately does NOT depend on the `pyrnnoise` PyPI package -- that
package's own `__init__.py` eagerly imports numpy, audiolab, matplotlib,
click, and tqdm just to reach its ctypes-level RNNoise binding, which is
far more than a packaged desktop app should carry for this one feature
(matches audio_utils.py's own established "pure stdlib, no numpy"
philosophy for audio post-processing). Instead, this vendors just the
compiled shared library those wheels bundle (librnnoise.dylib / rnnoise.dll,
see THIRD_PARTY_NOTICES.md) and talks to it directly via ctypes -- verified
directly that ctypes.c_float arrays built from plain Python floats work
fine with the library's rnnoise_process_frame() call, no numpy required.

RNNoise's model is fixed at 48kHz mono, 480-sample (10ms) frames, float32.
The device records 16kHz/stereo/16-bit (see recorder.cpp) -- 48000/16000 is
exactly 3, so resampling is a simple integer-ratio job (linear-interpolation
upsample, averaging-decimation downsample), not a general-purpose resampler.
"""
import array
import ctypes
import logging
import os
import sys

log = logging.getLogger("noise_reduction")

RNNOISE_SAMPLE_RATE = 48000
RNNOISE_FRAME_SIZE = 480  # 10ms at 48kHz -- fixed by the library itself (rnnoise_get_frame_size())

_lib = None
_lib_load_attempted = False


def _binary_name() -> str:
    if sys.platform == "darwin":
        return "librnnoise.dylib"
    if sys.platform == "win32":
        return "rnnoise.dll"
    raise OSError(f"no vendored RNNoise binary for platform {sys.platform!r}")


def _binary_path() -> str:
    """Same frozen-vs-dev resolution convention as meeting_recorder.py's
    helper_path() -- bundled next to the executable in a PyInstaller build
    (see clicky.spec/clicky_windows.spec's datas), else this file's own
    directory in a dev checkout."""
    name = _binary_name()
    if getattr(sys, "frozen", False):
        return os.path.join(sys._MEIPASS, name)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), name)


def _load_lib():
    """Loads the vendored RNNoise library once per process. Returns None
    (never raises) if it's missing or fails to load -- denoise_wav() treats
    that as "feature unavailable" and passes audio through unchanged rather
    than blocking ingestion on a packaging problem."""
    global _lib, _lib_load_attempted
    if _lib_load_attempted:
        return _lib
    _lib_load_attempted = True
    try:
        path = _binary_path()
        if not os.path.isfile(path):
            log.warning("RNNoise library not found at %s -- noise reduction disabled", path)
            return None
        lib = ctypes.CDLL(path)
        lib.rnnoise_create.argtypes = [ctypes.c_void_p]
        lib.rnnoise_create.restype = ctypes.c_void_p
        lib.rnnoise_destroy.argtypes = [ctypes.c_void_p]
        lib.rnnoise_process_frame.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_float), ctypes.POINTER(ctypes.c_float),
        ]
        lib.rnnoise_process_frame.restype = ctypes.c_float
        lib.rnnoise_get_frame_size.restype = ctypes.c_int
        if lib.rnnoise_get_frame_size() != RNNOISE_FRAME_SIZE:
            log.warning("RNNoise library reports unexpected frame size -- noise reduction disabled")
            return None
        _lib = lib
    except Exception as e:
        log.warning("failed to load RNNoise library -- noise reduction disabled: %s", e)
        _lib = None
    return _lib


def _downmix_to_mono(samples: array.array, channels: int) -> array.array:
    if channels == 1:
        return samples
    mono = array.array("h", bytes(len(samples) // channels * 2))
    for i in range(len(mono)):
        frame = samples[i * channels:(i + 1) * channels]
        mono[i] = sum(frame) // channels
    return mono


def _upsample_3x(samples: array.array) -> array.array:
    """Linear-interpolation upsample by exactly 3x (16kHz -> 48kHz)."""
    n = len(samples)
    if n == 0:
        return array.array("h")
    out = array.array("h", bytes(n * 3 * 2))
    for i in range(n - 1):
        a, b = samples[i], samples[i + 1]
        out[i * 3] = a
        out[i * 3 + 1] = a + (b - a) // 3
        out[i * 3 + 2] = a + 2 * (b - a) // 3
    # Last input sample has no "next" to interpolate toward -- repeat it.
    out[(n - 1) * 3] = samples[n - 1]
    out[(n - 1) * 3 + 1] = samples[n - 1]
    out[(n - 1) * 3 + 2] = samples[n - 1]
    return out


def _downsample_3x(samples: array.array) -> array.array:
    """Averaging decimation by exactly 3x (48kHz -> 16kHz) -- the average
    acts as a crude low-pass filter, reducing (not eliminating) aliasing;
    adequate for voice, not audiophile-grade, matching this module's
    "pure stdlib, no scipy" scope."""
    n = len(samples) // 3
    out = array.array("h", bytes(n * 2))
    for i in range(n):
        a, b, c = samples[i * 3], samples[i * 3 + 1], samples[i * 3 + 2]
        out[i] = (a + b + c) // 3
    return out


def _process_rnnoise(lib, samples_48k: array.array) -> array.array:
    """Runs 16-bit samples through RNNoise frame-by-frame, zero-padding the
    final partial frame. speech_prob (the library's own per-frame verdict)
    is computed but unused here -- see this module's docstring on why "is
    this a voice" isn't the same question as "is this the right voice"."""
    n = len(samples_48k)
    out = array.array("h", bytes(n * 2))
    state = lib.rnnoise_create(None)
    try:
        for start in range(0, n, RNNOISE_FRAME_SIZE):
            chunk = samples_48k[start:start + RNNOISE_FRAME_SIZE]
            pad = RNNOISE_FRAME_SIZE - len(chunk)
            floats = [float(s) for s in chunk] + [0.0] * pad
            buf = (ctypes.c_float * RNNOISE_FRAME_SIZE)(*floats)
            lib.rnnoise_process_frame(state, buf, buf)
            end = start + len(chunk)
            for i in range(len(chunk)):
                v = int(buf[i])
                out[start + i] = 32767 if v > 32767 else (-32768 if v < -32768 else v)
    finally:
        lib.rnnoise_destroy(state)
    return out


def _channels_differ(samples, channels: int, probe_frames: int = 4000) -> bool:
    """Whether a multi-channel recording actually carries different audio
    per channel, rather than one signal duplicated across them.

    This is the difference between the two kinds of stereo this app
    ingests: the ESP32 records a single mic into both channels (identical,
    safe to downmix), while meetingcap records system audio on L and the
    mic on R (one participant each -- downmixing destroys the separation).
    Sampled rather than compared exhaustively: a few thousand frames is
    conclusive for "are these the same signal" and keeps this cheap on an
    hour-long recording."""
    if channels < 2:
        return False
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


def denoise_wav(wav_bytes: bytes, sample_rate: int = 16000) -> bytes:
    """Suppresses background noise in 16-bit PCM WAV audio via RNNoise.
    Mono or stereo in; always returns the same channel count/duration/
    sample rate it was given.

    Channels that genuinely differ (meetingcap's system-audio-on-L,
    mic-on-R capture -- one participant per channel) are denoised
    independently and kept separate, because that separation IS the
    speaker attribution and is far more reliable than diarizing a mix.
    Channels that are copies of each other (the ESP32 records one mic into
    both) are downmixed, denoised once, and duplicated back -- there's no
    real per-channel difference to preserve there, and processing one
    track instead of two is half the work. See _channels_differ.

    Never raises -- returns the input unchanged on any failure (library
    missing/failed to load, corrupt/unrecognized WAV, unexpected format),
    since noise reduction is a quality enhancement that must never block a
    recording from being ingested. Apply BEFORE audio_utils.normalize_wav()
    (see storage.add_pending) so peak normalization doesn't first amplify
    the noise floor this function is about to remove.
    """
    try:
        lib = _load_lib()
        if lib is None:
            return wav_bytes
        if len(wav_bytes) < 44 or wav_bytes[0:4] != b"RIFF" or wav_bytes[8:12] != b"WAVE":
            return wav_bytes

        channels = int.from_bytes(wav_bytes[22:24], "little")
        bits_per_sample = int.from_bytes(wav_bytes[34:36], "little")
        if bits_per_sample != 16 or channels < 1:
            return wav_bytes  # only 16-bit PCM supported -- matches audio_utils.py's own scope

        header = wav_bytes[:44]
        data = wav_bytes[44:]
        if len(data) % 2 != 0:
            data = data[:-1]
        if not data:
            return wav_bytes

        samples = array.array("h")
        samples.frombytes(data)
        if sys.byteorder == "big":
            samples.byteswap()

        def _denoise_one(track):
            """Runs one channel's samples through RNNoise at 48kHz and
            returns them at the original rate and length."""
            up = _upsample_3x(track) if sample_rate * 3 == RNNOISE_SAMPLE_RATE else track
            out = _process_rnnoise(lib, up)
            out = _downsample_3x(out) if sample_rate * 3 == RNNOISE_SAMPLE_RATE else out
            # Trim/pad to exactly the original sample count -- resampling
            # round-trips can be off by a sample or two at the tail.
            if len(out) > len(track):
                out = out[:len(track)]
            elif len(out) < len(track):
                out = out + array.array("h", [0] * (len(track) - len(out)))
            return out

        if channels > 1 and _channels_differ(samples, channels):
            # Genuinely distinct channels: denoise each ON ITS OWN and keep
            # them separate.
            #
            # This used to downmix to mono and duplicate the result back
            # across every channel, on the assumption (true for the ESP32,
            # which records one mic as stereo) that channels carry no real
            # difference. That assumption is false for a Mac meeting
            # recording, where meetingcap deliberately puts system audio on
            # L and the mic on R -- one channel per participant. Collapsing
            # them destroyed a perfect speaker separation before Deepgram
            # ever saw the audio, leaving diarization to guess who spoke
            # from a mono mix of two people. Confirmed on a real recording:
            # L and R came out bit-identical, and the transcript attributed
            # one speaker's answers to the other.
            tracks = []
            for c in range(channels):
                track = array.array("h", samples[c::channels])
                tracks.append(_denoise_one(track))
            frames = min(len(t) for t in tracks)
            out_samples = array.array("h", bytes(frames * channels * 2))
            for c, track in enumerate(tracks):
                for i in range(frames):
                    out_samples[i * channels + c] = track[i]
        else:
            mono = _downmix_to_mono(samples, channels)
            if not mono:
                return wav_bytes
            denoised_mono = _denoise_one(mono)
            if channels == 1:
                out_samples = denoised_mono
            else:
                out_samples = array.array("h", bytes(len(denoised_mono) * channels * 2))
                for i, v in enumerate(denoised_mono):
                    for c in range(channels):
                        out_samples[i * channels + c] = v

        if sys.byteorder == "big":
            out_samples.byteswap()
        return header + out_samples.tobytes()
    except Exception as e:
        log.warning("noise reduction failed, passing audio through unchanged: %s", e)
        return wav_bytes
