"""Upload security core tests - magic bytes, filename policy, agreement checks."""

from __future__ import annotations

import pytest

from app.ingestion.file_validation import (
    DocumentKind,
    FileValidationError,
    sanitize_filename,
    sniff_kind,
    validate_upload,
)

PDF_HEAD = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n"
PNG_HEAD = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
JPEG_HEAD = b"\xff\xd8\xff\xe0\x00\x10JFIF"
ZIP_HEAD = b"PK\x03\x04" + b"\x00" * 16
CSV_HEAD = b"internal_part_number,name\nMF-001,Bracket\n"


class TestSniffing:
    def test_magic_bytes_win_over_extension(self) -> None:
        # a PDF renamed to .csv is identified as PDF (and later rejected on
        # extension/content mismatch)
        assert sniff_kind(PDF_HEAD, claimed_extension=".csv") is DocumentKind.PDF

    def test_zip_is_xlsx(self) -> None:
        assert sniff_kind(ZIP_HEAD, claimed_extension=".xlsx") is DocumentKind.XLSX

    def test_text_requires_csv_extension(self) -> None:
        with pytest.raises(FileValidationError):
            sniff_kind(CSV_HEAD, claimed_extension=".txt")

    def test_binary_garbage_rejected(self) -> None:
        with pytest.raises(FileValidationError):
            sniff_kind(b"\x00\x01\x02\x03 garbage", claimed_extension=".csv")


class TestValidateUpload:
    def _ok(self, **overrides: object) -> dict[str, object]:
        base: dict[str, object] = {
            "original_filename": "Quote_Q3 (rev2).pdf",
            "content_type": "application/pdf",
            "head": PDF_HEAD,
            "byte_size": 100_000,
            "max_bytes": 20_000_000,
        }
        base.update(overrides)
        return base

    def test_happy_path_generates_server_side_storage_key(self) -> None:
        result = validate_upload(**self._ok())  # type: ignore[arg-type]
        assert result.kind is DocumentKind.PDF
        assert result.safe_filename == "Quote_Q3 (rev2).pdf"
        assert result.storage_key.endswith(".pdf")
        assert "Quote" not in result.storage_key  # user input never in the key

    def test_extension_content_mismatch_rejected(self) -> None:
        with pytest.raises(FileValidationError, match="extension"):
            validate_upload(**self._ok(original_filename="quote.csv"))  # type: ignore[arg-type]

    def test_content_type_mismatch_rejected(self) -> None:
        with pytest.raises(FileValidationError, match="content type"):
            validate_upload(**self._ok(content_type="image/png"))  # type: ignore[arg-type]

    def test_oversize_rejected(self) -> None:
        with pytest.raises(FileValidationError, match="size"):
            validate_upload(**self._ok(byte_size=30_000_000))  # type: ignore[arg-type]

    def test_empty_rejected(self) -> None:
        with pytest.raises(FileValidationError, match="empty"):
            validate_upload(**self._ok(byte_size=0))  # type: ignore[arg-type]

    def test_csv_with_text_plain_accepted(self) -> None:
        result = validate_upload(
            original_filename="parts.csv",
            content_type="text/plain",
            head=CSV_HEAD,
            byte_size=1000,
            max_bytes=20_000_000,
        )
        assert result.kind is DocumentKind.CSV


class TestFilenamePolicy:
    def test_path_traversal_stripped(self) -> None:
        assert sanitize_filename("../../etc/passwd") == "passwd"
        assert sanitize_filename("..\\..\\windows\\system32.pdf") == "system32.pdf"

    def test_control_chars_and_specials_neutralized(self) -> None:
        assert (
            sanitize_filename("inv\x00oice<script>.pdf") == "invoice_script_.pdf"
        )

    def test_hidden_file_dot_stripped(self) -> None:
        # leading dots are stripped, so the stored name is never a dotfile
        assert sanitize_filename(".htaccess") == "htaccess"

    def test_dot_only_names_rejected(self) -> None:
        with pytest.raises(FileValidationError):
            sanitize_filename("...")

    def test_long_names_truncated_keeping_extension(self) -> None:
        name = sanitize_filename("a" * 500 + ".pdf")
        assert len(name) <= 160
        assert name.endswith(".pdf")

    def test_unicode_normalized_but_preserved(self) -> None:
        assert sanitize_filename("bräcket quote.pdf") == "bräcket quote.pdf"
