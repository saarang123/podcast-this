"""Public data models for the PodcastService API surface.

Frozen dataclasses so they're hashable + immutable; Bridge can serialize
them straight to JSON for REST / MCP without worrying about mutation.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SourceDoc:
    """Something we can turn into an episode."""

    title: str
    source_uri: str
    kind: str  # "markdown" | "html" | "pdf" | "url" | "docx" | ...
    updated_at: str | None = None  # ISO-8601


@dataclass(frozen=True)
class JobRef:
    """What ``generate_episode`` returns immediately."""

    job_id: str
    status: str  # "queued" | "running" | "complete" | "failed"
    activity_id: str | None = None  # reserved for future correlation w/ Bridge


@dataclass(frozen=True)
class JobStatus:
    """Full status of one generation job — what ``get_job`` returns."""

    job_id: str
    status: str  # "queued" | "running" | "complete" | "failed"
    phase: str  # "loading" | "rewriting" | "tts" | "stitching" | "publishing"
    progress: float | None  # 0.0–1.0 if known
    message: str = ""
    episode_id: str | None = None
    audio_url: str | None = None
    feed_url: str | None = None


@dataclass(frozen=True)
class Episode:
    """A produced (or in-flight) episode — what ``list_episodes`` returns."""

    episode_id: str
    title: str
    status: str  # "generating" | "published" | "failed"
    source_uri: str
    created_at: str  # ISO-8601
    duration_s: int | None = None
    audio_url: str | None = None
    feed_url: str | None = None
