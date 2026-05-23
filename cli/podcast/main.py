"""CLI entry point — ``podcast`` binary.

Subcommands:
  podcast gen <source>     Generate one MP3 episode from a markdown doc.
  podcast version          Print version + sanity-check deps.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import sys
from pathlib import Path
from typing import Optional

import typer

from . import __version__
from .pipeline import PipelineConfig, generate_podcast

cli = typer.Typer(
    name="podcast",
    help="Convert markdown documents into narrated podcast episodes via Spindle.",
    no_args_is_help=True,
)


@cli.command()
def gen(
    source: Path = typer.Argument(
        ..., exists=True, dir_okay=False, readable=True,
        help="Markdown file to convert.",
    ),
    config_id: str = typer.Option(
        "audio-tts-openai-v1", "--config-id", "-c",
        help="Spindle ModelConfig to use for TTS.",
    ),
    voice: Optional[str] = typer.Option(
        None, "--voice", help="Backend-specific voice id; omit for default.",
    ),
    spindle_url: str = typer.Option(
        "http://localhost:8080", "--spindle-url",
        help="Spindle API base URL.",
    ),
    audio_dir: Path = typer.Option(
        Path("./audio"), "--audio-dir",
        help="Output directory for the produced MP3.",
    ),
    work_dir: Path = typer.Option(
        Path("./work"), "--work-dir",
        help="Per-run scratch directory for rewrite IO.",
    ),
    keep_work: bool = typer.Option(
        False, "--keep-work", help="Don't delete the work dir after success.",
    ),
    bitrate: str = typer.Option("64k", "--bitrate", help="MP3 bitrate."),
    rewrite_concurrency: int = typer.Option(
        5, "--rewrite-concurrency",
        help="Max parallel `claude -p` subprocesses.",
    ),
    tts_concurrency: int = typer.Option(
        5, "--tts-concurrency",
        help="Max parallel in-flight Spindle TTS jobs.",
    ),
    cli_binary: str = typer.Option(
        "claude", "--cli-binary",
        help="Subprocess binary for rewrite (default: claude).",
    ),
    log_level: str = typer.Option("INFO", "--log-level"),
) -> None:
    """Generate one podcast episode from a markdown source."""
    logging.basicConfig(
        level=log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )

    cfg = PipelineConfig(
        spindle_url=spindle_url,
        tts_config_id=config_id,
        tts_voice=voice,
        audio_dir=audio_dir,
        work_dir=work_dir,
        keep_work_dir=keep_work,
        mp3_bitrate=bitrate,
        rewrite_concurrency=rewrite_concurrency,
        tts_concurrency=tts_concurrency,
        cli_binary=cli_binary,
    )

    try:
        out_path = asyncio.run(generate_podcast(source, cfg))
    except KeyboardInterrupt:
        typer.secho("interrupted", fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(130)
    except Exception as e:
        typer.secho(f"pipeline failed: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)

    typer.secho(f"wrote {out_path}", fg=typer.colors.GREEN)


@cli.command()
def version() -> None:
    """Print version + check for required deps on PATH."""
    typer.echo(f"podcast-this {__version__}")
    typer.echo("")
    for binary, where in [
        ("claude", "Anthropic's claude CLI — used to rewrite sections"),
        ("ffmpeg", "audio encoder — used to write MP3"),
    ]:
        path = shutil.which(binary)
        marker = "✓" if path else "✗"
        typer.echo(f"  {marker} {binary:8s} {path or '(not found)'}  — {where}")


if __name__ == "__main__":
    cli()
