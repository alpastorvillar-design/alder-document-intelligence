"""Parsing helpers for Spanish document conventions.

Amounts are written `1.234,56` and dates appear in several shapes. Getting
these wrong by a factor of a thousand is the failure mode that matters here, so
the separators are decided explicitly rather than handed to a locale.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation

_AMOUNT = re.compile(r"(?<![\d.,])(\d{1,3}(?:\.\d{3})*|\d+)(?:,(\d{1,2}))?(?![\d.,])")

# Some exports use a thin or non-breaking space as the thousands separator.
_SPACE_GROUPING = re.compile(
    "(?<=\\d)[\N{NO-BREAK SPACE}\N{NARROW NO-BREAK SPACE} ](?=\\d{3}(?!\\d))"
)
_WHITESPACE_RUN = re.compile("[ \t\N{NO-BREAK SPACE}\N{NARROW NO-BREAK SPACE}]+")

_ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DMY_DATE = re.compile(r"\b(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})\b")

_MONTHS_ES = {
    "enero": 1,
    "febrero": 2,
    "marzo": 3,
    "abril": 4,
    "mayo": 5,
    "junio": 6,
    "julio": 7,
    "agosto": 8,
    "septiembre": 9,
    "setiembre": 9,
    "octubre": 10,
    "noviembre": 11,
    "diciembre": 12,
}
_TEXT_DATE = re.compile(
    r"\b(\d{1,2})\s+de\s+(" + "|".join(_MONTHS_ES) + r")\s+de\s+(\d{4})\b", re.IGNORECASE
)

# OCR routinely turns these into each other inside otherwise numeric runs.
_DIGIT_LOOKALIKES = str.maketrans({"O": "0", "o": "0", "l": "1", "I": "1", "S": "5", "B": "8"})


def parse_amount(text: str) -> Decimal | None:
    """First Spanish-formatted amount in `text`, or None."""
    match = _AMOUNT.search(_SPACE_GROUPING.sub(".", text))
    if match is None:
        return None
    whole = match.group(1).replace(".", "")
    fraction = match.group(2) or "00"
    try:
        return Decimal(f"{whole}.{fraction.ljust(2, '0')}")
    except InvalidOperation:
        return None


def parse_amount_tolerant(text: str) -> Decimal | None:
    """As `parse_amount`, but repairs common OCR digit confusions first.

    Repairing before parsing matters: `1S.20O,00` parses "successfully" as 1,00
    if the strict reader runs first, which is a silent thousand-fold error.
    Only OCR output is passed through here, and the caller lowers confidence
    when the repaired reading is the one that was used.
    """
    repaired = re.sub(
        r"[0-9OoIlSB][0-9OoIlSB.,]{2,}", lambda m: m.group(0).translate(_DIGIT_LOOKALIKES), text
    )
    from_repaired = parse_amount(repaired)
    direct = parse_amount(text)
    if from_repaired is None:
        return direct
    if direct is None:
        return from_repaired
    # Prefer whichever reading consumed more of the numeric run.
    return from_repaired if from_repaired >= direct else direct


def parse_date(text: str) -> date | None:
    iso = _ISO_DATE.search(text)
    if iso:
        return _safe_date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))

    written = _TEXT_DATE.search(text)
    if written:
        return _safe_date(
            int(written.group(3)), _MONTHS_ES[written.group(2).lower()], int(written.group(1))
        )

    dmy = _DMY_DATE.search(text)
    if dmy:
        # Day-first: these documents are Spanish, and a month-first reading
        # would put half the dates in the wrong month without ever failing.
        return _safe_date(int(dmy.group(3)), int(dmy.group(2)), int(dmy.group(1)))
    return None


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def normalise_whitespace(text: str) -> str:
    return _WHITESPACE_RUN.sub(" ", text).strip()


def find_labelled(text: str, label_pattern: str) -> tuple[str, int, int] | None:
    """Value following a label, with its character span in `text`."""
    pattern = re.compile(label_pattern + r"\s*:?\s*(?P<value>[^\n\r]+)", re.IGNORECASE)
    match = pattern.search(text)
    if match is None:
        return None
    value = normalise_whitespace(match.group("value"))
    return value, match.start("value"), match.end("value")
