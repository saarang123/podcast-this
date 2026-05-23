"""Markdown source adapter.

Loads a ``.md`` file into the source-agnostic ``Document`` IR. Splits by
``##`` (H2) headings — one section per H2. Content before the first H2 is
kept on a synthetic "intro" section if non-empty (otherwise dropped).
"""
from __future__ import annotations

import re
from pathlib import Path

from .base import Document, Section


_H1_RE = re.compile(r"^# (.+)$", re.MULTILINE)
_H2_SPLIT_RE = re.compile(r"^## ", re.MULTILINE)


def load_markdown(path: Path) -> Document:
    text = path.read_text(encoding="utf-8")

    # Title = first H1 line, else fall back to the filename stem.
    title_match = _H1_RE.search(text)
    title = title_match.group(1).strip() if title_match else path.stem.replace("-", " ").title()

    parts = _H2_SPLIT_RE.split(text)
    intro = parts[0]
    h2_parts = parts[1:]

    sections: list[Section] = []

    intro_body = _strip_h1_block(intro).strip()
    if intro_body:
        sections.append(
            Section(heading="Introduction", content=intro_body, depth=2)
        )

    for part in h2_parts:
        lines = part.split("\n", 1)
        heading = lines[0].strip()
        body = lines[1].strip() if len(lines) > 1 else ""
        if not body:
            # Skip empty H2 (just a heading line, no content yet).
            continue
        sections.append(Section(heading=heading, content=body, depth=2))

    return Document(
        title=title,
        source_uri=str(path),
        sections=sections,
        metadata={"format": "markdown"},
    )


def _strip_h1_block(text: str) -> str:
    """Drop the H1 line and any single ``> blockquote`` line directly under
    it (these are the doc's title + style metadata; not narration material)."""
    lines = text.splitlines()
    out: list[str] = []
    skipping_blockquotes = False
    skipped_h1 = False
    for line in lines:
        if not skipped_h1 and line.startswith("# "):
            skipped_h1 = True
            skipping_blockquotes = True
            continue
        if skipping_blockquotes and (line.startswith(">") or not line.strip()):
            continue
        skipping_blockquotes = False
        out.append(line)
    return "\n".join(out)
