"""TTS backends — pluggable text-to-speech for the podcast pipeline.

All backends conform to ``BaseTTS`` and return 24 kHz mono WAV bytes, so the
downstream stitcher / mp3 encoder doesn't need to know which backend produced
the audio.

Quick start::

    from podcast.tts import OpenAITTS

    tts = OpenAITTS()
    wav = tts.synthesize("Hello world.", voice="onyx")

``F5TTS`` and ``KokoroTTS`` pull in heavy local dependencies (torch) and are
behind optional installs (``[f5]``, ``[kokoro]``). They are lazy-imported via
module-level ``__getattr__`` so OpenAI-only callers don't pay the cold-start
cost.
"""
from .base import SAMPLE_RATE, BaseTTS, Voice
from .openai import OpenAITTS

__all__ = ["BaseTTS", "OpenAITTS", "SAMPLE_RATE", "Voice"]


def __getattr__(name: str):
    if name == "F5TTS":
        from .f5 import F5TTS

        return F5TTS
    if name == "KokoroTTS":
        from .kokoro import KokoroTTS

        return KokoroTTS
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return [*__all__, "F5TTS", "KokoroTTS"]
