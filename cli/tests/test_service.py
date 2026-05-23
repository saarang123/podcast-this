"""Smoke tests for PodcastService — the things we can test without spinning
up Spindle / claude / ffmpeg.

End-to-end is exercised by ``podcast gen`` against a real Spindle deploy.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from podcast.models import Episode, JobStatus
from podcast import episode_store, feed as feed_module, job_store
from podcast.service import PodcastService, ServiceSettings


def test_list_sources_walks_source_roots(tmp_path: Path) -> None:
    root = tmp_path / "sources"
    root.mkdir()
    (root / "alpha.md").write_text("# Alpha\n\nbody")
    (root / "subdir").mkdir()
    (root / "subdir" / "beta.md").write_text("# Beta\n\nbody")
    # not a markdown file
    (root / "readme.txt").write_text("ignore me")

    svc = PodcastService(
        ServiceSettings(
            source_roots=[root],
            audio_dir=tmp_path / "audio",
            feed_dir=tmp_path / "feed",
            jobs_dir=tmp_path / "jobs",
        )
    )

    sources = svc.list_sources()
    titles = sorted(s.title for s in sources)
    assert titles == ["Alpha", "Beta"]
    assert all(s.kind == "markdown" for s in sources)
    assert all(s.source_uri.endswith(".md") for s in sources)


def test_list_episodes_reads_sidecars(tmp_path: Path) -> None:
    audio = tmp_path / "audio"
    audio.mkdir()
    episode_store.write(
        audio,
        Episode(
            episode_id="abc",
            title="Test",
            status="published",
            source_uri="/path/to/source.md",
            created_at="2026-05-22T20:00:00+00:00",
            duration_s=60,
            audio_url="http://example/ep-abc.mp3",
        ),
    )
    svc = PodcastService(ServiceSettings(audio_dir=audio, feed_dir=tmp_path / "feed", jobs_dir=tmp_path / "jobs"))
    episodes = svc.list_episodes()
    assert len(episodes) == 1
    assert episodes[0].title == "Test"
    assert episodes[0].status == "published"


def test_get_job_raises_for_unknown(tmp_path: Path) -> None:
    svc = PodcastService(
        ServiceSettings(
            audio_dir=tmp_path / "audio",
            feed_dir=tmp_path / "feed",
            jobs_dir=tmp_path / "jobs",
        )
    )
    with pytest.raises(KeyError):
        svc.get_job("not-a-real-id")


def test_job_status_round_trips_through_disk(tmp_path: Path) -> None:
    jobs = tmp_path / "jobs"
    status = JobStatus(
        job_id="j-1",
        status="running",
        phase="rewriting",
        progress=0.4,
        message="halfway",
        episode_id="ep-1",
    )
    job_store.write(jobs, status)
    got = job_store.read(jobs, "j-1")
    assert got == status


def test_feed_xml_only_includes_published(tmp_path: Path) -> None:
    feed_dir = tmp_path / "feed"
    episodes = [
        Episode(
            episode_id="published-1",
            title="Done",
            status="published",
            source_uri="/x.md",
            created_at="2026-05-22T20:00:00+00:00",
            duration_s=60,
            audio_url="http://example/ep-published-1.mp3",
        ),
        Episode(
            episode_id="in-flight",
            title="Generating",
            status="generating",
            source_uri="/y.md",
            created_at="2026-05-22T20:05:00+00:00",
        ),
    ]
    feed_path, json_path = feed_module.write(feed_dir, episodes)
    feed_text = feed_path.read_text()
    assert "published-1" in feed_text
    assert "in-flight" not in feed_text  # only published shows in RSS

    # but episodes.json includes both
    import json

    parsed = json.loads(json_path.read_text())
    assert len(parsed) == 2
    ids = {e["episode_id"] for e in parsed}
    assert ids == {"published-1", "in-flight"}


def test_generate_episode_returns_immediately(tmp_path: Path) -> None:
    """``generate_episode`` should return a JobRef without waiting for the
    pipeline to complete. The pipeline runs in a background thread; we
    don't drive it to completion in this test (would need Spindle + claude
    + ffmpeg)."""
    md = tmp_path / "src.md"
    md.write_text("# Tiny\n\n## Section A\n\nbody")
    svc = PodcastService(
        ServiceSettings(
            audio_dir=tmp_path / "audio",
            feed_dir=tmp_path / "feed",
            jobs_dir=tmp_path / "jobs",
            work_dir=tmp_path / "work",
            # Use a binary that doesn't exist so the rewrite step fails
            # quickly. We only care that generate_episode returns quickly
            # here, not that the pipeline succeeds.
            rewrite_cli_binary="absolutely-not-a-real-binary",
        )
    )
    try:
        t0 = time.monotonic()
        job_ref = svc.generate_episode(str(md))
        elapsed = time.monotonic() - t0
        assert elapsed < 0.5, f"generate_episode took {elapsed:.2f}s — should be ~instant"
        assert job_ref.status == "queued"
        assert job_ref.job_id
        # job_id should be retrievable
        status = svc.get_job(job_ref.job_id)
        assert status.job_id == job_ref.job_id
    finally:
        svc.close()
