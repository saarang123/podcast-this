"""F5-TTS backend (local; GPU recommended).

F5-TTS is voice-cloning-first: every synthesis needs a reference audio clip
plus its transcript. Configure a default reference via the constructor, or
pass ``voice="/path/to/ref.wav"`` (with ``ref_text=...`` or a sidecar
``.txt`` next to the wav) per-call.

Requires the optional ``[f5]`` install. API surface targets ``f5-tts >= 0.6``
and is best treated as untested until exercised end-to-end against the
installed version.
"""
from __future__ import annotations

from pathlib import Path

from ._util import samples_to_wav
from .base import BaseTTS, Voice


class F5TTS(BaseTTS):
    """F5-TTS local synthesis.

    Args:
        model_name: F5-TTS variant (e.g. ``"F5-TTS"``, ``"E2-TTS"``).
        device: torch device string (``"cuda"``, ``"cpu"``, ``"mps"``).
            ``None`` lets f5-tts pick.
        default_ref_audio: path to the reference wav for the default voice.
        default_ref_text: transcript of the reference audio.
    """

    sample_rate = 24_000

    def __init__(
        self,
        model_name: str = "F5-TTS",
        device: str | None = None,
        default_ref_audio: str | Path | None = None,
        default_ref_text: str | None = None,
    ):
        from f5_tts.api import F5TTS as F5TTSCore  # type: ignore

        kwargs: dict = {"model": model_name}
        if device is not None:
            kwargs["device"] = device
        self._core = F5TTSCore(**kwargs)

        self._default_ref_audio = (
            Path(default_ref_audio) if default_ref_audio else None
        )
        self._default_ref_text = default_ref_text

    def list_voices(self) -> list[Voice]:
        if self._default_ref_audio:
            return [
                Voice(
                    id="default",
                    name=f"Default ({self._default_ref_audio.name})",
                    description="Configured default reference voice.",
                )
            ]
        return []

    def synthesize(self, text: str, voice: str | None = None, **opts) -> bytes:
        ref_audio, ref_text = self._resolve_reference(voice, opts)
        wav_samples, sr, _ = self._core.infer(
            ref_file=str(ref_audio),
            ref_text=ref_text,
            gen_text=text,
        )
        return samples_to_wav(wav_samples, sr)

    def _resolve_reference(
        self, voice: str | None, opts: dict
    ) -> tuple[Path, str]:
        if voice is None or voice == "default":
            if not self._default_ref_audio or not self._default_ref_text:
                raise ValueError(
                    "F5TTS has no default voice configured. Pass "
                    "default_ref_audio= and default_ref_text= to the "
                    "constructor, or pass voice='/path/to/ref.wav' with "
                    "ref_text=... per synthesize call."
                )
            return self._default_ref_audio, self._default_ref_text

        ref_audio = Path(voice)
        if not ref_audio.exists():
            raise FileNotFoundError(f"Reference audio not found: {ref_audio}")

        ref_text = opts.get("ref_text")
        if ref_text is None:
            sidecar = ref_audio.with_suffix(".txt")
            if not sidecar.exists():
                raise ValueError(
                    f"No transcript provided for {ref_audio}. Pass ref_text= "
                    f"or place a transcript at {sidecar}."
                )
            ref_text = sidecar.read_text(encoding="utf-8").strip()
        return ref_audio, ref_text
