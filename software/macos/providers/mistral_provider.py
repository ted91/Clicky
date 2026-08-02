"""Mistral AI: Voxtral for transcription, Mistral chat completion for
summarization. EU-hosted. SDK: pip install mistralai
"""
import config
from providers.base import build_summary_prompt, parse_summary_json

_client_instance = None
_client_api_key = None


def _client():
    # mistralai>=2.x moved the client class under .client -- pinning the
    # import here (not just "from mistralai import Mistral") since that
    # top-level re-export doesn't exist in the version actually installed.
    global _client_instance, _client_api_key
    if not config.MISTRAL_API_KEY:
        raise RuntimeError("MISTRAL_API_KEY is not set in .env")
    # Reused across calls (Jarvis commonly makes several completions per
    # voice turn -- decide_action, then qa/compose) instead of re-instantiating
    # per call; re-created only if the configured key changes (Settings ->
    # API keys can update config.MISTRAL_API_KEY at runtime).
    if _client_instance is None or _client_api_key != config.MISTRAL_API_KEY:
        from mistralai.client import Mistral
        _client_instance = Mistral(api_key=config.MISTRAL_API_KEY)
        _client_api_key = config.MISTRAL_API_KEY
    return _client_instance


def transcribe(wav_bytes: bytes) -> dict:
    client = _client()
    # The SDK's File model accepts bytes | IO[bytes] | BufferedReader, but
    # its pydantic validation is strict about the last two -- a BytesIO
    # doesn't actually satisfy either check, so pass raw bytes directly.
    # diarize=True is real speaker-separation support (verified against the
    # installed SDK's TranscriptionSegmentChunk model, which carries a
    # speaker_id per segment) -- not available on OpenAI/local providers.
    # The API rejects diarize=True with a 422 unless timestamp_granularities
    # explicitly includes "segment" (confirmed live -- every transcription
    # was failing on this before it was added).
    result = client.audio.transcriptions.complete(
        model=config.MISTRAL_STT_MODEL,
        file={"file_name": "recording.wav", "content": wav_bytes},
        diarize=True,
        timestamp_granularities=["segment"],
    )
    segments = None
    if result.segments:
        segments = [
            {"speaker_id": seg.speaker_id, "text": seg.text, "start": seg.start, "end": seg.end}
            for seg in result.segments
        ]
    return {"text": result.text, "segments": segments}


def summarize(transcript: str, deepgram_insights: dict = None, meeting: dict = None) -> dict:
    client = _client()
    resp = client.chat.complete(
        model=config.MISTRAL_LLM_MODEL,
        messages=[{"role": "user", "content": build_summary_prompt(transcript, deepgram_insights, meeting)}],
    )
    return parse_summary_json(resp.choices[0].message.content)


def complete(prompt: str) -> str:
    """Plain text completion for non-transcript prompts (e.g. the person-
    knowledge merge, see notion_sync.push_people) -- same client/model as
    summarize(), no JSON-schema expectations on the output."""
    client = _client()
    resp = client.chat.complete(
        model=config.MISTRAL_LLM_MODEL,
        messages=[{"role": "user", "content": prompt}],
    )
    return (resp.choices[0].message.content or "").strip()
