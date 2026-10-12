"""On-board transcription with Parakeet-TDT 0.6B v3 (sherpa-onnx, int8, CPU) -- the same model and
files as live recording (live_transcriber.py) and Live Subtitles. Free, offline, ~25 European languages
detected automatically, noticeably more accurate than faster-whisper "base".

STT-only (no LLM). The whole file is cut into speech segments with Silero VAD, each decoded separately,
so long recordings stay fast and memory-bounded. No speaker separation (same as faster-whisper).
"""
import io
import wave

import numpy as np

RATE = 16000
_MAX_SEGMENT_S = 20.0


def _read_wav(wav_bytes: bytes) -> np.ndarray:
    with wave.open(io.BytesIO(wav_bytes)) as w:
        channels, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
        frames = w.readframes(w.getnframes())
    if width != 2:
        raise RuntimeError("Parakeet transcription expects 16-bit WAV audio.")
    x = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        x = x[: len(x) - len(x) % channels].reshape(-1, channels).mean(axis=1)
    if rate != RATE:
        import soxr
        x = soxr.resample(x, rate, RATE).astype(np.float32)
    return x


def _speech_segments(audio: np.ndarray):
    """[(start_sample, samples)] of speech, split at pauses (and at most ~20 s each)."""
    import sherpa_onnx
    import live_transcriber
    cfg = sherpa_onnx.VadModelConfig()
    cfg.silero_vad.model = live_transcriber._silero_path()
    cfg.silero_vad.threshold = 0.5
    cfg.silero_vad.min_silence_duration = 0.5
    cfg.silero_vad.min_speech_duration = 0.25
    if hasattr(cfg.silero_vad, "max_speech_duration"):
        cfg.silero_vad.max_speech_duration = _MAX_SEGMENT_S
    cfg.sample_rate = RATE
    vad = sherpa_onnx.VoiceActivityDetector(cfg, buffer_size_in_seconds=60)
    out = []
    window = cfg.silero_vad.window_size

    def drain():
        while not vad.empty():
            out.append((vad.front.start, np.array(vad.front.samples, dtype=np.float32)))
            vad.pop()

    for i in range(0, len(audio), window):
        vad.accept_waveform(audio[i:i + window])
        drain()
    vad.flush()
    drain()
    return out


def transcribe(wav_bytes: bytes) -> dict:
    import live_transcriber
    if not live_transcriber.models_available():
        raise RuntimeError("The Parakeet model isn't installed -- run `python live_transcriber.py` once, "
                           "or choose another transcription provider in Settings -> Providers.")
    rec = live_transcriber._get_recognizer()
    pieces = []
    for _start, samples in _speech_segments(_read_wav(wav_bytes)):
        stream = rec.create_stream()
        stream.accept_waveform(RATE, samples)
        rec.decode_stream(stream)
        text = stream.result.text.strip()
        if text:
            pieces.append(text)
    return {"text": " ".join(pieces), "segments": None}


def summarize(transcript: str, deepgram_insights: dict = None, meeting: dict = None) -> dict:
    raise RuntimeError("Parakeet is transcription-only -- pick a summarization provider (or None) separately.")


def complete(prompt: str) -> str:
    raise RuntimeError("Parakeet is transcription-only.")
