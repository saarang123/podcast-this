"""``PodcastService`` — the public Python API for Bridge (or any caller).

Spec:
    list_sources()                              → list[SourceDoc]
    generate_episode(source_uri)                → JobRef  (non-blocking)
    list_episodes()                             → list[Episode]
    get_job(job_id)                             → JobStatus

``generate_episode`` returns immediately. Heavy work (claude rewrites,
Spindle TTS, ffmpeg stitch) runs in a background thread with its own
asyncio event loop. Job state is mirrored to disk so multiple processes /
restarts can read the same view.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable
from uuid import uuid4

from . import episode_store, job_store
from . import feed as feed_module
from .models import Episode, JobRef, JobStatus, SourceDoc
from .pipeline import PipelineConfig, generate_podcast
from .sources.markdown import load_markdown

log = logging.getLogger(__name__)


@dataclass
class ServiceSettings:
    """How a PodcastService is configured. All paths default to local CWD."""

    # Directories to walk for ``list_sources``. Each entry is recursively
    # scanned for ``*.md`` files.
    source_roots: list[Path] = field(default_factory=list)

    # Where to write produced MP3s + their per-episode JSON sidecars.
    audio_dir: Path = Path("./audio")

    # Where to write feed.xml + episodes.json.
    feed_dir: Path = Path("./feed")

    # Where to keep per-job status JSON.
    jobs_dir: Path = Path("./jobs")

    # Per-run scratch directory (rewrite IO, etc.).
    work_dir: Path = Path("./work")

    # URL prefix Caddy / Bridge serves the audio under. The episode's
    # ``audio_url`` becomes ``{audio_url_base}/ep-<id>.mp3``.
    audio_url_base: str = "http://localhost:8000/audio"

    # URL Caddy serves the feed at.
    feed_url: str = "http://localhost:8000/feed/feed.xml"

    # Spindle API to submit TTS jobs to.
    spindle_url: str = "http://localhost:8080"
    spindle_auth_token: str | None = None

    # Default TTS config_id; callers can override via per-call options later.
    tts_config_id: str = "audio-tts-openai-v1"
    tts_voice: str | None = None

    # Concurrency caps inside the pipeline.
    rewrite_concurrency: int = 5
    tts_concurrency: int = 5

    # CLI binary name for rewrite subprocesses.
    rewrite_cli_binary: str = "claude"

    # Bitrate for MP3 encoding.
    mp3_bitrate: str = "64k"

    # Podcast feed metadata (used in feed.xml).
    podcast_title: str = "Podcast This"
    podcast_description: str = "Auto-generated narrations of technical documents."
    podcast_author: str = "podcast-this"


class PodcastService:
    """The Python API Bridge (or any caller) wraps.

    Instantiate once per host process. Background episode generation runs
    in a ThreadPoolExecutor that lives for the lifetime of the service.
    """

    def __init__(
        self,
        settings: ServiceSettings | None = None,
        *,
        max_concurrent_episodes: int = 2,
    ) -> None:
        self.settings = settings or ServiceSettings()
        self._executor = ThreadPoolExecutor(
            max_workers=max_concurrent_episodes,
            thread_name_prefix="podcast-gen",
        )
        self._jobs_lock = threading.Lock()
        self._jobs: dict[str, JobStatus] = {}

        # Make sure target dirs exist so callers don't trip on first call.
        for d in (
            self.settings.audio_dir,
            self.settings.feed_dir,
            self.settings.jobs_dir,
            self.settings.work_dir,
        ):
            Path(d).mkdir(parents=True, exist_ok=True)

    # ─── public API ──────────────────────────────────────────────────

    def list_sources(self) -> list[SourceDoc]:
        out: list[SourceDoc] = []
        for root in self.settings.source_roots:
            root_path = Path(root).expanduser()
            if not root_path.exists():
                continue
            for md in sorted(root_path.rglob("*.md")):
                out.append(_source_doc_from_markdown(md))
        return out

    def generate_episode(self, source_uri: str) -> JobRef:
        job_id = str(uuid4())
        episode_id = str(uuid4())
        status = JobStatus(
            job_id=job_id,
            status="queued",
            phase="loading",
            progress=0.0,
            message="",
            episode_id=episode_id,
        )
        self._save_status(status)

        # Submit to thread pool — `_run_pipeline_sync` boots its own
        # asyncio event loop so the caller doesn't have to be async.
        self._executor.submit(
            self._run_pipeline_sync, job_id, episode_id, source_uri
        )

        return JobRef(job_id=job_id, status="queued")

    def list_episodes(self) -> list[Episode]:
        return episode_store.read_all(Path(self.settings.audio_dir))

    def get_job(self, job_id: str) -> JobStatus:
        with self._jobs_lock:
            cached = self._jobs.get(job_id)
        if cached is not None:
            return cached
        on_disk = job_store.read(Path(self.settings.jobs_dir), job_id)
        if on_disk is None:
            raise KeyError(f"unknown job_id: {job_id}")
        return on_disk

    def close(self) -> None:
        """Shut the thread pool down. New ``generate_episode`` calls fail
        after this; in-flight jobs finish."""
        self._executor.shutdown(wait=False, cancel_futures=False)

    # ─── background execution ────────────────────────────────────────

    def _run_pipeline_sync(
        self, job_id: str, episode_id: str, source_uri: str
    ) -> None:
        """Run the async pipeline in this worker thread."""
        try:
            asyncio.run(self._run_pipeline(job_id, episode_id, source_uri))
        except Exception as e:
            log.exception("pipeline crashed for job %s", job_id)
            self._update_status(
                job_id,
                status="failed",
                phase="loading",
                message=f"unhandled error: {e}",
            )

    async def _run_pipeline(
        self, job_id: str, episode_id: str, source_uri: str
    ) -> None:
        log.info("generate_episode job_id=%s source=%s", job_id, source_uri)
        self._update_status(
            job_id, status="running", phase="loading", progress=0.05
        )

        # Pre-load the source so we can extract the title for the episode
        # record before the long-running rewrite/TTS work starts.
        try:
            source_path = Path(source_uri).expanduser()
            doc = load_markdown(source_path)
        except Exception as e:
            self._update_status(
                job_id, status="failed", phase="loading", message=str(e)
            )
            return

        title = doc.title
        created_at = datetime.now(UTC).isoformat()
        slug = _slugify(title) or episode_id

        # Drop a "generating" episode sidecar so list_episodes shows in-flight
        # work immediately.
        episode = Episode(
            episode_id=episode_id,
            title=title,
            status="generating",
            source_uri=str(source_path),
            created_at=created_at,
        )
        episode_store.write(Path(self.settings.audio_dir), episode)

        self._update_status(
            job_id, phase="rewriting", progress=0.1, episode_id=episode_id
        )

        cfg = PipelineConfig(
            spindle_url=self.settings.spindle_url,
            spindle_auth_token=self.settings.spindle_auth_token,
            tts_config_id=self.settings.tts_config_id,
            tts_voice=self.settings.tts_voice,
            audio_dir=Path(self.settings.audio_dir),
            work_dir=Path(self.settings.work_dir),
            mp3_bitrate=self.settings.mp3_bitrate,
            rewrite_concurrency=self.settings.rewrite_concurrency,
            tts_concurrency=self.settings.tts_concurrency,
            cli_binary=self.settings.rewrite_cli_binary,
        )

        # The current pipeline doesn't yet take a progress callback. We
        # update phases at the boundaries we can see from out here
        # (rewriting → tts → stitching → publishing) by reading the time
        # the pipeline call has been running and the on-disk artifacts it
        # writes. For v0 we update on coarse boundaries only.
        try:
            mp3_path = await asyncio.wait_for(
                generate_podcast(str(source_path), cfg),
                timeout=30 * 60,
            )
        except Exception as e:
            log.exception("pipeline failed for job %s", job_id)
            self._update_status(
                job_id, status="failed", phase="rewriting", message=str(e)
            )
            episode_store.write(
                Path(self.settings.audio_dir),
                replace(episode, status="failed"),
            )
            return

        # Move the MP3 into a stable, episode_id-keyed filename so the
        # audio_url is deterministic.
        stable_mp3 = Path(self.settings.audio_dir) / f"ep-{episode_id}.mp3"
        try:
            mp3_path.replace(stable_mp3)
        except OSError:
            # cross-device rename → fall back to copy
            stable_mp3.write_bytes(mp3_path.read_bytes())
            mp3_path.unlink(missing_ok=True)

        duration_s = _peek_mp3_duration(stable_mp3)
        audio_url = f"{self.settings.audio_url_base.rstrip('/')}/ep-{episode_id}.mp3"

        # Publish: update episode sidecar + regenerate feed
        self._update_status(
            job_id, phase="publishing", progress=0.95
        )
        published_episode = Episode(
            episode_id=episode_id,
            title=title,
            status="published",
            source_uri=str(source_path),
            created_at=created_at,
            duration_s=duration_s,
            audio_url=audio_url,
            feed_url=self.settings.feed_url,
        )
        episode_store.write(Path(self.settings.audio_dir), published_episode)
        feed_module.write(
            Path(self.settings.feed_dir),
            self.list_episodes(),
            podcast_title=self.settings.podcast_title,
            podcast_description=self.settings.podcast_description,
            podcast_author=self.settings.podcast_author,
            podcast_link=self.settings.audio_url_base,
        )

        self._update_status(
            job_id,
            status="complete",
            phase="publishing",
            progress=1.0,
            message=f"published {stable_mp3.name}",
            episode_id=episode_id,
            audio_url=audio_url,
            feed_url=self.settings.feed_url,
        )

    # ─── status helpers ──────────────────────────────────────────────

    def _save_status(self, status: JobStatus) -> None:
        with self._jobs_lock:
            self._jobs[status.job_id] = status
        job_store.write(Path(self.settings.jobs_dir), status)

    def _update_status(self, job_id: str, **changes) -> None:
        with self._jobs_lock:
            current = self._jobs.get(job_id)
        if current is None:
            current = job_store.read(Path(self.settings.jobs_dir), job_id)
        if current is None:
            log.warning("update_status for unknown job_id=%s", job_id)
            return
        new_status = replace(current, **changes)
        self._save_status(new_status)


# ─── helpers ─────────────────────────────────────────────────────────


def _source_doc_from_markdown(path: Path) -> SourceDoc:
    title = _first_h1(path) or path.stem.replace("-", " ").title()
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat()
    except OSError:
        mtime = None
    return SourceDoc(
        title=title,
        source_uri=str(path),
        kind="markdown",
        updated_at=mtime,
    )


def _first_h1(path: Path) -> str | None:
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("# "):
                    return line[2:].strip()
                if line.strip():
                    # Non-empty, non-heading first line → no H1 in this doc.
                    break
    except OSError:
        return None
    return None


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(s: str) -> str:
    return _SLUG_RE.sub("-", s.lower()).strip("-")[:60]


def _peek_mp3_duration(mp3_path: Path) -> int | None:
    """Best-effort duration in seconds; returns None if we can't read."""
    try:
        from mutagen.mp3 import MP3

        return int(MP3(str(mp3_path)).info.length)
    except Exception:
        return None
