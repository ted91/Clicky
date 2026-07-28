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
        else:
            raise ValueError(f"Unknown provider '{name}' — expected one of: mistral, openai, anthropic, local, deepgram")
        _PROVIDER_MODULES[name] = mod
    return _PROVIDER_MODULES[name]


def get_transcriber():
    """Returns (provider_name, transcribe_fn)."""
    name = config.STT_PROVIDER
    return name, _load(name).transcribe


def get_summarizer():
    """Returns (provider_name, summarize_fn)."""
    name = config.LLM_PROVIDER
    return name, _load(name).summarize


def get_completer():
    """Returns (provider_name, complete_fn) -- plain text completion on
    the configured LLM provider, for non-transcript prompts like the
    person-knowledge merge (see notion_sync.push_people)."""
    name = config.LLM_PROVIDER
    return name, _load(name).complete
