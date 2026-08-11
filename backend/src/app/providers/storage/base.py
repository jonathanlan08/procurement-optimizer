"""`StorageProvider` Protocol — PRINCIPAL-SPECIFIED, implemented exactly as
given (frozen design; see the delegating task's own literal interface).

No public URLs, ever: bytes are streamed through the API
(`GET /quote-documents/{id}/content`), never handed out as a link a browser
can fetch directly (04-document-pipeline.md §4). Every implementation MUST:

1. validate `key` against `KEY_RE` before touching any backing store, raising
   `ValueError` otherwise — `validate_key()` below is the one place this
   check is written, and every provider in this package calls it from every
   public method rather than re-deriving the regex; and
2. namespace strictly by `organization_id` (filesystem: `root/<org_id>/<key>`;
   S3: `<bucket>/<org_id>/<key>`) — org isolation enforced at the storage
   layer itself, not merely trusted from the caller.

`key` is always server-generated (`app.ingestion.file_validation.
validate_upload`'s `storage_key`: 32 lowercase hex chars + the sniffed
extension, e.g. `"a1b2c3...d4.pdf"`) — never derived from the client's
filename, so path traversal is structurally impossible upstream of this
module too.
"""

from __future__ import annotations

import re
import uuid
from typing import Protocol

KEY_RE = re.compile(r"^[a-z0-9]{32}\.[a-z0-9]{2,5}$")


class StorageProvider(Protocol):
    def put(
        self, *, organization_id: uuid.UUID, key: str, data: bytes, content_type: str
    ) -> None: ...

    def get(self, *, organization_id: uuid.UUID, key: str) -> bytes: ...

    def exists(self, *, organization_id: uuid.UUID, key: str) -> bool: ...

    def delete(self, *, organization_id: uuid.UUID, key: str) -> None: ...


def validate_key(key: str) -> None:
    """Shared by every implementation's every public method. Raises
    `ValueError` (not an `AppError` subclass — this is a package-internal
    contract violation, not a user-facing request error) when `key` is not a
    server-generated storage key."""
    if not KEY_RE.match(key):
        raise ValueError(
            f"Storage key {key!r} does not match the server-generated key format "
            f"({KEY_RE.pattern})."
        )


__all__ = ["KEY_RE", "StorageProvider", "validate_key"]
