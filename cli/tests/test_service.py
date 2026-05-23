"""Smoke tests for PodcastService.

The store tests need a running MongoDB (matches Spindle's pattern). They
skip with a clear message if Mongo isn't reachable at SPINDLE_TEST_MONGO_URL
(default ``mongodb://localhost:27017``). The renderer / list_sources tests
don't need Mongo at all.

Network deps (Spindle, claude, ffmpeg) are still exercised by ``podcast
gen`` against the live stack, not here.
"""
from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest
from pymongo import MongoClient
from pymongo.errors import ServerSelectionTimeoutError

from podcast import episode_store, feed as feed_module, job_store
from podcast.models import Episode, JobStatus


_TEST_MONGO_URL = os.environ.get(
    "SPINDLE_TEST_MONGO_URL", "mongodb://localhost:27017"
)


@pytest.fixture
def mongo_db():
    db_name = f"podcast_test_{uuid4().hex[:10]}"
    client = MongoClient(_TEST_MONGO_URL, serverSelectionTimeoutMS=500)
    try:
        client.admin.command("ping")
    except ServerSelectionTimeoutError:
        pytest.skip(f"MongoDB not reachable at {_TEST_MONGO_URL}")
    db = client[db_name]
    yield db
    client.drop_database(db_name)
    client.close()


# ─── list_sources is a pure filesystem walk; no Mongo ─────────────────


def test_source_doc_from_markdown(tmp_path: Path) -> None:
    from podcast.service import _source_doc_from_markdown

    root = tmp_path / "sources"
    root.mkdir()
    (root / "alpha.md").write_text("# Alpha\n\nbody")
    (root / "subdir").mkdir()
    (root / "subdir" / "beta.md").write_text("# Beta\n\nbody")
    (root / "readme.txt").write_text("ignore me")

    sources = sorted(
        (_source_doc_from_markdown(p) for p in root.rglob("*.md")),
        key=lambda s: s.title,
    )
    titles = [s.title for s in sources]
    assert titles == ["Alpha", "Beta"]
    assert all(s.kind == "markdown" for s in sources)


# ─── Mongo-backed stores ──────────────────────────────────────────────


def test_job_status_round_trips_through_mongo(mongo_db) -> None:
    status = JobStatus(
        job_id="j-1",
        status="running",
        phase="rewriting",
        progress=0.4,
        message="halfway",
        episode_id="ep-1",
    )
    job_store.write(mongo_db, status)
    got = job_store.read(mongo_db, "j-1")
    assert got == status

    assert job_store.read(mongo_db, "nonexistent") is None


def test_episode_round_trips_with_artifact_key(mongo_db) -> None:
    ep = Episode(
        episode_id="abc",
        title="Test",
        status="published",
        source_uri="/x.md",
        created_at="2026-05-23T03:00:00+00:00",
        duration_s=60,
    )
    episode_store.write(mongo_db, ep, artifact_key="ep-abc.mp3")
    got = episode_store.read(mongo_db, "abc")
    assert got is not None
    got_ep, got_key = got
    assert got_ep.title == "Test"
    assert got_key == "ep-abc.mp3"


def test_read_all_sorts_newest_first(mongo_db) -> None:
    older = Episode(
        episode_id="old",
        title="Older",
        status="published",
        source_uri="/x.md",
        created_at="2026-05-22T01:00:00+00:00",
    )
    newer = Episode(
        episode_id="new",
        title="Newer",
        status="published",
        source_uri="/y.md",
        created_at="2026-05-23T01:00:00+00:00",
    )
    episode_store.write(mongo_db, older, artifact_key="ep-old.mp3")
    episode_store.write(mongo_db, newer, artifact_key="ep-new.mp3")
    rows = episode_store.read_all(mongo_db)
    titles = [ep.title for ep, _ in rows]
    assert titles == ["Newer", "Older"]


# ─── feed renderer is pure, no Mongo ─────────────────────────────────


def test_feed_xml_only_includes_published() -> None:
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
    feed_xml, episodes_json = feed_module.render(episodes)
    assert "published-1" in feed_xml
    assert "in-flight" not in feed_xml

    import json
    parsed = json.loads(episodes_json)
    assert {e["episode_id"] for e in parsed} == {"published-1", "in-flight"}
