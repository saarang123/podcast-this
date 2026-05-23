"""Source-agnostic IR.

Every source adapter (markdown, html, pdf, ...) loads its input into the same
``Document`` shape so the downstream pipeline (rewrite → TTS → stitch) doesn't
care where the text came from.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Section:
    """One unit of content. H2-shaped for markdown; analogous splits for
    other formats."""

    heading: str
    """The section title as it should be announced in narration / written
    into the chapter marker."""

    content: str
    """The raw source text under this heading (markdown body, HTML inner,
    etc.). The rewriter sees this verbatim plus the rewrite prompt."""

    depth: int = 2
    """1 for H1, 2 for H2, etc. v0 only emits depth=2 sections; v1 may
    surface depth=3 for sub-chapter markers."""


@dataclass
class Document:
    title: str
    source_uri: str
    sections: list[Section] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


def load(uri: str | Path) -> Document:
    """Dispatch URI → matching source adapter.

    v0: only ``.md`` files; just imports and runs the markdown adapter.
    Add HTML / PDF / docx adapters as siblings later.
    """
    uri_str = str(uri)

    if uri_str.startswith(("http://", "https://")):
        raise NotImplementedError(
            "HTML / URL sources not implemented yet — only .md files"
        )
    if uri_str.endswith(".pdf"):
        raise NotImplementedError(
            "PDF sources not implemented yet — only .md files"
        )
    if uri_str.endswith((".md", ".markdown")):
        from .markdown import load_markdown

        return load_markdown(Path(uri_str))

    raise ValueError(f"Unrecognized source URI: {uri_str}")
