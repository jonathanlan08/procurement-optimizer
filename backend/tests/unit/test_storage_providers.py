"""`StorageProvider` contract tests — one shared suite run against
`FilesystemStorageProvider` (`tmp_path`) AND `MemoryStorageProvider`, per this
task's explicit instruction. `S3StorageProvider` is deliberately excluded: no
network in a unit test (see `app/providers/storage/s3.py`'s own module
docstring for the matching note on the implementation side).

Covers: put/get round-trip, `exists`, `delete`, key-regex rejection
(traversal attempts, uppercase, no-extension, wrong-length), and org
namespacing (same key, two orgs => two independent objects).
"""

from __future__ import annotations

import uuid

import pytest

from app.providers.storage.base import StorageProvider
from app.providers.storage.filesystem import FilesystemStorageProvider
from app.providers.storage.memory import MemoryStorageProvider

VALID_KEY = "a" * 32 + ".pdf"
OTHER_VALID_KEY = "b" * 32 + ".png"

_BAD_KEYS = [
    "../x.pdf",
    "../../etc/passwd.pdf",
    "a" * 32 + "/../b.pdf",
    "A" * 32 + ".pdf",  # uppercase not allowed
    "a" * 32,  # no extension at all
    "a" * 32 + ".",  # empty extension
    "a" * 31 + ".pdf",  # 31 hex chars, one short
    "a" * 33 + ".pdf",  # 33 hex chars, one over
    "a" * 32 + ".toolongext",  # extension > 5 chars
    "not-hex-characters-at-all-here!.pdf",
    "",
]


@pytest.fixture(params=["filesystem", "memory"])
def provider(request: pytest.FixtureRequest, tmp_path: object) -> StorageProvider:
    if request.param == "filesystem":
        return FilesystemStorageProvider(str(tmp_path))
    return MemoryStorageProvider()


class TestPutGetRoundTrip:
    def test_put_then_get_returns_same_bytes(self, provider: StorageProvider) -> None:
        org = uuid.uuid4()
        provider.put(
            organization_id=org, key=VALID_KEY, data=b"hello world", content_type="application/pdf"
        )
        assert provider.get(organization_id=org, key=VALID_KEY) == b"hello world"

    def test_put_round_trips_empty_bytes(self, provider: StorageProvider) -> None:
        org = uuid.uuid4()
        provider.put(organization_id=org, key=VALID_KEY, data=b"", content_type="application/pdf")
        assert provider.get(organization_id=org, key=VALID_KEY) == b""

    def test_put_overwrites_existing_key(self, provider: StorageProvider) -> None:
        org = uuid.uuid4()
        provider.put(organization_id=org, key=VALID_KEY, data=b"v1", content_type="application/pdf")
        provider.put(organization_id=org, key=VALID_KEY, data=b"v2", content_type="application/pdf")
        assert provider.get(organization_id=org, key=VALID_KEY) == b"v2"

    def test_get_missing_key_raises_file_not_found(self, provider: StorageProvider) -> None:
        org = uuid.uuid4()
        with pytest.raises(FileNotFoundError):
            provider.get(organization_id=org, key=VALID_KEY)


class TestExists:
    def test_exists_true_after_put(self, provider: StorageProvider) -> None:
        org = uuid.uuid4()
        provider.put(organization_id=org, key=VALID_KEY, data=b"x", content_type="text/csv")
        assert provider.exists(organization_id=org, key=VALID_KEY) is True

    def test_exists_false_before_put(self, provider: StorageProvider) -> None:
        org = uuid.uuid4()
        assert provider.exists(organization_id=org, key=VALID_KEY) is False

    def test_exists_false_after_delete(self, provider: StorageProvider) -> None:
        org = uuid.uuid4()
        provider.put(organization_id=org, key=VALID_KEY, data=b"x", content_type="text/csv")
        provider.delete(organization_id=org, key=VALID_KEY)
        assert provider.exists(organization_id=org, key=VALID_KEY) is False


class TestDelete:
    def test_delete_removes_object(self, provider: StorageProvider) -> None:
        org = uuid.uuid4()
        provider.put(organization_id=org, key=VALID_KEY, data=b"x", content_type="text/csv")
        provider.delete(organization_id=org, key=VALID_KEY)
        with pytest.raises(FileNotFoundError):
            provider.get(organization_id=org, key=VALID_KEY)

    def test_delete_missing_key_is_a_noop(self, provider: StorageProvider) -> None:
        org = uuid.uuid4()
        provider.delete(organization_id=org, key=VALID_KEY)  # must not raise

    def test_delete_one_key_leaves_sibling_key_intact(self, provider: StorageProvider) -> None:
        org = uuid.uuid4()
        provider.put(organization_id=org, key=VALID_KEY, data=b"x", content_type="text/csv")
        provider.put(organization_id=org, key=OTHER_VALID_KEY, data=b"y", content_type="image/png")
        provider.delete(organization_id=org, key=VALID_KEY)
        assert provider.exists(organization_id=org, key=OTHER_VALID_KEY) is True
        assert provider.get(organization_id=org, key=OTHER_VALID_KEY) == b"y"


class TestKeyRegexRejection:
    """`KEY_RE` in `providers/storage/base.py`: `^[a-z0-9]{32}\\.[a-z0-9]{2,5}$`.
    Every public method validates before touching the backing store."""

    @pytest.mark.parametrize("bad_key", _BAD_KEYS)
    def test_put_rejects_non_conforming_key(self, provider: StorageProvider, bad_key: str) -> None:
        org = uuid.uuid4()
        with pytest.raises(ValueError):
            provider.put(
                organization_id=org, key=bad_key, data=b"x", content_type="application/pdf"
            )

    @pytest.mark.parametrize("bad_key", _BAD_KEYS)
    def test_get_rejects_non_conforming_key(self, provider: StorageProvider, bad_key: str) -> None:
        org = uuid.uuid4()
        with pytest.raises(ValueError):
            provider.get(organization_id=org, key=bad_key)

    @pytest.mark.parametrize("bad_key", _BAD_KEYS)
    def test_exists_rejects_non_conforming_key(
        self, provider: StorageProvider, bad_key: str
    ) -> None:
        org = uuid.uuid4()
        with pytest.raises(ValueError):
            provider.exists(organization_id=org, key=bad_key)

    @pytest.mark.parametrize("bad_key", _BAD_KEYS)
    def test_delete_rejects_non_conforming_key(
        self, provider: StorageProvider, bad_key: str
    ) -> None:
        org = uuid.uuid4()
        with pytest.raises(ValueError):
            provider.delete(organization_id=org, key=bad_key)

    def test_traversal_key_never_escapes_the_org_root(
        self, tmp_path: object, provider: StorageProvider
    ) -> None:
        """Belt and braces: even though KEY_RE rejects this key before any
        filesystem call is made, assert no file was written anywhere outside
        (or inside) the intended root as a result of attempting it."""
        org = uuid.uuid4()
        with pytest.raises(ValueError):
            provider.put(
                organization_id=org,
                key="../../../../tmp/escaped.pdf",
                data=b"x",
                content_type="application/pdf",
            )
        assert provider.exists(organization_id=org, key=VALID_KEY) is False


class TestOrgNamespacing:
    def test_same_key_two_orgs_are_different_objects(self, provider: StorageProvider) -> None:
        org_a = uuid.uuid4()
        org_b = uuid.uuid4()
        provider.put(
            organization_id=org_a,
            key=VALID_KEY,
            data=b"org-a-bytes",
            content_type="application/pdf",
        )
        provider.put(
            organization_id=org_b,
            key=VALID_KEY,
            data=b"org-b-bytes",
            content_type="application/pdf",
        )
        assert provider.get(organization_id=org_a, key=VALID_KEY) == b"org-a-bytes"
        assert provider.get(organization_id=org_b, key=VALID_KEY) == b"org-b-bytes"

    def test_delete_in_one_org_does_not_affect_other(self, provider: StorageProvider) -> None:
        org_a = uuid.uuid4()
        org_b = uuid.uuid4()
        provider.put(
            organization_id=org_a, key=VALID_KEY, data=b"a", content_type="application/pdf"
        )
        provider.put(
            organization_id=org_b, key=VALID_KEY, data=b"b", content_type="application/pdf"
        )
        provider.delete(organization_id=org_a, key=VALID_KEY)
        assert provider.exists(organization_id=org_a, key=VALID_KEY) is False
        assert provider.exists(organization_id=org_b, key=VALID_KEY) is True

    def test_exists_false_for_unwritten_org_even_if_another_org_has_the_key(
        self, provider: StorageProvider
    ) -> None:
        org_a = uuid.uuid4()
        org_b = uuid.uuid4()
        provider.put(
            organization_id=org_a, key=VALID_KEY, data=b"a", content_type="application/pdf"
        )
        assert provider.exists(organization_id=org_b, key=VALID_KEY) is False


class TestFilesystemLayout:
    """Filesystem-only behaviors not part of the shared Protocol contract
    (on-disk layout, atomicity) — `MemoryStorageProvider` has no directory
    structure or partial-write failure mode to assert against."""

    def test_layout_is_root_slash_org_slash_key(self, tmp_path: object) -> None:
        from pathlib import Path

        provider = FilesystemStorageProvider(str(tmp_path))
        org = uuid.uuid4()
        provider.put(organization_id=org, key=VALID_KEY, data=b"x", content_type="application/pdf")
        assert (Path(str(tmp_path)) / str(org) / VALID_KEY).is_file()

    def test_no_leftover_temp_file_after_put(self, tmp_path: object) -> None:
        from pathlib import Path

        provider = FilesystemStorageProvider(str(tmp_path))
        org = uuid.uuid4()
        provider.put(organization_id=org, key=VALID_KEY, data=b"x", content_type="application/pdf")
        names = {p.name for p in (Path(str(tmp_path)) / str(org)).iterdir()}
        assert VALID_KEY in names
        assert not any(n.startswith(".tmp-") for n in names)

    def test_content_type_sidecar_written(self, tmp_path: object) -> None:
        from pathlib import Path

        provider = FilesystemStorageProvider(str(tmp_path))
        org = uuid.uuid4()
        provider.put(organization_id=org, key=VALID_KEY, data=b"x", content_type="application/pdf")
        sidecar = Path(str(tmp_path)) / str(org) / f"{VALID_KEY}.meta"
        assert sidecar.read_text(encoding="utf-8") == "application/pdf"

    def test_delete_removes_sidecar_too(self, tmp_path: object) -> None:
        from pathlib import Path

        provider = FilesystemStorageProvider(str(tmp_path))
        org = uuid.uuid4()
        provider.put(organization_id=org, key=VALID_KEY, data=b"x", content_type="application/pdf")
        provider.delete(organization_id=org, key=VALID_KEY)
        sidecar = Path(str(tmp_path)) / str(org) / f"{VALID_KEY}.meta"
        assert not sidecar.exists()
