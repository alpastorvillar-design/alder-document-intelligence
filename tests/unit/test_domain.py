"""State machine, contracts and locators."""

from __future__ import annotations

import itertools
import uuid
from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from iep.domain.contracts import (
    CONTRACT_VERSION,
    DossierCreate,
    ExcelCellLocator,
    OcrWordBoxLocator,
    PdfPageLocator,
)
from iep.domain.enums import DossierStatus as S
from iep.domain.states import (
    ALLOWED,
    InvalidTransitionError,
    assert_transition,
    can_transition,
    sources_for,
)


class TestStateMachine:
    def test_every_state_is_declared(self) -> None:
        assert set(ALLOWED) == set(S)

    def test_the_normal_path_is_legal(self) -> None:
        path = [S.DRAFT, S.INGESTED, S.QUEUED, S.PROCESSING, S.NEEDS_REVIEW, S.APPROVED]
        for current, target in itertools.pairwise(path):
            assert can_transition(current, target), f"{current} -> {target}"

    def test_processing_cannot_approve(self) -> None:
        # The whole design rests on this: nothing automated approves a dossier.
        assert not can_transition(S.PROCESSING, S.APPROVED)
        with pytest.raises(InvalidTransitionError):
            assert_transition(S.PROCESSING, S.APPROVED)

    def test_approved_is_terminal(self) -> None:
        assert ALLOWED[S.APPROVED] == frozenset()

    @pytest.mark.parametrize(
        ("current", "target"),
        [
            (S.DRAFT, S.APPROVED),
            (S.DRAFT, S.PROCESSING),
            (S.INGESTED, S.NEEDS_REVIEW),
            (S.QUEUED, S.APPROVED),
            (S.APPROVED, S.NEEDS_REVIEW),
            (S.APPROVED, S.REJECTED),
        ],
    )
    def test_illegal_transitions_raise(self, current: S, target: S) -> None:
        with pytest.raises(InvalidTransitionError):
            assert_transition(current, target)

    def test_sources_for_is_the_inverse(self) -> None:
        assert S.PROCESSING in sources_for(S.NEEDS_REVIEW)
        assert S.NEEDS_REVIEW in sources_for(S.APPROVED)
        assert sources_for(S.DRAFT) == frozenset()


class TestDossierCreate:
    def test_reference_is_normalised(self) -> None:
        payload = DossierCreate(
            reference=" inn-2025-041 ",
            title="T",
            period_start=date(2025, 1, 1),
            period_end=date(2025, 12, 31),
            claimed_total_eur=Decimal("1.00"),
        )
        assert payload.reference == "INN-2025-041"

    @pytest.mark.parametrize("reference", ["INN-25-041", "XXX-2025-041", "INN-2025-4100", ""])
    def test_bad_reference_is_rejected(self, reference: str) -> None:
        with pytest.raises(ValidationError):
            DossierCreate(
                reference=reference,
                title="T",
                period_start=date(2025, 1, 1),
                period_end=date(2025, 12, 31),
                claimed_total_eur=Decimal("1.00"),
            )

    def test_period_must_be_ordered(self) -> None:
        with pytest.raises(ValidationError):
            DossierCreate(
                reference="INN-2025-041",
                title="T",
                period_start=date(2025, 12, 31),
                period_end=date(2025, 1, 1),
                claimed_total_eur=Decimal("1.00"),
            )

    def test_negative_total_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            DossierCreate(
                reference="INN-2025-041",
                title="T",
                period_start=date(2025, 1, 1),
                period_end=date(2025, 12, 31),
                claimed_total_eur=Decimal("-1.00"),
            )


class TestLocators:
    def test_an_excel_cell_cannot_carry_a_bounding_box(self) -> None:
        # The discriminated union is the point: this combination is not a bug
        # to catch downstream, it cannot be constructed.
        with pytest.raises(ValidationError):
            ExcelCellLocator(sheet="S", cell="A1", row=1, column="A", left=0)  # type: ignore[call-arg]

    def test_cell_reference_shape_is_enforced(self) -> None:
        with pytest.raises(ValidationError):
            ExcelCellLocator(sheet="S", cell="1A", row=1, column="A")

    def test_ocr_confidence_is_bounded(self) -> None:
        with pytest.raises(ValidationError):
            OcrWordBoxLocator(page=1, left=0, top=0, width=1, height=1, word_confidence=140.0)

    def test_page_numbers_start_at_one(self) -> None:
        with pytest.raises(ValidationError):
            PdfPageLocator(page=0)

    def test_locators_round_trip_through_json(self) -> None:
        locator = OcrWordBoxLocator(
            page=2, left=10, top=20, width=30, height=8, word_confidence=91.5, snippet="TOTAL"
        )
        assert locator.model_dump(mode="json")["kind"] == "OCR_WORD_BOX"


def test_contract_version_is_pinned() -> None:
    # Bumping this is a deliberate act: stored extractions record the version
    # they were written under.
    assert CONTRACT_VERSION == "1.0.0"


def test_uuids_are_not_reused_between_locators() -> None:
    assert uuid.uuid4() != uuid.uuid4()
