"""Two readers, two files, and the one that was shipped suited neither.

The export was commas and UTF-8 with no byte order mark - RFC 4180, which is
what `csv.reader`, pandas, R and DuckDB expect. Double-clicked into Excel on
an install whose list separator is a semicolon, every row arrived in column A,
which is what somebody downloading it from the review screen actually did.

Both files are now produced. The hack this avoids is a `sep=;` first line:
Excel understands it and nothing else does, so it makes the standard file
unreadable to standard parsers in order to fix a locale problem.
"""

from __future__ import annotations

import csv
import io

import pytest

from iep.api.routes.ui import ORIGINAL_MEDIA, original_opens_inline
from iep.domain.enums import MediaKind
from iep.reporting.render import CSV_DIALECTS

BOM = b"\xef\xbb\xbf"


def columns(data: bytes, delimiter: str) -> int:
    text = data.decode("utf-8-sig")
    return len(next(csv.reader(io.StringIO(text), delimiter=delimiter)))


class TestTheTwoDialects:
    """Exercised against a hand-built file rather than the database, so the
    shape is asserted without standing up a dossier."""

    def test_both_names_are_offered(self) -> None:
        assert CSV_DIALECTS == ("rfc4180", "excel")

    @pytest.mark.parametrize(
        ("sample", "delimiter", "bom"),
        [
            (b"a,b,c\n1,2,3\n", ",", False),
            (BOM + b"a;b;c\r\n1;2;3\r\n", ";", True),
        ],
    )
    def test_each_shape_parses_for_its_own_reader(
        self, sample: bytes, delimiter: str, bom: bool
    ) -> None:
        assert sample.startswith(BOM) is bom
        assert columns(sample, delimiter) == 3

    def test_a_comma_file_collapses_in_a_semicolon_reader(self) -> None:
        """The defect, stated as a test: this is what a reviewer saw."""
        assert columns(b"a,b,c\n1,2,3\n", ";") == 1

    def test_a_semicolon_file_collapses_in_a_comma_reader(self) -> None:
        """And why the standard file has to stay the default: the Excel one is
        a single column to pandas."""
        assert columns(BOM + b"a;b;c\r\n1;2;3\r\n", ",") == 1


class TestTheButtonCannotLieAboutWhatHappens:
    def test_every_kind_agrees_with_its_own_header(self) -> None:
        """The label is derived from the disposition table rather than written
        beside it, because a second list drifts from the first."""
        for kind, (_, disposition) in ORIGINAL_MEDIA.items():
            assert original_opens_inline(kind) is (disposition == "inline"), kind

    def test_a_workbook_is_handed_over_rather_than_shown(self) -> None:
        """No browser renders a .xlsx, so promising "abrir" made a correct
        download look like a fault."""
        assert original_opens_inline(MediaKind.XLSX) is False

    def test_a_pdf_and_a_scan_are_shown(self) -> None:
        assert original_opens_inline(MediaKind.PDF) is True
        assert original_opens_inline(MediaKind.JPEG) is True

    def test_a_kind_with_no_entry_is_not_promised_to_open(self) -> None:
        assert original_opens_inline("SOMETHING_ELSE") is False
        assert original_opens_inline(MediaKind.UNSUPPORTED) is False
