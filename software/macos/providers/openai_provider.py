"""OpenAI: Whisper API for transcription, GPT chat completion for
summarization. SDK: pip install openai
"""
import io
import config
from providers.base import build_summary_prompt, parse_summary_json


def _client():
    from openai import OpenAI
    if not config.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not set in .env")
    return OpenAI(api_key=config.OPENAI_API_KEY)


def transcribe(wav_bytes: bytes) -> dict:
    client = _client()
    audio_file = io.BytesIO(wav_bytes)
    audio_file.name = "recording.wav"  # SDK infers format from this
    result = client.audio.transcriptions.create(
        model=config.OPENAI_STT_MODEL,
        file=audio_file,
    )
    return {"text": result.text, "segments": None}  # no diarization support


def summarize(transcript: str, deepgram_insights: dict = None, meeting: dict = None) -> dict:
    client = _client()
    resp = client.chat.completions.create(
        model=config.OPENAI_LLM_MODEL,
        messages=[{"role": "user", "content": build_summary_prompt(transcript, deepgram_insights, meeting)}],
    )
    return parse_summary_json(resp.choices[0].message.content)


def complete(prompt: str) -> str:
    """Plain text completion for non-transcript prompts (e.g. the person-
    knowledge merge, see notion_sync.push_people) -- same client/model as
    summarize(), no JSON-schema expectations on the output."""
    client = _client()
    resp = client.chat.completions.create(
        model=config.OPENAI_LLM_MODEL,
        messages=[{"role": "user", "content": prompt}],
    )
    return (resp.choices[0].message.content or "").strip()
