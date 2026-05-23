"""Top-level pipeline: source → rewrite → TTS → stitch → MP3 on disk.

Usage: ``podcast gen <markdown-path>``. Each step parallelises across
sections via asyncio.gather + a small concurrency cap.
"""
from __future__ import annotations

import asyncio
import logging
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .rewrite import load_prompt, rewrite_section
from .sources import Document, load
from .spindle_client import SpindleClient
from .stitch import concat_wavs, embed_chapters, encode_wav_to_mp3, wav_durations_seconds

log = logging.getLogger(__name__)


@dataclass
class PipelineConfig:
    spindle_url: str = "http://localhost:8080"
    spindle_auth_token: str | None = None
    tts_config_id: str = "audio-tts-openai-v1"
    tts_voice: str | None = None
    audio_dir: Path = Path("./audio")
    work_dir: Path = Path("./work")
    keep_work_dir: bool = False
    mp3_bitrate: str = "64k"
    rewrite_concurrency: int = 5
    tts_concurrency: int = 5
    cli_binary: str = "claude"


async def generate_podcast(
    source_uri: str | Path,
    cfg: PipelineConfig,
) -> Path:
    """Run the full pipeline for one source document. Returns the path to
    the produced MP3 on disk."""
    overall_start = time.monotonic()

    doc = load(source_uri)
    if not doc.sections:
        raise RuntimeError(f"source {source_uri!r} has no sections to narrate")
    log.info(
        "loaded source: title=%r sections=%d", doc.title, len(doc.sections)
    )

    job_slug = _slugify(doc.title) or "episode"
    job_work_dir = (cfg.work_dir / f"{int(time.time())}-{job_slug}").resolve()
    rewrite_dir = job_work_dir / "rewrites"
    rewrite_dir.mkdir(parents=True, exist_ok=True)

    rewrite_prompt = load_prompt()

    # ---- step 1: rewrite each section in parallel ----------------------------
    rewrite_sem = asyncio.Semaphore(cfg.rewrite_concurrency)

    async def _rewrite_one(idx: int, section_text: str, heading: str) -> str:
        async with rewrite_sem:
            return await rewrite_section(
                section_text=section_text,
                section_idx=idx,
                heading=heading,
                rewrite_prompt=rewrite_prompt,
                work_dir=rewrite_dir,
                cli_binary=cfg.cli_binary,
            )

    rewrite_start = time.monotonic()
    rewritten = await asyncio.gather(
        *(
            _rewrite_one(i, s.content, s.heading)
            for i, s in enumerate(doc.sections)
        )
    )
    log.info(
        "rewrites complete: %d sections in %.1fs",
        len(rewritten), time.monotonic() - rewrite_start,
    )

    # ---- step 2: TTS each rewritten section in parallel via Spindle ---------
    tts_sem = asyncio.Semaphore(cfg.tts_concurrency)

    async with SpindleClient(
        base_url=cfg.spindle_url, auth_token=cfg.spindle_auth_token
    ) as spindle:

        async def _tts_one(idx: int, text: str) -> bytes:
            async with tts_sem:
                return await spindle.synthesize_to_wav(
                    text=text,
                    config_id=cfg.tts_config_id,
                    voice=cfg.tts_voice,
                    idempotency_key=f"{job_slug}-{int(time.time())}-{idx:02d}",
                )

        tts_start = time.monotonic()
        wav_blobs = await asyncio.gather(
            *(_tts_one(i, text) for i, text in enumerate(rewritten))
        )
        log.info(
            "tts complete: %d sections in %.1fs (%.0f KB total)",
            len(wav_blobs),
            time.monotonic() - tts_start,
            sum(len(b) for b in wav_blobs) / 1024,
        )

    # ---- step 3: build chapter table from per-section durations -------------
    durations = wav_durations_seconds(wav_blobs)
    starts: list[float] = []
    cursor = 0.0
    for d in durations:
        starts.append(cursor)
        cursor += d
    total_seconds = cursor
    chapters = [
        (sec.heading, starts[i]) for i, sec in enumerate(doc.sections)
    ]

    # ---- step 4: concat WAVs → MP3 + chapter markers ------------------------
    log.info("stitching %d WAVs (≈ %.1fs total audio)", len(wav_blobs), total_seconds)
    full_wav = concat_wavs(wav_blobs)
    mp3 = encode_wav_to_mp3(full_wav, bitrate=cfg.mp3_bitrate)
    mp3_with_chaps = embed_chapters(mp3, title=doc.title, chapters=chapters)

    # ---- step 5: write MP3 + sidecar metadata to audio_dir ------------------
    cfg.audio_dir.mkdir(parents=True, exist_ok=True)
    out_name = f"{int(time.time())}-{job_slug}.mp3"
    out_path = (cfg.audio_dir / out_name).resolve()
    out_path.write_bytes(mp3_with_chaps)

    log.info(
        "done in %.1fs → %s (%.0f KB, %.1fs audio, %d chapters)",
        time.monotonic() - overall_start,
        out_path,
        len(mp3_with_chaps) / 1024,
        total_seconds,
        len(chapters),
    )

    if not cfg.keep_work_dir:
        # Best-effort cleanup; ignore errors.
        import shutil as _sh
        try:
            _sh.rmtree(job_work_dir)
        except Exception:
            log.debug("could not clean work dir %s", job_work_dir, exc_info=True)

    return out_path


def _slugify(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")[:60]
