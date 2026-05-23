"""Per-job status persisted to MongoDB.

Collection: ``{mongo_db}.jobs`` — one document per job, ``_id`` is the
``job_id`` string. Schema mirrors the ``JobStatus`` dataclass exactly.

Sync API (pymongo) because the calling pipeline runs in a background
thread and Mongo writes are sub-millisecond — no benefit from an async
client.

Indexed on ``status`` so ``list_jobs(status=...)`` is cheap if we ever add
that to the public API.
"""
from __future__ import annotations

from dataclasses import asdict

from pymongo.collection import Collection
from pymongo.database import Database

from .models import JobStatus


_COLLECTION = "jobs"


def ensure_indexes(db: Database) -> None:
    """Idempotent. Call once at PodcastService init."""
    coll = db[_COLLECTION]
    coll.create_index("status")
    coll.create_index("episode_id")


def _coll(db: Database) -> Collection:
    return db[_COLLECTION]


def write(db: Database, status: JobStatus) -> None:
    doc = asdict(status)
    doc["_id"] = status.job_id
    _coll(db).replace_one({"_id": status.job_id}, doc, upsert=True)


def read(db: Database, job_id: str) -> JobStatus | None:
    doc = _coll(db).find_one({"_id": job_id})
    if doc is None:
        return None
    doc.pop("_id", None)
    return JobStatus(**doc)
