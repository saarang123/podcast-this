"""Spawn ``claude -p`` as a subprocess to rewrite one section.

Pattern: hand Claude two file paths (input + output) in the prompt + the
load-bearing rewrite rules. Claude reads the input, writes the rewritten
narration to the output file. We read it back.

Why file-IO instead of stdout-parsing: Claude tends to wrap stdout responses
("Here's the rewrite:", code fences, etc.). Pinning the output target to a
file means we get exactly what was written, nothing else.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

log = logging.getLogger(__name__)


# Pulled in by the pipeline at startup so this module stays import-cheap.
DEFAULT_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent.parent / "prompts" / "rewrite-section.md"
)


def load_prompt(path: Path | None = None) -> str:
    p = path or DEFAULT_PROMPT_PATH
    return p.read_text(encoding="utf-8")


async def rewrite_section(
    section_text: str,
    section_idx: int,
    heading: str,
    rewrite_prompt: str,
    work_dir: Path,
    *,
    cli_binary: str = "claude",
    timeout_s: float = 180.0,
) -> str:
    """Spawn ``claude -p`` for one section. Returns the rewritten narration.

    Args:
        section_text: raw section body (markdown).
        section_idx: integer index — only used for logging + filenames.
        heading: section heading, included in the prompt to give Claude
            context about what the section is.
        rewrite_prompt: contents of ``prompts/rewrite-section.md``.
        work_dir: per-job temp directory. ``{idx:02d}-in.md`` and
            ``{idx:02d}-out.txt`` are written under here.
        cli_binary: override for testing (default ``"claude"``).
        timeout_s: kill the subprocess if it hasn't finished by this point.
    """
    if not shutil.which(cli_binary):
        raise RuntimeError(
            f"{cli_binary!r} not on PATH. Install Claude Code "
            f"(https://docs.anthropic.com/en/docs/claude-code) or pass a "
            f"different cli_binary."
        )

    work_dir.mkdir(parents=True, exist_ok=True)
    in_path = work_dir / f"{section_idx:02d}-in.md"
    out_path = work_dir / f"{section_idx:02d}-out.txt"
    in_path.write_text(section_text, encoding="utf-8")
    if out_path.exists():
        out_path.unlink()

    prompt = _build_prompt(
        rewrite_prompt=rewrite_prompt,
        heading=heading,
        in_path=in_path,
        out_path=out_path,
    )

    log.info(
        "rewrite section %d heading=%r chars_in=%d",
        section_idx, heading, len(section_text),
    )

    proc = await asyncio.create_subprocess_exec(
        cli_binary,
        "-p",
        prompt,
        # Non-interactive: bypass per-tool confirmation so Claude can Write
        # the output file without a human to approve. Blast radius is bounded
        # — each subprocess runs once and exits, only writes to the work_dir
        # path we hand it.
        "--permission-mode",
        "bypassPermissions",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_s
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(
            f"claude rewrite of section {section_idx} timed out after {timeout_s}s"
        )

    if proc.returncode != 0:
        raise RuntimeError(
            f"claude failed for section {section_idx} "
            f"(rc={proc.returncode}): {stderr.decode(errors='replace')[:500]}"
        )

    if not out_path.exists():
        raise RuntimeError(
            f"claude did not write {out_path} for section {section_idx}. "
            f"stdout tail: {stdout.decode(errors='replace')[-500:]}"
        )

    rewritten = out_path.read_text(encoding="utf-8").strip()
    if not rewritten:
        raise RuntimeError(
            f"claude wrote an empty file for section {section_idx}"
        )

    log.info(
        "rewrite section %d done chars_in=%d chars_out=%d",
        section_idx, len(section_text), len(rewritten),
    )
    return rewritten


def _build_prompt(
    *,
    rewrite_prompt: str,
    heading: str,
    in_path: Path,
    out_path: Path,
) -> str:
    return (
        f"{rewrite_prompt}\n\n"
        f"## Task\n\n"
        f"Read the source section from this file:\n"
        f"  {in_path}\n\n"
        f"The section is titled: {heading!r}\n\n"
        f"Apply the rewrite rules above. Write the result to:\n"
        f"  {out_path}\n\n"
        f"Use your Write tool to write the file. The file should contain "
        f"ONLY the rewritten narration — no preamble, no markdown headers, "
        f"no commentary, no code fences. Just the text as it should be "
        f"spoken aloud.\n\n"
        f"When done, you can briefly confirm 'wrote section to {out_path.name}' "
        f"to stdout — but everything that's actually podcast content must be "
        f"in the file."
    )
