"""Filesystem `StorageProvider` - the dev/default backend
(`PO_STORAGE_PROVIDER=filesystem`, `app.core.config.Settings.storage_root`).

Layout: `<root>/<organization_id>/<key>` - org isolation is a real directory
boundary, not just a query filter. `content_type` is recorded in a sidecar
`<key>.meta` text file (the choice `document_service.py`'s task brief left
open): the `StorageProvider.get()` signature returns only `bytes` - nothing
in this package's Protocol ever reads the sidecar back - but it is written
anyway for defense in depth (an operator inspecting `storage_root` by hand,
or a future admin/debug tool, should not have to guess a file's type from
its extensionless key).

Writes are atomic: bytes go to a temp file in the *same* directory first,
`fsync`ed, then moved into place with `os.replace` (a single filesystem
rename, atomic on POSIX and Windows alike) - a concurrent reader can only
ever observe either the previous complete object or the new complete one,
never a partial write.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
import uuid
from pathlib import Path

from app.providers.storage.base import validate_key


class FilesystemStorageProvider:
    def __init__(self, root: str) -> None:
        self._root = Path(root)

    def _org_dir(self, organization_id: uuid.UUID) -> Path:
        return self._root / str(organization_id)

    def _path(self, organization_id: uuid.UUID, key: str) -> Path:
        validate_key(key)
        return self._org_dir(organization_id) / key

    def _meta_path(self, organization_id: uuid.UUID, key: str) -> Path:
        # key already validated by _path's caller in every public method
        # below; this helper is only ever called alongside _path.
        return self._org_dir(organization_id) / f"{key}.meta"

    def put(self, *, organization_id: uuid.UUID, key: str, data: bytes, content_type: str) -> None:
        path = self._path(organization_id, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=f"-{key}")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.remove(tmp_name)
            raise
        self._meta_path(organization_id, key).write_text(content_type, encoding="utf-8")

    def get(self, *, organization_id: uuid.UUID, key: str) -> bytes:
        path = self._path(organization_id, key)
        try:
            return path.read_bytes()
        except FileNotFoundError:
            raise FileNotFoundError(f"No stored object for key {key!r}.") from None

    def exists(self, *, organization_id: uuid.UUID, key: str) -> bool:
        return self._path(organization_id, key).is_file()

    def delete(self, *, organization_id: uuid.UUID, key: str) -> None:
        self._path(organization_id, key).unlink(missing_ok=True)
        self._meta_path(organization_id, key).unlink(missing_ok=True)


__all__ = ["FilesystemStorageProvider"]
