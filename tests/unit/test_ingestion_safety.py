"""File intake: what the system refuses, and why.

These are the security tests for the ingestion boundary. Each one corresponds
to an entry in docs/threat-model.md.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from iep.domain.enums import MediaKind
from iep.ingestion.service import safe_display_name
from iep.ingestion.sniff import CorruptFileError, UnsupportedMediaError, guess_from_name, sniff
from iep.storage.local import LocalObjectStore, content_digest, key_for

MAX_DECOMPRESSED = 10 * 1024 * 1024


def _sniff(data: bytes) -> MediaKind:
    return sniff(data, max_decompressed_bytes=MAX_DECOMPRESSED)


class TestSignatureDetection:
    def test_pdf_is_recognised_by_signature(self) -> None:
        assert _sniff(b"%PDF-1.7\nrest") is MediaKind.PDF

    def test_png_is_recognised_by_signature(self) -> None:
        assert _sniff(b"\x89PNG\r\n\x1a\nrest") is MediaKind.PNG

    def test_jpeg_is_recognised_by_signature(self) -> None:
        assert _sniff(b"\xff\xd8\xff\xe0rest") is MediaKind.JPEG

    def test_the_extension_does_not_decide(self) -> None:
        # A .pdf full of text is not a PDF, whatever it is called.
        with pytest.raises(UnsupportedMediaError):
            _sniff(b"this is plain text pretending to be a document")

    def test_empty_payload_is_corrupt(self) -> None:
        with pytest.raises(CorruptFileError):
            _sniff(b"")

    def test_extension_is_recorded_but_not_trusted(self) -> None:
        assert guess_from_name("factura.PDF") == "pdf"
        assert guess_from_name("noextension") is None


class TestZipHandling:
    def _workbook(self, extra: dict[str, bytes] | None = None) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("xl/workbook.xml", "<workbook/>")
            for name, payload in (extra or {}).items():
                archive.writestr(name, payload)
        return buffer.getvalue()

    def test_a_real_workbook_is_accepted(self) -> None:
        assert _sniff(self._workbook()) is MediaKind.XLSX

    def test_a_zip_that_is_not_a_workbook_is_refused(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("word/document.xml", "<document/>")
        with pytest.raises(UnsupportedMediaError):
            _sniff(buffer.getvalue())

    def test_macro_enabled_workbook_is_refused(self) -> None:
        data = self._workbook({"xl/vbaProject.bin": b"\x00\x01"})
        with pytest.raises(UnsupportedMediaError, match="macro"):
            _sniff(data)

    def test_decompression_bomb_is_refused_before_expansion(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("xl/workbook.xml", "<workbook/>")
            archive.writestr("xl/bomb.xml", b"\x00" * (2 * 1024 * 1024))
        with pytest.raises(UnsupportedMediaError, match="uncompressed size"):
            sniff(buffer.getvalue(), max_decompressed_bytes=1024)

    def test_truncated_zip_is_corrupt(self) -> None:
        data = self._workbook()[:40]
        with pytest.raises(CorruptFileError):
            _sniff(data)


class TestFilenameHandling:
    @pytest.mark.parametrize(
        ("supplied", "expected"),
        [
            ("../../etc/passwd", "passwd"),
            ("..\\..\\windows\\system32\\config", "config"),
            ("/absolute/path/factura.pdf", "factura.pdf"),
            ("normal.pdf", "normal.pdf"),
            ("...", "unnamed"),
            ("", "unnamed"),
        ],
    )
    def test_a_filename_never_becomes_a_path(self, supplied: str, expected: str) -> None:
        assert safe_display_name(supplied) == expected

    def test_control_characters_are_stripped(self) -> None:
        assert "\n" not in safe_display_name("in\nvoice.pdf")

    def test_length_is_capped(self) -> None:
        assert len(safe_display_name("a" * 900 + ".pdf")) <= 255


class TestObjectStore:
    def test_key_is_derived_from_content_only(self) -> None:
        digest = content_digest(b"hello")
        assert key_for(digest) == f"{digest[:2]}/{digest[2:4]}/{digest}"

    def test_a_non_digest_key_is_refused(self) -> None:
        with pytest.raises(ValueError, match="64 lowercase hex"):
            key_for("../../../etc/passwd")

    def test_traversal_in_a_key_is_refused(self, tmp_path: Path) -> None:
        store = LocalObjectStore(tmp_path)
        with pytest.raises(ValueError, match="malformed storage key"):
            store.get("../../secret")

    def test_storing_the_same_bytes_twice_is_one_object(self, tmp_path: Path) -> None:
        store = LocalObjectStore(tmp_path)
        digest = content_digest(b"payload")
        first = store.put(digest, b"payload")
        second = store.put(digest, b"payload")
        assert first == second
        assert store.get(first) == b"payload"
        assert len(list(tmp_path.rglob("*"))) == 3  # two fan-out dirs plus the object

    def test_no_partial_files_are_left_behind(self, tmp_path: Path) -> None:
        store = LocalObjectStore(tmp_path)
        store.put(content_digest(b"x"), b"x")
        assert not list(tmp_path.rglob("*.part"))
