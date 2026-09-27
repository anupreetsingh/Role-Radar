"""Thin boto3 helpers: sessions, the companies file in S3, secrets in SSM Parameter Store.

boto3 is an optional dependency (`pip install 'role-radar[aws]'`). It's
imported only here and in dynamo.py, so JSON-only use never needs it.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlsplit

log = logging.getLogger(__name__)


def session(region: str | None = None, profile: str | None = None) -> Any:
    try:
        import boto3
    except ImportError as exc:
        raise RuntimeError("the AWS backends need boto3: pip install 'role-radar[aws]'") from exc
    return boto3.session.Session(profile_name=profile, region_name=region)


def client(sess: Any, service: str) -> Any:
    """A client that retries throttling and transient errors more patiently than the default.

    The table runs on a small provisioned capacity, so short bursts of
    throttling are expected; boto3 backs off and retries them.
    """
    from botocore.config import Config

    config = Config(
        retries={"mode": "standard", "max_attempts": 10},
        max_pool_connections=50,  # companies are saved from several threads at once
        connect_timeout=5,
        read_timeout=20,
    )
    return sess.client(service, config=config)


def parse_s3_url(url: str) -> tuple[str, str]:
    """s3://bucket/key → (bucket, key)."""
    parts = urlsplit(url)
    key = parts.path.lstrip("/")
    if parts.scheme != "s3" or not parts.netloc or not key:
        raise ValueError(f"expected s3://bucket/key, got {url!r}")
    return parts.netloc, key


class S3Text:
    """A small text object in S3 (the companies file), re-read only when it changes."""

    def __init__(self, s3: Any, url: str) -> None:
        self.s3 = s3
        self.url = url
        self.bucket, self.key = parse_s3_url(url)
        self.etag: str | None = None
        self.text: str | None = None

    def read(self) -> str:
        """The current text. After the first read, asks S3 only for a newer version (If-None-Match)."""
        from botocore.exceptions import ClientError

        kwargs = {"Bucket": self.bucket, "Key": self.key}
        if self.etag and self.text is not None:
            kwargs["IfNoneMatch"] = self.etag
        try:
            obj = self.s3.get_object(**kwargs)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in ("304", "NotModified") and self.text is not None:
                return self.text
            if code in ("NoSuchKey", "404"):
                raise FileNotFoundError(f"{self.url} doesn't exist yet: run `role-radar config push`") from exc
            raise
        self.text = obj["Body"].read().decode("utf-8")
        self.etag = obj.get("ETag")
        return self.text

    def write(self, text: str) -> None:
        self.s3.put_object(Bucket=self.bucket, Key=self.key, Body=text.encode("utf-8"), ContentType="application/yaml")
        self.text, self.etag = text, None


def ssm_parameters(ssm: Any, path: str) -> dict[str, str]:
    """Every parameter directly under `path` (e.g. /role-radar/), decrypted.

    Keyed by the name's last segment, so /role-radar/DISCORD_WEBHOOK_URL
    becomes DISCORD_WEBHOOK_URL, the same name as the environment variable.
    """
    values: dict[str, str] = {}
    for page in ssm.get_paginator("get_parameters_by_path").paginate(Path=path, WithDecryption=True):
        for param in page.get("Parameters", []):
            values[param["Name"].rsplit("/", 1)[-1]] = param["Value"]
    log.info("Loaded %d setting(s) from SSM under %s", len(values), path)  # names only, never values
    return values
