# Podcast This

Convert documents from a markdown source directory into narrated podcast episodes. CLI on a home server runs the pipeline (LLM rewrite → local TTS → mp3 + RSS feed); a phone consumes the feed via any podcast app (Overcast, Apple Podcasts, Spotify, etc.).

Designed as the first mini-app of [`../bridge/`](../bridge/). Standalone CLI works without Bridge too.

> **Project type:** Personal infra. Open-source target — released once pipeline + Bridge integration stabilize. Until then, internal-only.
> **Source content:** any directory of markdown docs (config-driven; default in `./content/`, override per-invocation).
> **Hosting model:** CLI + audio + feed live on the home server; phone subscribes over Tailscale.
> **Status:** pre-implementation. Scope locked, code not started.

---

## 1. The mental model — server generates, phone consumes

Expensive work (LLM rewrite, TTS inference, audio stitching) happens on the home server ahead of time. The phone never does work — it streams or downloads mp3s through a normal podcast app.

The selection gesture ("convert this doc into an episode") happens once on the server via CLI — or remotely via [`../bridge/`](../bridge/) from the phone, or via an MCP tool call from a local LLM agent. From then on, the episode lives in the RSS feed and any podcast app picks it up. No mobile dev needed for MVP — Overcast is the player.

## 2. Architecture

```
┌──────────────────┐
│  Markdown source │
│  directory       │
└────────┬─────────┘
         │
         ▼
┌──────────────────┐     ┌──────────────────┐
│  CLI on server   │ ──▶ │ LLM API          │  rewrite per H2 section
│  podcast gen ... │     │ (Claude Opus)    │
└────────┬─────────┘     └────────┬─────────┘
         │  spoken-form script    │
         ▼  ◀──────────────────────
┌──────────────────┐
│  Local TTS       │  per-section wavs
│  (F5-TTS, GPU)   │
└────────┬─────────┘
         │  stitch + ID3v2 chapter markers
         ▼
┌──────────────────┐
│  mp3 (chaptered) │
└────────┬─────────┘
         │
         ▼
┌──────────────────┐    Tailscale    ┌──────────────────┐
│  Caddy on server │ ──────────────▶ │  Phone           │
│  serves feed.xml │    HTTPS        │  Overcast etc.   │
│  + audio files   │                 │  subscribes      │
└──────────────────┘                 └──────────────────┘
```

## 3. Locked design decisions

| Decision | Choice | Why |
|---|---|---|
| Selection workflow | CLI on server, RSS feed → phone | Skips native app; podcast apps handle queue, state, sync |
| Episode granularity | 1 H1 doc = 1 episode, H2s = chapter markers | Clean feed; chapters render as skip points in app |
| Narration style | Single narrator, faithful rewrite | Two-host dialogue flattens technical content |
| Length cap | None | Podcast apps handle long-form fine; skip via chapters |
| LLM for rewrite | Claude API (Opus, section-by-section) | Best quality for technical narration |
| TTS | F5-TTS local (GPU-backed) | Quality near hosted APIs, no per-character cost, voice cloning option |
| Audio format | MP3 with ID3v2 CHAP frames | Universal podcast app support |
| Hosting | Local server + Caddy + Tailscale | Private, owned |
| Feed | RSS 2.0 + iTunes namespace | Compatible with every podcast app |

## 4. Pipeline

1. **Input:** `podcast gen <path/to/doc.md>` on the server.
2. **Parse:** split markdown by `##` headings. Track section titles + offsets.
3. **Rewrite:** for each section, call the LLM with `prompts/rewrite-section.md`. Outputs spoken-form text.
4. **Stitch script:** assemble sections with a title intro + spoken transitions ("Section 3: backpropagation in matrix form.").
5. **TTS:** synthesize each section to wav.
6. **Combine audio:** concat wavs, encode to mp3, embed ID3v2 CHAP frames at section boundaries.
7. **Publish:** copy mp3 to `audio/`, update `feed.xml`.
8. **Phone:** podcast app polls feed, downloads new episode.

## 5. The load-bearing prompt: section rewrite

Lives at `prompts/rewrite-section.md`. Responsibilities:

- **Code blocks → intent + shape.** "A Python snippet that reads N bytes, applies tanh, writes back." Never raw syntax.
- **Tables → narrated comparison.** "On read latency, NVMe gets 70 microseconds, SATA SSD 200 microseconds, HDD 4 milliseconds."
- **Inline math → English.** "Gradient with respect to w is x times the error term."
- **Preserve concrete numbers.** Numbers are why technical docs exist; don't soften them.
- **Preserve technical jargon.** Domain terms stay as-is. "Backpropagation" stays "backpropagation," not "the chain rule update step." The point is real teaching, not background ambience.
- **Cross-references → speak by title.** When source markdown links to another doc, mention it by title ("see the linear algebra doc, section three"), don't expand the linked content inline. Inline expansion is a v1+ feature gated on a retrieval index.
- **Drop heading markers**, replace with spoken transitions.
- **No host chatter.** No "and that's super cool" — clean narration.

This prompt is where podcast quality lives. Iterate on one section before running the full pipeline.

## 6. MVP scope (v0)

- CLI: `podcast gen <path>` on a single markdown file.
- Output: one mp3 written to `audio/`.
- Feed: hand-maintained `feed.xml` initially, to validate end-to-end.
- TTS: F5-TTS, fixed default voice.
- Hosting: Caddy serves `audio/` + `feed.xml` over Tailscale.

## 7. Deferred to v1+

| Feature | When |
|---|---|
| Folder-level batch (`podcast gen path/to/dir/`) | After single-file works |
| Auto-feed updates (CLI mutates `feed.xml`) | v0.5 |
| Bridge mini-app integration: phone-triggered generation | After [`../bridge/`](../bridge/) MVP |
| Bridge mini-app integration: LLM agent triggers generation via MCP | After Bridge MCP layer works |
| Voice cloning (custom narrator voice) | If default voice gets stale |
| Voice agent / interactive Q&A about an episode | v2 — different architecture (Realtime API) |
| Cross-reference enrichment via retrieval over the source corpus | After a retrieval system exists |
| Public OSS release | After Bridge stabilizes |

## 8. Project structure (planned)

```
podcast-this/
├── README.md
├── cli/
│   ├── podcast/
│   │   ├── main.py          CLI entry (typer)
│   │   ├── parse.py         markdown → sections
│   │   ├── rewrite.py       LLM API rewrite
│   │   ├── tts.py           F5-TTS wrapper
│   │   ├── stitch.py        wav concat + mp3 encode + chapters
│   │   └── feed.py          RSS feed mutation
│   └── pyproject.toml
├── prompts/
│   └── rewrite-section.md   the load-bearing prompt
├── server/
│   └── Caddyfile
├── audio/                   generated mp3s (gitignored)
├── feed/
│   └── feed.xml
└── bridge-mini-app/
    └── manifest.json        how this surfaces in ../bridge/
```

## 9. Pluggability

Two layers of the pipeline are swap points behind a base class. The rest of the pipeline doesn't know which backend is in use.

### TTS backends

```python
class BaseTTS(ABC):
    @abstractmethod
    def synthesize(self, text: str, voice: str | None = None, **opts) -> bytes:
        """Return WAV bytes for the given text."""

    @abstractmethod
    def list_voices(self) -> list[Voice]: ...
```

| Backend | Class | When to use |
|---|---|---|
| F5-TTS | `F5TTS` | Default. Local on GPU, near-ElevenLabs quality, optional voice cloning. |
| OpenAI TTS | `OpenAITTS` | Fallback if local GPU unavailable. ~$0.50/hr audio. |
| ElevenLabs | `ElevenLabsTTS` | If the quality bar rises beyond F5-TTS. |
| Kokoro-82M | `KokoroTTS` | Lightweight fallback; runs on CPU. |

### LLM providers for rewrite

```python
class BaseRewriter(ABC):
    @abstractmethod
    def rewrite(self, section: Section, prompt: str) -> str: ...
```

| Provider | Class | When to use |
|---|---|---|
| Claude API | `ClaudeRewriter` | Default. Opus for quality. |
| OpenAI | `OpenAIRewriter` | If Claude unavailable, or for A/B comparison. |
| Local (vLLM, Ollama) | `LocalRewriter` | Once open models close the gap on technical narration. |

Config picks one of each at runtime.

## 10. Practical commands

```bash
# Generate one episode
cd ~/Documents/podcast-this
uv run podcast gen path/to/doc.md

# Serve feed + audio locally
caddy run --config server/Caddyfile

# Subscribe on phone: Overcast → Add URL → http://<host>.tailnet.ts.net:8732/feed.xml
```

## 11. Cost model (rough)

| Step | Per episode (~1hr audio, ~10k source tokens) | Notes |
|---|---|---|
| LLM rewrite | $0.20–0.50 | Claude Opus, section-by-section |
| TTS inference | $0 | Local GPU |
| Storage | negligible | ~30 MB/hr at 64 kbps |
| **Total** | **<$1/episode** | vs $0.50–2/hr for hosted TTS APIs |
