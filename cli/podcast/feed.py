"""RSS 2.0 feed generation from the on-disk episode store.

Regenerated whenever an episode publishes; written to
``feed/feed.xml`` + ``feed/episodes.json``.

Two outputs:
  - ``feed.xml`` — what Overcast / Apple Podcasts / etc. subscribe to.
    RSS 2.0 + iTunes namespace (the de-facto podcast standard).
  - ``episodes.json`` — machine-readable list of the same data, for
    Bridge or any other programmatic consumer.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

from .models import Episode


def write(
    feed_dir: Path,
    episodes: list[Episode],
    *,
    podcast_title: str = "Podcast This",
    podcast_description: str = "Auto-generated narrations of technical documents.",
    podcast_author: str = "podcast-this",
    podcast_link: str = "http://localhost",
) -> tuple[Path, Path]:
    """Write feed.xml + episodes.json from the given episode list.

    Returns (feed_xml_path, episodes_json_path).
    """
    feed_dir.mkdir(parents=True, exist_ok=True)

    # Only published episodes go into the RSS feed (Overcast doesn't want
    # half-finished episodes), but episodes.json includes everything so
    # Bridge can show in-flight ones too.
    published = [e for e in episodes if e.status == "published"]

    feed_xml = _build_rss(
        published,
        podcast_title=podcast_title,
        podcast_description=podcast_description,
        podcast_author=podcast_author,
        podcast_link=podcast_link,
    )
    feed_path = _atomic_write(feed_dir / "feed.xml", feed_xml)

    episodes_json = json.dumps(
        [asdict(e) for e in episodes],
        indent=2,
    )
    json_path = _atomic_write(feed_dir / "episodes.json", episodes_json)

    return feed_path, json_path


def _build_rss(
    episodes: list[Episode],
    *,
    podcast_title: str,
    podcast_description: str,
    podcast_author: str,
    podcast_link: str,
) -> str:
    import datetime as _dt

    now = _dt.datetime.now(_dt.UTC)
    last_build = format_datetime(now)

    items_xml = "\n".join(_build_item(e) for e in episodes)

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
  xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
  xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>{xml_escape(podcast_title)}</title>
    <link>{xml_escape(podcast_link)}</link>
    <description>{xml_escape(podcast_description)}</description>
    <language>en-us</language>
    <itunes:author>{xml_escape(podcast_author)}</itunes:author>
    <itunes:explicit>false</itunes:explicit>
    <itunes:category text="Technology"/>
    <lastBuildDate>{last_build}</lastBuildDate>
{items_xml}
  </channel>
</rss>
"""


def _build_item(e: Episode) -> str:
    import datetime as _dt

    pub_date = _to_rfc2822(e.created_at)
    audio_url = e.audio_url or ""
    duration_str = (
        _format_itunes_duration(e.duration_s)
        if e.duration_s is not None
        else ""
    )
    return f"""    <item>
      <title>{xml_escape(e.title)}</title>
      <guid isPermaLink="false">{xml_escape(e.episode_id)}</guid>
      <pubDate>{pub_date}</pubDate>
      <enclosure url="{xml_escape(audio_url)}" type="audio/mpeg" />
      <itunes:duration>{duration_str}</itunes:duration>
      <description>{xml_escape(f"Source: {e.source_uri}")}</description>
    </item>"""


def _to_rfc2822(iso_string: str) -> str:
    import datetime as _dt

    try:
        # Python 3.11+ handles trailing Z
        dt = _dt.datetime.fromisoformat(iso_string.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.UTC)
        return format_datetime(dt)
    except ValueError:
        return format_datetime(_dt.datetime.now(_dt.UTC))


def _format_itunes_duration(seconds: int) -> str:
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _atomic_write(target: Path, content: str) -> Path:
    fd, tmp_path = tempfile.mkstemp(
        suffix=target.suffix,
        prefix=f".{target.name}.",
        dir=target.parent,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, target)
        return target
    except Exception:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise
