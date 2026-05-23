"""Concatenate per-section WAV bytes into a single MP3 with chapter markers.

v0:
  - Concatenate WAVs in memory (cheap — they all share the same 24 kHz mono
    16-bit format because every Spindle TTS backend normalises to that).
  - Encode to MP3 via ffmpeg (subprocess; faster + battle-tested than pure-
    python LAME bindings).
  - Embed ID3v2 CHAP frames via mutagen so podcast apps can skip between
    sections.
"""
from __future__ import annotations

import io
import logging
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path

log = logging.getLogger(__name__)


def concat_wavs(wav_blobs: list[bytes]) -> bytes:
    """Concatenate same-format WAV blobs into one WAV (24 kHz mono 16-bit)."""
    if not wav_blobs:
        return b""
    if len(wav_blobs) == 1:
        return wav_blobs[0]

    out = io.BytesIO()
    first_reader = wave.open(io.BytesIO(wav_blobs[0]), "rb")
    try:
        with wave.open(out, "wb") as writer:
            writer.setnchannels(first_reader.getnchannels())
            writer.setsampwidth(first_reader.getsampwidth())
            writer.setframerate(first_reader.getframerate())
            writer.writeframes(first_reader.readframes(first_reader.getnframes()))
            for blob in wav_blobs[1:]:
                r = wave.open(io.BytesIO(blob), "rb")
                try:
                    writer.writeframes(r.readframes(r.getnframes()))
                finally:
                    r.close()
    finally:
        first_reader.close()
    return out.getvalue()


def wav_durations_seconds(wav_blobs: list[bytes]) -> list[float]:
    """Per-WAV duration in seconds.

    OpenAI's WAV writer leaves the data-chunk length header as INT32_MAX
    (it's streaming-produced, no known length at header time), so naive
    ``getnframes() / framerate`` blows up. Fall back to buffer-size math
    in that case — see also workers/audio_tts/backends/_util.py.
    """
    out = []
    for blob in wav_blobs:
        if not blob:
            out.append(0.0)
            continue
        r = wave.open(io.BytesIO(blob), "rb")
        try:
            rate = r.getframerate()
            if rate == 0:
                out.append(0.0)
                continue
            nframes = r.getnframes()
            channels = r.getnchannels()
            sample_width = r.getsampwidth()
            if nframes <= 0 or nframes >= 2_000_000_000:
                audio_bytes = max(0, len(blob) - 44)
                denom = max(1, channels * sample_width)
                nframes = audio_bytes // denom
            out.append(nframes / rate)
        finally:
            r.close()
    return out


def encode_wav_to_mp3(wav_bytes: bytes, *, bitrate: str = "64k") -> bytes:
    """Pipe WAV → ffmpeg → MP3."""
    if not shutil.which("ffmpeg"):
        raise RuntimeError(
            "ffmpeg not on PATH. Install it (`brew install ffmpeg` / `apt install ffmpeg`)."
        )
    proc = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel", "error",
            "-f", "wav",
            "-i", "pipe:0",
            "-codec:a", "libmp3lame",
            "-b:a", bitrate,
            "-f", "mp3",
            "pipe:1",
        ],
        input=wav_bytes,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg encode failed (rc={proc.returncode}): "
            f"{proc.stderr.decode(errors='replace')[:500]}"
        )
    return proc.stdout


def embed_chapters(
    mp3_bytes: bytes,
    *,
    title: str,
    chapters: list[tuple[str, float]],
) -> bytes:
    """Add ID3v2 metadata (title) + CHAP frames (one per section).

    Args:
        mp3_bytes: encoded MP3 body.
        title: ID3 TIT2 (the episode title shown in podcast apps).
        chapters: list of ``(chapter_title, start_seconds)`` in playback order.
            End time of chapter N is taken as the start of chapter N+1, or
            None for the last (mutagen treats that as "to end").
    """
    from mutagen.id3 import CHAP, CTOC, ID3, ID3NoHeaderError, TIT2, CTOCFlags

    # mutagen wants a file path; write to tmp, modify in place, read back.
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        f.write(mp3_bytes)
        tmp_path = Path(f.name)

    try:
        try:
            tag = ID3(tmp_path)
        except ID3NoHeaderError:
            tag = ID3()

        tag.delall("TIT2")
        tag.add(TIT2(encoding=3, text=title))

        # Convert start_seconds → milliseconds (ID3 chapter times are ms).
        # End of each chapter = start of the next, or ~end-of-file for last.
        starts_ms = [int(s * 1000) for _, s in chapters]
        # We don't know exact file duration without re-parsing the MP3.
        # Use start of next chapter as end; for the last, use a generous
        # sentinel that's well beyond any reasonable length (most podcast
        # apps clamp this to file duration on playback).
        ends_ms = starts_ms[1:] + [starts_ms[-1] + 60 * 60 * 1000 if starts_ms else 0]

        # Drop any pre-existing chapters / TOC so we own the layout.
        tag.delall("CHAP")
        tag.delall("CTOC")

        element_ids = []
        for i, (chap_title, _start_s) in enumerate(chapters):
            elem_id = f"chap{i:03d}"
            element_ids.append(elem_id)
            tag.add(
                CHAP(
                    element_id=elem_id,
                    start_time=starts_ms[i],
                    end_time=ends_ms[i],
                    start_offset=0xFFFFFFFF,
                    end_offset=0xFFFFFFFF,
                    sub_frames=[TIT2(encoding=3, text=chap_title)],
                )
            )

        if element_ids:
            tag.add(
                CTOC(
                    element_id="toc",
                    flags=CTOCFlags.TOP_LEVEL | CTOCFlags.ORDERED,
                    child_element_ids=element_ids,
                    sub_frames=[TIT2(encoding=3, text=title)],
                )
            )

        tag.save(tmp_path, v2_version=3)
        return tmp_path.read_bytes()
    finally:
        tmp_path.unlink(missing_ok=True)
