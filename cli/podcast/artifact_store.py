"""MinIO / S3 upload for produced MP3 bytes.

We don't bother with the spindle-core ArtifactStore protocol here — that's
a different repo, and podcast-this has exactly one use case (write an
episode's mp3, build a URL for it). Thin boto3 wrapper is plenty.

The bucket is **not** the same as Spindle's ``spindle-artifacts``. Spindle
holds per-section WAVs from TTS jobs; podcast-this writes the stitched
final MP3 here. Keeping them separate is cleaner than cross-purposing.

Bucket policy: assumes public-read inside the Tailscale / LAN where MinIO
is bound. That's enough — Tailscale is the auth boundary. If MinIO ever
goes wider, switch to pre-signed URLs (``s3.generate_presigned_url``) and
re-hydrate the URL on each ``list_episodes`` call instead of storing it.
"""
from __future__ import annotations

import logging
from io import BytesIO

import boto3
from botocore.client import Config

log = logging.getLogger(__name__)


class MinIOArtifactStore:
    """Single bucket. ``put_mp3`` is the only write path we need."""

    def __init__(
        self,
        *,
        endpoint_url: str,
        bucket: str,
        access_key: str,
        secret_key: str,
        region: str = "us-east-1",
    ) -> None:
        self.endpoint_url = endpoint_url.rstrip("/")
        self.bucket = bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=self.endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=Config(signature_version="s3v4"),
        )

    def ensure_bucket(self) -> None:
        """Idempotent. Creates the bucket if it doesn't exist."""
        try:
            self._client.head_bucket(Bucket=self.bucket)
            return
        except Exception:
            pass
        try:
            self._client.create_bucket(Bucket=self.bucket)
            log.info("created bucket %r at %s", self.bucket, self.endpoint_url)
        except Exception as e:
            log.warning("could not create bucket %r: %s", self.bucket, e)

    def put_mp3(
        self,
        key: str,
        data: bytes,
        *,
        metadata: dict[str, str] | None = None,
    ) -> str:
        """Upload bytes; return the object key (caller resolves URL)."""
        self._client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=BytesIO(data),
            ContentType="audio/mpeg",
            Metadata=metadata or {},
        )
        log.info("uploaded %d bytes to s3://%s/%s", len(data), self.bucket, key)
        return key

    def url_for(self, key: str) -> str:
        """Build a direct-fetch URL for ``key``.

        Suitable for Tailscale-only deploys with a public-read bucket. For
        a wider deploy switch to ``generate_presigned_url`` + an
        appropriate TTL.
        """
        return f"{self.endpoint_url}/{self.bucket}/{key}"

    def put_text(self, key: str, text: str, *, content_type: str) -> str:
        """Used for the regenerated feed.xml + episodes.json."""
        self._client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=text.encode("utf-8"),
            ContentType=content_type,
            CacheControl="no-cache",
        )
        return key
