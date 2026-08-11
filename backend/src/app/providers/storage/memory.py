"""In-memory `StorageProvider` — test double for `document_service.py` and
`tests/unit/test_storage_providers.py`. No filesystem, no network; state
lives only for the life of the Python object.
"""

from __future__ import annotations

import uuid

from app.providers.storage.base import validate_key


class MemoryStorageProvider:
    def __init__(self) -> None:
        self._objects: dict[tuple[uuid.UUID, str], bytes] = {}
        self._content_types: dict[tuple[uuid.UUID, str], str] = {}

    def put(self, *, organization_id: uuid.UUID, key: str, data: bytes, content_type: str) -> None:
        validate_key(key)
        self._objects[(organization_id, key)] = data
        self._content_types[(organization_id, key)] = content_type

    def get(self, *, organization_id: uuid.UUID, key: str) -> bytes:
        validate_key(key)
        try:
            return self._objects[(organization_id, key)]
        except KeyError:
            raise FileNotFoundError(f"No stored object for key {key!r}.") from None

    def exists(self, *, organization_id: uuid.UUID, key: str) -> bool:
        validate_key(key)
        return (organization_id, key) in self._objects

    def delete(self, *, organization_id: uuid.UUID, key: str) -> None:
        validate_key(key)
        self._objects.pop((organization_id, key), None)
        self._content_types.pop((organization_id, key), None)


__all__ = ["MemoryStorageProvider"]
