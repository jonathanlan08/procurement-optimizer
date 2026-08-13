"""Storage provider package: the `StorageProvider` Protocol, its three
implementations, and `build_storage_provider()` - the factory that turns
`app.core.config.Settings` into a concrete provider.

**Not a FastAPI dependency in `app/api/deps.py`.** `deps.py` is
 and off limits to this change; per the delegating task's own
instruction, `api/v1/documents.py` calls `build_storage_provider(settings)`
directly inside its own `get_document_service` dependency function (using the
shared `SettingsDep`), building a fresh provider once per
request and handing it to `DocumentService` as a constructor argument -
mirroring how every other per-request service dependency in this codebase is
already assembled (`get_quote_service`, `get_part_import_service`, ...).
"""

from __future__ import annotations

from app.core.config import Settings, StorageProviderKind
from app.providers.storage.base import KEY_RE, StorageProvider, validate_key
from app.providers.storage.filesystem import FilesystemStorageProvider
from app.providers.storage.memory import MemoryStorageProvider


def build_storage_provider(settings: Settings) -> StorageProvider:
    if settings.storage_provider is StorageProviderKind.FILESYSTEM:
        return FilesystemStorageProvider(settings.storage_root)

    if settings.storage_provider is StorageProviderKind.S3:
        # Imported lazily so `boto3` is only required at runtime when S3 is
        # actually selected, not merely because the package was imported
        # (filesystem/memory-only deployments never need it installed).
        from app.providers.storage.s3 import S3StorageProvider

        # `Settings._fail_fast` already refuses to construct a Settings
        # object with storage_provider=s3 and any of these unset; the checks
        # here are just what mypy needs to narrow `str | None` -> `str`.
        if settings.s3_bucket is None:
            raise ValueError("storage provider 's3' requires PO_S3_BUCKET")
        if settings.s3_access_key is None:
            raise ValueError("storage provider 's3' requires PO_S3_ACCESS_KEY")
        if settings.s3_secret_key is None:
            raise ValueError("storage provider 's3' requires PO_S3_SECRET_KEY")
        return S3StorageProvider(
            bucket=settings.s3_bucket,
            endpoint_url=settings.s3_endpoint_url,
            access_key=settings.s3_access_key.get_secret_value(),
            secret_key=settings.s3_secret_key.get_secret_value(),
        )

    raise ValueError(f"Unsupported storage provider: {settings.storage_provider!r}")


__all__ = [
    "KEY_RE",
    "FilesystemStorageProvider",
    "MemoryStorageProvider",
    "StorageProvider",
    "build_storage_provider",
    "validate_key",
]
