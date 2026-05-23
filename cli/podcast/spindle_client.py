"""Thin async client for Spindle's API.

Only covers the endpoints podcast-this needs: submit a job, poll until
terminal, download an artifact. See spindle/AGENTS.md for the full surface.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)


_TERMINAL_STATUSES = frozenset(
    {"succeeded", "failed", "canceled", "dead_lettered"}
)


class SpindleClient:
    """One-shot client for the podcast-gen pipeline. Use as an async context
    manager so the underlying httpx client closes cleanly."""

    def __init__(
        self,
        base_url: str = "http://localhost:8080",
        *,
        auth_token: str | None = None,
        timeout_s: float = 120.0,
    ) -> None:
        headers = {"Authorization": f"Bearer {auth_token}"} if auth_token else {}
        self._client = httpx.AsyncClient(
            base_url=base_url, headers=headers, timeout=timeout_s
        )

    async def __aenter__(self) -> SpindleClient:
        return self

    async def __aexit__(self, *_exc) -> None:
        await self._client.aclose()

    async def close(self) -> None:
        await self._client.aclose()

    # ---- submission + polling -------------------------------------------------

    async def submit_audio_tts(
        self,
        *,
        text: str,
        config_id: str,
        voice: str | None = None,
        idempotency_key: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> str:
        """Submit a text → WAV job. Returns job_id."""
        input_: dict[str, Any] = {"text": text}
        if voice:
            input_["voice"] = voice
        if options:
            input_["options"] = options

        body: dict[str, Any] = {
            "type": "audio.tts",
            "config_id": config_id,
            "input": input_,
        }
        if idempotency_key:
            body["idempotency_key"] = idempotency_key

        r = await self._client.post("/jobs", json=body)
        r.raise_for_status()
        return r.json()["job_id"]

    async def wait_for_terminal(
        self,
        job_id: str,
        *,
        poll_interval_s: float = 1.0,
        timeout_s: float = 600.0,
    ) -> dict[str, Any]:
        """Poll until the job hits a terminal state. Returns the full job dict."""
        deadline = asyncio.get_event_loop().time() + timeout_s
        while True:
            r = await self._client.get(f"/jobs/{job_id}")
            r.raise_for_status()
            job = r.json()
            if job["status"] in _TERMINAL_STATUSES:
                return job
            if asyncio.get_event_loop().time() > deadline:
                raise RuntimeError(
                    f"job {job_id} did not reach terminal state within {timeout_s}s "
                    f"(last status: {job['status']})"
                )
            await asyncio.sleep(poll_interval_s)

    # ---- artifacts -----------------------------------------------------------

    async def download_artifact(self, artifact_id: str) -> bytes:
        r = await self._client.get(f"/artifacts/{artifact_id}/bytes")
        r.raise_for_status()
        return r.content

    # ---- convenience ---------------------------------------------------------

    async def synthesize_to_wav(
        self,
        *,
        text: str,
        config_id: str,
        voice: str | None = None,
        idempotency_key: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> bytes:
        """Submit → wait → download in one call. Raises if the job didn't
        succeed or didn't produce an audio artifact."""
        job_id = await self.submit_audio_tts(
            text=text,
            config_id=config_id,
            voice=voice,
            idempotency_key=idempotency_key,
            options=options,
        )
        log.info("submitted job_id=%s", job_id)
        job = await self.wait_for_terminal(job_id)

        if job["status"] != "succeeded":
            err = job.get("error") or {}
            raise RuntimeError(
                f"job {job_id} ended {job['status']}: "
                f"code={err.get('code')} msg={err.get('message')!r:.200}"
            )

        audio_artifacts = [a for a in job["artifacts"] if a["kind"] == "audio"]
        if not audio_artifacts:
            raise RuntimeError(
                f"job {job_id} succeeded but produced no audio artifact"
            )
        return await self.download_artifact(audio_artifacts[0]["id"])
