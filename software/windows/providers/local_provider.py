"""Fully local, zero-cost path: faster-whisper for transcription (runs on
your Mac's CPU/GPU) and Ollama for summarization (a local model server you
run yourself). Nothing leaves your machine, no API key needed.

SDK: pip install faster-whisper ollama
Also requires Ollama installed and running (https://ollama.com) with a
model pulled, e.g. `ollama pull llama3`.
"""
import io
import config
from providers.base import build_summary_prompt, parse_summary_json

_whisper_model = None  # lazy singleton — loading the model is slow


def _get_whisper_model():
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        _whisper_model = WhisperModel(config.LOCAL_WHISPER_MODEL, device="cpu", compute_type="int8")
    return _whisper_model


def transcribe(wav_bytes: bytes) -> dict:
    model = _get_whisper_model()
    segments, _info = model.transcribe(io.BytesIO(wav_bytes))
    text = " ".join(segment.text.strip() for segment in segments).strip()
    return {"text": text, "segments": None}  # faster-whisper doesn't diarize


def summarize(transcript: str, deepgram_insights: dict = None, meeting: dict = None) -> dict:
    import ollama
    client = ollama.Client(host=config.OLLAMA_BASE_URL)
    resp = client.chat(
        model=config.OLLAMA_MODEL,
        messages=[{"role": "user", "content": build_summary_prompt(transcript, deepgram_insights, meeting)}],
    )
    return parse_summary_json(resp["message"]["content"])


def complete(prompt: str) -> str:
    """Plain text completion for non-transcript prompts (e.g. the person-
    knowledge merge, see notion_sync.push_people) -- same client/model as
    summarize(), no JSON-schema expectations on the output."""
    import ollama
    client = ollama.Client(host=config.OLLAMA_BASE_URL)
    resp = client.chat(
        model=config.OLLAMA_MODEL,
        messages=[{"role": "user", "content": prompt}],
    )
    return (resp["message"]["content"] or "").strip()
