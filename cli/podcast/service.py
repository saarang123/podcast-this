"""``PodcastService`` — the public Python API for Bridge (or any caller).

Spec:
    list_sources()                              → list[SourceDoc]
    generate_episode(source_uri)                → JobRef  (non-blocking)
    list_episodes()                             → list[Episode]
    get_job(job_id)                             → JobStatus

State lives in MongoDB (``podcast_this.jobs`` + ``podcast_this.episodes``).
MP3 bytes live in MinIO bucket ``podcast-episodes``. Nothing podcast-related
is stored on local disk anymore — both stores survive process restarts and
are queryable from multiple processes simultaneously.

``generate_episode`` returns immediately; the heavy work (claude rewrites,
Spindle TTS, ffmpeg stitch, MinIO upload) runs in a ThreadPoolExecutor
worker thread.
"""
from __future__ import annotations

import asyncio
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pymongo import MongoClient

from . import episode_store, job_store
from .artifact_store import MinIOArtifactStore
from .models import Episode, JobRef, JobStatus, SourceDoc
from .pipeline import PipelineConfig, generate_podcast
from .sources.markdown import load_markdown

log = logging.getLogger(__name__)


@dataclass
class ServiceSettings:
    """How a PodcastService is configured."""

    # Directories to walk for ``list_sources``. Each entry is recursively
    # scanned for ``*.md`` files.
    source_roots: list[Path] = field(default_factory=list)

    # Per-run scratch directory (rewrite IO, intermediate stitch wav, etc.).
    # Cleared after each successful generate_episode unless ``keep_work_dir``.
    work_dir: Path = Path("./work")

    # MongoDB
    mongo_url: str = "mongodb://localhost:27017"
    mongo_db: str = "podcast_this"

    # MinIO / S3 for the produced MP3 bytes
    s3_endpoint: str = "http://localhost:9000"
    s3_bucket: str = "podcast-episodes"
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_region: str = "us-east-1"

    # If set, used as the audio_url base instead of building it from
    # ``s3_endpoint`` / ``s3_bucket``. Useful when Caddy fronts MinIO under
    # a different hostname (e.g. ``http://homeserver.tailnet.ts.net/audio``).
    audio_url_base_override: str | None = None

    # URL where Caddy / MinIO serves the regenerated feed.xml.
    feed_url: str = "http://localhost:9000/podcast-episodes/feed.xml"

    # Spindle API to submit TTS jobs to.
    spindle_url: str = "http://localhost:8080"
    spindle_auth_token: str | None = None

    # Default TTS config_id.
    tts_config_id: str = "audio-tts-openai-v1"
    tts_voice: str | None = None

    # Pipeline concurrency caps.
    rewrite_concurrency: int = 5
    tts_concurrency: int = 5

    # CLI binary for the rewrite subprocess.
    rewrite_cli_binary: str = "claude"

    # MP3 bitrate.
    mp3_bitrate: str = "64k"

    # Podcast metadata (used by feed.xml).
    podcast_title: str = "Podcast This"
    podcast_description: str = "Auto-generated narrations of technical documents."
    podcast_author: str = "podcast-this"


class PodcastService:
    """Public API. Instantiate once per host process. Background pipeline
    runs in a ThreadPoolExecutor that lives until ``close()``."""

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

        self._mongo = MongoClient(self.settings.mongo_url)
        self._db = self._mongo[self.settings.mongo_db]
        job_store.ensure_indexes(self._db)
        episode_store.ensure_indexes(self._db)

        self._artifacts = MinIOArtifactStore(
            endpoint_url=self.settings.s3_endpoint,
            bucket=self.settings.s3_bucket,
            access_key=self.settings.s3_access_key,
            secret_key=self.settings.s3_secret_key,
            region=self.settings.s3_region,
        )
        self._artifacts.ensure_bucket()

        Path(self.settings.work_dir).mkdir(parents=True, exist_ok=True)

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

        self._executor.submit(
            self._run_pipeline_sync, job_id, episode_id, source_uri
        )
        return JobRef(job_id=job_id, status="queued")

    def list_episodes(self) -> list[Episode]:
        out: list[Episode] = []
        for ep, key in episode_store.read_all(self._db):
            audio_url = self._audio_url_for(key) if key else None
            out.append(replace(ep, audio_url=audio_url))
        return out

    def get_job(self, job_id: str) -> JobStatus:
        with self._jobs_lock:
            cached = self._jobs.get(job_id)
        if cached is not None:
            return cached
        on_disk = job_store.read(self._db, job_id)
        if on_disk is None:
            raise KeyError(f"unknown job_id: {job_id}")
        return on_disk

    def close(self) -> None:
        """Stop accepting new generations; let in-flight jobs finish."""
        self._executor.shutdown(wait=False, cancel_futures=False)
        try:
            self._mongo.close()
        except Exception:
            pass

    # ─── background execution ────────────────────────────────────────

    def _run_pipeline_sync(
        self, job_id: str, episode_id: str, source_uri: str
    ) -> None:
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

        # Drop a "generating" episode record so list_episodes shows in-flight
        # work immediately (audio_url is None until publish).
        generating_episode = Episode(
            episode_id=episode_id,
            title=title,
            status="generating",
            source_uri=str(source_path),
            created_at=created_at,
        )
        episode_store.write(self._db, generating_episode)

        self._update_status(
            job_id, phase="rewriting", progress=0.1, episode_id=episode_id
        )

        # Pipeline writes the MP3 to a temp path under work_dir; we then
        # upload to MinIO and delete the local file.
        cfg = PipelineConfig(
            spindle_url=self.settings.spindle_url,
            spindle_auth_token=self.settings.spindle_auth_token,
            tts_config_id=self.settings.tts_config_id,
            tts_voice=self.settings.tts_voice,
            audio_dir=Path(self.settings.work_dir) / "mp3-tmp",
            work_dir=Path(self.settings.work_dir),
            mp3_bitrate=self.settings.mp3_bitrate,
            rewrite_concurrency=self.settings.rewrite_concurrency,
            tts_concurrency=self.settings.tts_concurrency,
            cli_binary=self.settings.rewrite_cli_binary,
        )

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
                self._db, replace(generating_episode, status="failed")
            )
            return

        # Read MP3 bytes, upload to MinIO under a stable key, delete temp.
        mp3_bytes = mp3_path.read_bytes()
        try:
            mp3_path.unlink(missing_ok=True)
        except OSError:
            pass

        artifact_key = f"ep-{episode_id}.mp3"
        self._update_status(job_id, phase="publishing", progress=0.95)
        self._artifacts.put_mp3(
            artifact_key,
            mp3_bytes,
            metadata={
                "episode_id": episode_id,
                "title": title[:200],  # S3 metadata is byte-limited
                "source_uri": str(source_path)[:1000],
            },
        )

        duration_s = _peek_mp3_duration(mp3_bytes)
        audio_url = self._audio_url_for(artifact_key)

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
        episode_store.write(self._db, published_episode, artifact_key=artifact_key)

        # Rebuild feed.xml + episodes.json from the current episode list,
        # upload both to MinIO so Caddy / Overcast pick them up.
        self._republish_feed()

        self._update_status(
            job_id,
            status="complete",
            phase="publishing",
            progress=1.0,
            message=f"published {artifact_key}",
            episode_id=episode_id,
            audio_url=audio_url,
            feed_url=self.settings.feed_url,
        )

    # ─── status helpers ──────────────────────────────────────────────

    def _save_status(self, status: JobStatus) -> None:
        with self._jobs_lock:
            self._jobs[status.job_id] = status
        job_store.write(self._db, status)

    def _update_status(self, job_id: str, **changes) -> None:
        with self._jobs_lock:
            current = self._jobs.get(job_id)
        if current is None:
            current = job_store.read(self._db, job_id)
        if current is None:
            log.warning("update_status for unknown job_id=%s", job_id)
            return
        new_status = replace(current, **changes)
        self._save_status(new_status)

    # ─── URL building + feed regeneration ─────────────────────────────

    def _audio_url_for(self, artifact_key: str) -> str:
        if self.settings.audio_url_base_override:
            base = self.settings.audio_url_base_override.rstrip("/")
            return f"{base}/{artifact_key}"
        return self._artifacts.url_for(artifact_key)

    def _republish_feed(self) -> None:
        from . import feed as feed_module

        episodes = self.list_episodes()
        feed_xml, episodes_json = feed_module.render(
            episodes,
            podcast_title=self.settings.podcast_title,
            podcast_description=self.settings.podcast_description,
            podcast_author=self.settings.podcast_author,
            podcast_link=self.settings.audio_url_base_override
            or self.settings.s3_endpoint,
        )
        self._artifacts.put_text("feed.xml", feed_xml, content_type="application/rss+xml")
        self._artifacts.put_text(
            "episodes.json", episodes_json, content_type="application/json"
        )


# ─── helpers ─────────────────────────────────────────────────────────


def _source_doc_from_markdown(path: Path) -> SourceDoc:
    title = _first_h1(path) or path.stem.replace("-", " ").title()
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat()
    except OSError:
        mtime = None
    return SourceDoc(
        title=title, source_uri=str(path), kind="markdown", updated_at=mtime
    )


def _first_h1(path: Path) -> str | None:
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("# "):
                    return line[2:].strip()
                if line.strip():
                    break
    except OSError:
        return None
    return None


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(s: str) -> str:
    return _SLUG_RE.sub("-", s.lower()).strip("-")[:60]


def _peek_mp3_duration(mp3_bytes: bytes) -> int | None:
    """Read duration without writing to disk. mutagen handles bytes via
    a tiny BytesIO wrapper, but the simplest correct call is via a
    temp file — same outcome, simpler code path."""
    import tempfile

    try:
        from mutagen.mp3 import MP3

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            f.write(mp3_bytes)
            tmp = Path(f.name)
        try:
            return int(MP3(str(tmp)).info.length)
        finally:
            tmp.unlink(missing_ok=True)
    except Exception:
        return None
