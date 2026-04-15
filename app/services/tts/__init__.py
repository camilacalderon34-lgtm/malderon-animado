"""
TTS provider registry.

Usage:
    from app.services.tts import get_provider

    provider = get_provider(
        name="elevenlabs",
        api_key="sk-...",
        config={"voice_id": "xyz", "stability": 0.5},
    )
    provider.generate("Hello world", Path("output.mp3"))

To add a new provider:
  1. Create app/services/tts/myprovider.py with a class that extends TTSProvider
  2. Import it here and add it to PROVIDERS
"""
from .base import TTSProvider
from .genaipro import GenAIProTTS
from .elevenlabs import ElevenLabsTTS
from .openai_tts import OpenAITTS
from .xtts import XTTTS

# Registry: provider_name → class
PROVIDERS: dict[str, type[TTSProvider]] = {
    "genaipro":   GenAIProTTS,
    "elevenlabs": ElevenLabsTTS,
    "openai":     OpenAITTS,
    "xtts":       XTTTS,
}


def get_provider(name: str, api_key: str, config: dict) -> TTSProvider:
    """Instantiate and return a TTS provider by name."""
    cls = PROVIDERS.get(name)
    if cls is None:
        available = list(PROVIDERS.keys())
        raise ValueError(f"Unknown TTS provider: {name!r}. Available: {available}")
    return cls(api_key=api_key, config=config)


__all__ = ["TTSProvider", "GenAIProTTS", "ElevenLabsTTS", "OpenAITTS", "XTTTS", "PROVIDERS", "get_provider"]
