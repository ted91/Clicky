"""Factories selecting a provider module by name from config.STT_PROVIDER /
config.LLM_PROVIDER. poller.py and app.py should only ever go through these
two functions — never import a provider module directly — so adding a new
provider later means one new file here, zero changes elsewhere.
"""
import config

_PROVIDER_MODULES = {}


def _load(name: str):
    if name not in _PROVIDER_MODULES:
        if name == "mistral":
            from providers import mistral_provider as mod
        elif name == "openai":
            from providers import openai_provider as mod
        elif name == "anthropic":
            from providers import anthropic_provider as mod
        elif name == "local":
            from providers import local_provider as mod
        elif name == "deepgram":
            from providers import deepgram_provider as mod
        elif name == "parakeet":
            from providers import parakeet_provider as mod
        else:
            raise ValueError(f"Unknown provider '{name}' — expected one of: mistral, openai, anthropic, local, deepgram, parakeet")
        _PROVIDER_MODULES[name] = mod
    return _PROVIDER_MODULES[name]


def get_transcriber():
    """Returns (provider_name, transcribe_fn)."""
    name = config.STT_PROVIDER
    return name, _load(name).transcribe


NO_LLM = "none"


def llm_enabled() -> bool:
    """False when the user chose "None" for summarization (transcription only, no LLM API key)."""
    return config.LLM_PROVIDER != NO_LLM


def transcript_only_summary(transcript: str, deepgram_insights: dict = None, meeting: dict = None) -> dict:
    """Stand-in summarize() for LLM_PROVIDER == "none": same shape as providers.base.parse_summary_json,
    all empty, so storage/dashboard/Notion/Obsidian handle the recording like any other (transcript only)."""
    return {"summary": "", "action_items": [], "calendar_events": [], "stakeholders": [],
            "follow_ups": [], "speaker_names": {}, "type": "actionable", "excluded_background_note": None}


def _completion_disabled(prompt: str) -> str:
    raise RuntimeError("Summarization is turned off (Settings -> Providers -> Summarization = None), "
                       "so this step needs an LLM provider.")


def get_summarizer():
    """Returns (provider_name, summarize_fn)."""
    name = config.LLM_PROVIDER
    if name == NO_LLM:
        return name, transcript_only_summary
    return name, _load(name).summarize


def get_completer():
    """Returns (provider_name, complete_fn) -- plain text completion on
    the configured LLM provider, for non-transcript prompts like the
    person-knowledge merge (see notion_sync.push_people). With
    summarization turned off, complete_fn raises RuntimeError -- every
    optional caller already treats an LLM failure as non-fatal."""
    name = config.LLM_PROVIDER
    if name == NO_LLM:
        return name, _completion_disabled
    return name, _load(name).complete
