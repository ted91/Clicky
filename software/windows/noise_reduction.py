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


def denoise_wav(wav_bytes: bytes, sample_rate: int = 16000) -> bytes:
    """Suppresses background noise in 16-bit PCM WAV audio via RNNoise.
    Mono or stereo in (see recorder.cpp -- this device records stereo);
    always returns the same channel count/duration/sample rate it was
    given, with denoised content duplicated back across all channels
    (a single mic feed captured in stereo has no real per-channel
    difference to preserve).

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

        mono = _downmix_to_mono(samples, channels)
        if not mono:
            return wav_bytes
        up = _upsample_3x(mono) if sample_rate * 3 == RNNOISE_SAMPLE_RATE else mono
        denoised_48k = _process_rnnoise(lib, up)
        denoised_mono = _downsample_3x(denoised_48k) if sample_rate * 3 == RNNOISE_SAMPLE_RATE else denoised_48k

        # Trim/pad to exactly the original mono sample count -- resampling
        # round-trips can be off by a sample or two at the tail.
        target_len = len(mono)
        if len(denoised_mono) > target_len:
            denoised_mono = denoised_mono[:target_len]
        elif len(denoised_mono) < target_len:
            denoised_mono = denoised_mono + array.array("h", [0] * (target_len - len(denoised_mono)))

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
