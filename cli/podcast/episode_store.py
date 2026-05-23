"""Per-episode metadata persisted alongside each MP3.

For every produced (or generating) episode we write a JSON sidecar next to
the MP3 file under ``audio/``. ``list_episodes`` walks this directory and
returns the parsed sidecars sorted by ``created_at`` desc.

Schema mirrors ``Episode`` exactly; on read we tolerate missing optional
fields (forward-compat with older sidecars).
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path

from .models import Episode


def write(audio_dir: Path, episode: Episode) -> Path:
    audio_dir.mkdir(parents=True, exist_ok=True)
    target = audio_dir / f"ep-{episode.episode_id}.json"
    fd, tmp_path = tempfile.mkstemp(
        suffix=".json", prefix=f".ep-{episode.episode_id}.", dir=audio_dir
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(asdict(episode), f, indent=2)
        os.replace(tmp_path, target)
        return target
    except Exception:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


def read(audio_dir: Path, episode_id: str) -> Episode | None:
    target = audio_dir / f"ep-{episode_id}.json"
    if not target.exists():
        return None
    return _from_dict(json.loads(target.read_text(encoding="utf-8")))


def read_all(audio_dir: Path) -> list[Episode]:
    if not audio_dir.exists():
        return []
    episodes: list[Episode] = []
    for path in audio_dir.glob("ep-*.json"):
        try:
            episodes.append(_from_dict(json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, json.JSONDecodeError, TypeError):
            # Skip corrupt / partial sidecars.
            continue
    episodes.sort(key=lambda e: e.created_at, reverse=True)
    return episodes


def _from_dict(d: dict) -> Episode:
    # Forward-compat: accept extra keys, fill missing optional fields.
    known = {
        "episode_id", "title", "status", "source_uri", "created_at",
        "duration_s", "audio_url", "feed_url",
    }
    filtered = {k: v for k, v in d.items() if k in known}
    return Episode(**filtered)
