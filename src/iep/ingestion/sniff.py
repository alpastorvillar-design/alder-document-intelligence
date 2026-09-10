"""Decide what a file actually is.

The declared content type and the filename extension are recorded because they
are useful provenance, but neither is trusted. A file is what its signature and
its parser say it is, and anything that fails to open is rejected before it
reaches the pipeline.
"""

from __future__ import annotations

import io
import zipfile

from iep.domain.enums import MediaKind

# Magic numbers, checked against the head of the payload.
_SIGNATURES: tuple[tuple[bytes, MediaKind], ...] = (
    (b"%PDF-", MediaKind.PDF),
    (b"\x89PNG\r\n\x1a\n", MediaKind.PNG),
    (b"\xff\xd8\xff", MediaKind.JPEG),
)

# Office Open XML is a zip. The signature alone cannot distinguish a workbook
# from a document or from a zip bomb, so the container is inspected.
_ZIP_SIGNATURES = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")

_XLSX_MARKER = "xl/workbook.xml"


class UnsupportedMediaError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class CorruptFileError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def sniff(data: bytes, *, max_decompressed_bytes: int) -> MediaKind:
    """Classify `data`, raising rather than guessing when it is not supported."""
    if not data:
        raise CorruptFileError("el fichero está vacío")

    for signature, kind in _SIGNATURES:
        if data.startswith(signature):
            return kind

    if data.startswith(_ZIP_SIGNATURES):
        return _sniff_zip(data, max_decompressed_bytes=max_decompressed_bytes)

    # Naming what is accepted matters more than naming what was refused. The
    # person reading this has just dropped a folder onto the intake screen and
    # needs to know which of its files to drop instead - "unrecognised file
    # signature" on its own is accurate and tells them nothing.
    raise UnsupportedMediaError(
        "la firma del fichero no corresponde a ninguno de los formatos aceptados: "
        "PDF, escaneo PNG o JPEG, o libro .xlsx"
    )


def _sniff_zip(data: bytes, *, max_decompressed_bytes: int) -> MediaKind:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if archive.testzip() is not None:
                raise CorruptFileError("el contenedor zip tiene un miembro dañado")
            names = archive.namelist()
            # Decompression bomb guard: refuse before any member is expanded.
            declared = sum(info.file_size for info in archive.infolist())
            if declared > max_decompressed_bytes:
                raise UnsupportedMediaError(
                    f"el tamaño descomprimido que declara ({declared} bytes) supera el "
                    f"límite de {max_decompressed_bytes} bytes"
                )
            # An encrypted member cannot be parsed and must not be stored as if
            # it were readable.
            if any(info.flag_bits & 0x1 for info in archive.infolist()):
                raise UnsupportedMediaError("el archivo contiene miembros cifrados")
            if _XLSX_MARKER in names:
                if any(
                    name.startswith("xl/macrosheets/") or name.endswith(".bin") for name in names
                ):
                    raise UnsupportedMediaError("el libro contiene macros")
                return MediaKind.XLSX
    except zipfile.BadZipFile as exc:
        raise CorruptFileError(f"no es un contenedor zip legible: {exc}") from exc

    raise UnsupportedMediaError("el contenedor zip no es un libro de los admitidos")


def guess_from_name(filename: str) -> str | None:
    """Extension of the caller's filename. Recorded as provenance only."""
    _, _, ext = filename.rpartition(".")
    return ext.lower() if ext and ext != filename else None
