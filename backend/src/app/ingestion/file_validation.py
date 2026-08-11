"""Upload security core — PRINCIPAL-OWNED. Every uploaded document is untrusted.

Pure module (no I/O): callers stream bytes and enforce the size cap at the
transport layer; this module rules on what the bytes claim to be.

Controls (docs/planning/07-security-model.md; SPEC §Document and AI security):
- type allowlist decided by MAGIC BYTES, never by extension or Content-Type
  alone; extension and declared type must AGREE with the sniffed type
- filename policy: the original name is data (stored, displayed escaped),
  never a path; storage keys are server-generated UUIDs
- CSV guarded against NUL bytes and undecodable content
- XLSX is a zip: sniffed as such, deep validation happens in the parser layer
  (read_only + no formula evaluation), and zip-bomb limits are enforced there
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from dataclasses import dataclass
from enum import StrEnum


class DocumentKind(StrEnum):
    PDF = "pdf"
    PNG = "png"
    JPEG = "jpeg"
    CSV = "csv"
    XLSX = "xlsx"


_EXTENSIONS: dict[DocumentKind, tuple[str, ...]] = {
    DocumentKind.PDF: (".pdf",),
    DocumentKind.PNG: (".png",),
    DocumentKind.JPEG: (".jpg", ".jpeg"),
    DocumentKind.CSV: (".csv",),
    DocumentKind.XLSX: (".xlsx",),
}

_CONTENT_TYPES: dict[DocumentKind, tuple[str, ...]] = {
    DocumentKind.PDF: ("application/pdf",),
    DocumentKind.PNG: ("image/png",),
    DocumentKind.JPEG: ("image/jpeg",),
    DocumentKind.CSV: ("text/csv", "application/csv", "text/plain"),
    DocumentKind.XLSX: (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/octet-stream",
    ),
}

MAX_FILENAME_LENGTH = 160


class FileValidationError(ValueError):
    """Message is safe for display; never echoes raw file content."""


def sniff_kind(head: bytes, *, claimed_extension: str) -> DocumentKind:
    """Decide the document kind from leading bytes. `head` should be >= 512 bytes
    when available. Extension breaks the CSV/ambiguous-text tie only."""
    if head.startswith(b"%PDF-"):
        return DocumentKind.PDF
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return DocumentKind.PNG
    if head.startswith(b"\xff\xd8\xff"):
        return DocumentKind.JPEG
    if head.startswith(b"PK\x03\x04"):
        # zip container; only xlsx is allowed among zip formats
        return DocumentKind.XLSX
    # CSV has no magic: require decodable text without NUL bytes
    if b"\x00" in head:
        raise FileValidationError("File type not recognized or not allowed.")
    try:
        head.decode("utf-8")
    except UnicodeDecodeError:
        try:
            head.decode("latin-1")
        except UnicodeDecodeError:
            raise FileValidationError(
                "File type not recognized or not allowed."
            ) from None
    if claimed_extension.lower() not in _EXTENSIONS[DocumentKind.CSV]:
        raise FileValidationError(
            "Plain-text uploads must be .csv files."
        )
    return DocumentKind.CSV


@dataclass(frozen=True, slots=True)
class ValidatedUpload:
    kind: DocumentKind
    safe_filename: str  # sanitized for storage/display metadata
    storage_key: str  # server-generated; the ONLY thing used as a path


def validate_upload(
    *,
    original_filename: str,
    content_type: str | None,
    head: bytes,
    byte_size: int,
    max_bytes: int,
) -> ValidatedUpload:
    if byte_size <= 0:
        raise FileValidationError("The uploaded file is empty.")
    if byte_size > max_bytes:
        raise FileValidationError("The uploaded file exceeds the size limit.")

    safe_name = sanitize_filename(original_filename)
    extension = _extension_of(safe_name)
    kind = sniff_kind(head, claimed_extension=extension)

    if extension not in _EXTENSIONS[kind]:
        raise FileValidationError(
            f"The file extension does not match its content ({kind.value})."
        )
    if content_type is not None and content_type.split(";")[0].strip().lower() not in (
        _CONTENT_TYPES[kind]
    ):
        raise FileValidationError(
            f"The declared content type does not match the file content ({kind.value})."
        )

    storage_key = f"{uuid.uuid4().hex}{_EXTENSIONS[kind][0]}"
    return ValidatedUpload(kind=kind, safe_filename=safe_name, storage_key=storage_key)


def sanitize_filename(raw: str) -> str:
    """Reduce an untrusted filename to a safe display/metadata form.

    Never used as a filesystem path — storage keys are server-generated — but
    sanitized anyway for defense in depth and safe rendering.
    """
    # strip any path components (both separators) and null/control chars
    name = raw.replace("\\", "/").split("/")[-1]
    name = unicodedata.normalize("NFKC", name)
    name = "".join(ch for ch in name if ch.isprintable())
    name = re.sub(r"[^\w.\- ()\[\]]", "_", name, flags=re.UNICODE).strip(" .")
    if not name or name.startswith("."):
        raise FileValidationError("The filename is not acceptable.")
    if len(name) > MAX_FILENAME_LENGTH:
        stem, dot, ext = name.rpartition(".")
        keep = MAX_FILENAME_LENGTH - (len(ext) + 1 if dot else 0)
        name = (stem[:keep] + dot + ext) if dot else name[:MAX_FILENAME_LENGTH]
    return name


def _extension_of(name: str) -> str:
    idx = name.rfind(".")
    return name[idx:].lower() if idx >= 0 else ""
