"""S3 `StorageProvider` — `boto3`-backed, for `PO_STORAGE_PROVIDER=s3`
deployments (`app.core.config.Settings.s3_endpoint_url/s3_bucket/
s3_access_key/s3_secret_key`, all required together by `Settings._fail_fast`
when `storage_provider` is `s3`).

Configuration is threaded through the constructor by
`providers/storage/__init__.py`'s factory, never read from `Settings`
directly in this module — the same constructor-injection shape
`FilesystemStorageProvider`/`MemoryStorageProvider` already use, so every
provider is equally testable without a real settings object.

**Not exercised by `tests/unit/test_storage_providers.py`** — that suite runs
the shared put/get/exists/delete/key-regex/org-namespacing contract against
the filesystem and memory providers only; this module talks to a real (or
mocked-at-the-socket-level) S3-compatible endpoint, which is out of scope for
a no-network unit test, per the delegating task's own instruction.

`boto3`/`botocore` ship no inline type stubs and this project does not vendor
`boto3-stubs`, so the import and the client's dynamically-generated methods
are `Any` from mypy's point of view — `type: ignore[import-untyped]` on the
import is therefore precise (not a blanket suppression) rather than a
project-wide mypy config change, which is outside this task's only-permitted
`pyproject.toml` edit (the one added dependency line).
"""

from __future__ import annotations

import uuid

import boto3  # type: ignore[import-untyped]
from botocore.client import Config as BotoConfig  # type: ignore[import-untyped]
from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from app.providers.storage.base import validate_key

_NOT_FOUND_CODES = frozenset({"404", "NoSuchKey"})


class S3StorageProvider:
    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: str | None,
        access_key: str,
        secret_key: str,
        region_name: str | None = None,
    ) -> None:
        self._bucket = bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region_name,
            config=BotoConfig(signature_version="s3v4"),
        )

    def _object_key(self, organization_id: uuid.UUID, key: str) -> str:
        validate_key(key)
        return f"{organization_id}/{key}"

    def put(self, *, organization_id: uuid.UUID, key: str, data: bytes, content_type: str) -> None:
        self._client.put_object(
            Bucket=self._bucket,
            Key=self._object_key(organization_id, key),
            Body=data,
            ContentType=content_type,
        )

    def get(self, *, organization_id: uuid.UUID, key: str) -> bytes:
        try:
            obj = self._client.get_object(
                Bucket=self._bucket, Key=self._object_key(organization_id, key)
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in _NOT_FOUND_CODES:
                raise FileNotFoundError(f"No stored object for key {key!r}.") from None
            raise
        body: bytes = obj["Body"].read()
        return body

    def exists(self, *, organization_id: uuid.UUID, key: str) -> bool:
        try:
            self._client.head_object(
                Bucket=self._bucket, Key=self._object_key(organization_id, key)
            )
            return True
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in _NOT_FOUND_CODES:
                return False
            raise

    def delete(self, *, organization_id: uuid.UUID, key: str) -> None:
        self._client.delete_object(Bucket=self._bucket, Key=self._object_key(organization_id, key))


__all__ = ["S3StorageProvider"]
