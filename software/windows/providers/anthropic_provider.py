"""Anthropic: Claude for summarization only — Anthropic doesn't offer a
transcription API, so this provider is LLM-only. Pick a different
STT_PROVIDER (mistral/openai/local) if you want to use this for LLM_PROVIDER.
SDK: pip install anthropic
"""
import config
from providers.base import build_summary_prompt, parse_summary_json


def _client():
    from anthropic import Anthropic
    if not config.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set in .env")
    return Anthropic(api_key=config.ANTHROPIC_API_KEY)


def transcribe(wav_bytes: bytes) -> str:
    raise NotImplementedError(
        "Anthropic has no transcription API — set STT_PROVIDER to mistral, "
        "openai, or local instead."
    )


def summarize(transcript: str, deepgram_insights: dict = None, meeting: dict = None) -> dict:
    client = _client()
    resp = client.messages.create(
        model=config.ANTHROPIC_LLM_MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": build_summary_prompt(transcript, deepgram_insights, meeting)}],
    )
    return parse_summary_json(resp.content[0].text)


def complete(prompt: str) -> str:
    """Plain text completion for non-transcript prompts (e.g. the person-
    knowledge merge, see notion_sync.push_people) -- same client/model as
    summarize(), no JSON-schema expectations on the output."""
    client = _client()
    resp = client.messages.create(
        model=config.ANTHROPIC_LLM_MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    return (resp.content[0].text or "").strip()
