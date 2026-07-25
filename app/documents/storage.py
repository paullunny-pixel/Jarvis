"""Where uploaded files live.

Production target: Cloudflare R2 (S3-compatible, via boto3). Until R2 keys are
configured, files are stored in the database (bytea) — perfectly sound for a
single user's documents and means the library works on day one. The `documents`
table records which backend holds each file, so both can coexist and a later
migration to R2 is trivial.
"""
from __future__ import annotations

from typing import Protocol


class ObjectStore(Protocol):
    name: str

    async def put(self, key: str, data: bytes, content_type: str = "") -> None: ...
    async def get(self, key: str) -> bytes: ...


class R2Store:
    name = "r2"

    def __init__(self, access_key: str, secret_key: str, bucket: str, endpoint: str) -> None:
        self._bucket = bucket
        self._endpoint = endpoint
        self._access_key = access_key
        self._secret_key = secret_key
        self._client = None

    def _s3(self):
        if self._client is None:
            import boto3

            self._client = boto3.client(
                "s3",
                endpoint_url=self._endpoint,
                aws_access_key_id=self._access_key,
                aws_secret_access_key=self._secret_key,
                region_name="auto",
            )
        return self._client

    async def put(self, key: str, data: bytes, content_type: str = "") -> None:
        import asyncio

        extra = {"ContentType": content_type} if content_type else {}
        await asyncio.to_thread(
            self._s3().put_object, Bucket=self._bucket, Key=key, Body=data, **extra
        )

    async def get(self, key: str) -> bytes:
        import asyncio

        def _get() -> bytes:
            obj = self._s3().get_object(Bucket=self._bucket, Key=key)
            return obj["Body"].read()

        return await asyncio.to_thread(_get)


def make_object_store(access_key: str, secret_key: str, bucket: str, endpoint: str):
    """R2 when configured, otherwise None (files go in the database)."""
    if access_key and secret_key and bucket and endpoint:
        return R2Store(access_key, secret_key, bucket, endpoint)
    return None
