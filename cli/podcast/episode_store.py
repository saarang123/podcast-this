"""Per-episode metadata persisted to MongoDB.

Collection: ``{mongo_db}.episodes`` — one document per episode, ``_id`` is
the ``episode_id`` string. Schema mirrors ``Episode`` plus an opaque
``artifact_key`` field pointing at the MP3 object in MinIO (the
``audio_url`` is computed at read time so changing audio_url_base later
doesn't require a backfill).

Sync API (pymongo). Same rationale as job_store: pipeline thread, fast
writes.
"""
from __future__ import annotations

from dataclasses import asdict, replace

from pymongo.collection import Collection
from pymongo.database import Database
from pymongo import DESCENDING

from .models import Episode


_COLLECTION = "episodes"


def ensure_indexes(db: Database) -> None:
    coll = db[_COLLECTION]
    coll.create_index([("created_at", DESCENDING)])
    coll.create_index("status")
    coll.create_index("source_uri")


def _coll(db: Database) -> Collection:
    return db[_COLLECTION]


def write(
    db: Database,
    episode: Episode,
    *,
    artifact_key: str | None = None,
) -> None:
    """Upsert an episode. ``artifact_key`` is stored alongside the rest so
    ``read_all`` can rehydrate ``audio_url`` against the current settings."""
    doc = asdict(episode)
    doc["_id"] = episode.episode_id
    if artifact_key is not None:
        doc["artifact_key"] = artifact_key
    _coll(db).replace_one({"_id": episode.episode_id}, doc, upsert=True)


def read(db: Database, episode_id: str) -> tuple[Episode, str | None] | None:
    """Returns (episode, artifact_key) or None."""
    doc = _coll(db).find_one({"_id": episode_id})
    if doc is None:
        return None
    artifact_key = doc.pop("artifact_key", None)
    doc.pop("_id", None)
    return _from_dict(doc), artifact_key


def read_all(db: Database) -> list[tuple[Episode, str | None]]:
    """Newest first. Each entry is (episode, artifact_key)."""
    out: list[tuple[Episode, str | None]] = []
    for doc in _coll(db).find({}).sort("created_at", DESCENDING):
        artifact_key = doc.pop("artifact_key", None)
        doc.pop("_id", None)
        try:
            out.append((_from_dict(doc), artifact_key))
        except TypeError:
            # tolerate forward-compat unknown fields
            continue
    return out


def _from_dict(d: dict) -> Episode:
    known = {
        "episode_id", "title", "status", "source_uri", "created_at",
        "duration_s", "audio_url", "feed_url",
    }
    return Episode(**{k: v for k, v in d.items() if k in known})
