"""Per-job status persisted to disk.

Keeps an on-disk record of each ``generate_episode`` job so:
  - status survives process restarts
  - Bridge can call ``get_job`` from a different worker / request than the
    one that started the job
  - in-flight jobs are recoverable when the process boots back up (v0
    treats interrupted jobs as failed — Spindle's job state is still in
    Mongo, but the orchestrator's `await` is lost on restart)

One file per job at ``{jobs_dir}/{job_id}.json``. Reads + writes are
atomic via temp-file + rename.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path

from .models import JobStatus


def write(jobs_dir: Path, status: JobStatus) -> None:
    jobs_dir.mkdir(parents=True, exist_ok=True)
    target = jobs_dir / f"{status.job_id}.json"
    # atomic write via NamedTemporaryFile + os.replace
    fd, tmp_path = tempfile.mkstemp(
        suffix=".json", prefix=f".{status.job_id}.", dir=jobs_dir
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(asdict(status), f, indent=2)
        os.replace(tmp_path, target)
    except Exception:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


def read(jobs_dir: Path, job_id: str) -> JobStatus | None:
    target = jobs_dir / f"{job_id}.json"
    if not target.exists():
        return None
    data = json.loads(target.read_text(encoding="utf-8"))
    return JobStatus(**data)
