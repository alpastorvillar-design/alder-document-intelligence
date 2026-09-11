"""The comparison the filed report rests on.

The report's first table is the check a person does by hand: for each concepto
de gasto, what the memoria declares against what the supporting documents add
up to. Everything else in the artefact is evidence for those three rows, so
they are worth pinning down on their own - including the case where a figure
is missing, which must not be allowed to look like a match.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from iep.api import vocabulary as vocab
from iep.db.models import Dossier, Extraction
from iep.domain.enums import DossierStatus, FieldStatus
from iep.reporting.render import reconcile


def dossier(claimed: str) -> Dossier:
    return Dossier(
        id=uuid.uuid4(),
        reference="INN-2025-999",
        title="Expediente de prueba",
        period_start=date(2025, 1, 1),
        period_end=date(2025, 12, 31),
        claimed_total_eur=Decimal(claimed),
        status=DossierStatus.NEEDS_REVIEW,
    )


def extraction(field_path: str, amount: str) -> Extraction:
    return Extraction(
        id=uuid.uuid4(),
        dossier_id=uuid.uuid4(),
        document_id=None,
        field_path=field_path,
        value_number=Decimal(amount),
        locator={"kind": "DERIVED", "rule": "test", "inputs": []},
        method="test",
        extractor_version="test/1.0.0",
        contract_version="1.0.0",
        confidence=Decimal("1.0"),
        status=FieldStatus.EXTRACTED,
    )


def by_concept(rows: list[Any]) -> dict[str, Any]:
    return {row.concept: row for row in rows}


def extraction_on(field_path: str, document_id: uuid.UUID | None) -> Extraction:
    row = extraction(field_path, "1.00")
    row.document_id = document_id
    return row


class TestTheDifferenceIsArithmetic:
    def test_a_descuadre_is_the_subtraction_and_says_so(self) -> None:
        rows = by_concept(
            reconcile(
                dossier("85000.00"),
                [
                    extraction("report.declared_personnel_cost_eur", "31500.00"),
                    extraction("timesheet.total_amount_eur", "23777.60"),
                ],
            )
        )
        personnel = rows["Gastos de personal"]
        assert personnel.declared == Decimal("31500.00")
        assert personnel.supported == Decimal("23777.60")
        assert personnel.difference == Decimal("7722.40")
        assert personnel.state == "descuadre"

    def test_equal_figures_cuadran(self) -> None:
        rows = by_concept(
            reconcile(
                dossier("40000.00"),
                [
                    extraction("report.declared_external_cost_eur", "12000.00"),
                    extraction("invoices.total_eur", "12000.00"),
                ],
            )
        )
        external = rows["Colaboraciones externas"]
        assert external.difference == Decimal("0")
        assert external.state == "cuadra"

    def test_the_cents_are_not_rounded_away(self) -> None:
        """A one-cent descuadre is still a descuadre."""
        rows = by_concept(
            reconcile(
                dossier("40000.00"),
                [
                    extraction("report.declared_external_cost_eur", "12000.01"),
                    extraction("invoices.total_eur", "12000.00"),
                ],
            )
        )
        external = rows["Colaboraciones externas"]
        assert external.difference == Decimal("0.01")
        assert external.state == "descuadre"

    def test_the_total_is_checked_against_what_the_entity_claims(self) -> None:
        """The declared total's counterpart is the cuenta justificativa, not a
        figure read from a document, so it has no evidence link and says why."""
        rows = by_concept(
            reconcile(
                dossier("85000.00"),
                [extraction("report.declared_total_eur", "83500.00")],
            )
        )
        total = rows["Total del proyecto"]
        assert total.supported == Decimal("85000.00")
        assert total.supported_source is None
        assert total.difference == Decimal("-1500.00")
        assert total.state == "descuadre"


class TestAMissingFigureStaysMissing:
    def test_nothing_read_is_not_zero(self) -> None:
        """Substituting zero would turn a missing figure into a descuadre of
        the whole amount, or - worse, when both sides are missing - into a
        match. Neither is true: the answer is that it could not be checked."""
        rows = by_concept(reconcile(dossier("85000.00"), []))
        personnel = rows["Gastos de personal"]
        assert personnel.declared is None
        assert personnel.supported is None
        assert personnel.difference is None
        assert personnel.state == "incompleto"

    def test_one_side_missing_is_still_incomplete(self) -> None:
        rows = by_concept(
            reconcile(
                dossier("85000.00"),
                [extraction("report.declared_personnel_cost_eur", "31500.00")],
            )
        )
        personnel = rows["Gastos de personal"]
        assert personnel.declared == Decimal("31500.00")
        assert personnel.supported is None
        assert personnel.state == "incompleto"

    def test_every_concept_appears_even_with_no_data(self) -> None:
        """A concepto de gasto that silently vanished from the table would read
        as "nothing to justify here"."""
        rows = reconcile(dossier("0.00"), [])
        assert [row.concept for row in rows] == [
            concept for concept, _, _, _ in vocab.RECONCILIATION
        ]


class TestTheFiguresCarryTheirSource:
    def test_each_side_keeps_the_extraction_it_came_from(self) -> None:
        """That reference is what lets the report link a figure to the page,
        cell or box it was read from instead of asking for trust."""
        declared = extraction("report.declared_personnel_cost_eur", "31500.00")
        supported = extraction("timesheet.total_amount_eur", "23777.60")
        rows = by_concept(reconcile(dossier("85000.00"), [declared, supported]))
        personnel = rows["Gastos de personal"]
        assert personnel.declared_source is declared
        assert personnel.supported_source is supported

    def test_the_supported_side_is_labelled_in_spanish(self) -> None:
        rows = by_concept(
            reconcile(
                dossier("85000.00"),
                [extraction("timesheet.total_amount_eur", "23777.60")],
            )
        )
        label = rows["Gastos de personal"].supported_label
        assert label == vocab.field_label("timesheet.total_amount_eur")
        assert "timesheet" not in label


class TestTwoEvidenceLinksAreNeverTheSameWord:
    """`DUPLICATE_INVOICE_NUMBER` points at the same field on two documents.

    Both links read "Número de factura", which told a reviewer nothing about
    which one they were about to open.
    """

    def test_a_repeated_label_gains_the_document_it_belongs_to(self) -> None:
        first, second = uuid.uuid4(), uuid.uuid4()
        rows = [
            extraction_on("invoice.number", first),
            extraction_on("invoice.number", second),
        ]
        names = {str(first): "justificante-01.jpg", str(second): "justificante-02.jpg"}
        labels = [label for _, label in vocab.evidence_links(rows, names)]
        assert labels == [
            "Número de factura · justificante-01.jpg",
            "Número de factura · justificante-02.jpg",
        ]
        assert len(set(labels)) == 2

    def test_a_label_that_appears_once_is_left_alone(self) -> None:
        """A filename appended to every link would be noise."""
        document = uuid.uuid4()
        rows = [
            extraction_on("invoice.number", document),
            extraction_on("invoice.total_eur", document),
        ]
        names = {str(document): "justificante-01.jpg"}
        labels = [label for _, label in vocab.evidence_links(rows, names)]
        assert labels == [
            vocab.field_label("invoice.number"),
            vocab.field_label("invoice.total_eur"),
        ]
        assert all("justificante" not in label for label in labels)

    def test_a_derived_value_has_no_document_to_name(self) -> None:
        """Nothing to disambiguate with is not a crash, and not the word None."""
        rows = [
            extraction_on("timesheet.total_amount_eur", None),
            extraction_on("timesheet.total_amount_eur", None),
        ]
        labels = [label for _, label in vocab.evidence_links(rows, {})]
        assert labels == [vocab.field_label("timesheet.total_amount_eur")] * 2
        assert all("None" not in label for label in labels)

    def test_the_rows_come_back_in_order_and_unchanged(self) -> None:
        rows = [
            extraction_on("invoice.total_eur", uuid.uuid4()),
            extraction_on("invoice.issue_date", uuid.uuid4()),
        ]
        assert [row for row, _ in vocab.evidence_links(rows, {})] == rows


class TestAnAmountNeverAppearsWithoutItsUnit:
    """`400.000,00` beside a date and a code is an unlabelled number.

    The unit comes from the field path, so there is no second table to fall
    out of step with the first.
    """

    def test_euros_and_hours_are_recognised(self) -> None:
        assert vocab.unit_of("call.max_funding_eur") == "€"
        assert vocab.unit_of("invoice.total_eur") == "€"
        assert vocab.unit_of("timesheet.rows[0].hourly_rate_eur") == "€"
        assert vocab.unit_of("timesheet.rows[0].hours") == "h"

    def test_a_code_a_date_and_a_count_get_nothing(self) -> None:
        for path in (
            "call.code",
            "report.period_start",
            "invoices.count",
            "timesheet.row_count",
            "invoice.supplier_name",
        ):
            assert vocab.unit_of(path) == "", path

    def test_every_money_field_in_the_catalogue_has_a_unit(self) -> None:
        """Read from the label table, so a field added later is covered."""
        money_paths = [p for p in vocab._FIELD_LABELS if p.endswith("_eur")]
        assert money_paths
        for path in money_paths:
            assert vocab.unit_of(path) == "€", path


class TestWhatTheReportLeadsWith:
    """Ninety-three rows of "read correctly, 98 %" say nothing the summary
    above them does not. The fields worth a reader's attention are the ones
    the machine could not read confidently and the ones a person has already
    touched, so those get their own section before the appendix."""

    def picked(self, rows: list[Extraction]) -> list[str]:
        from iep.reporting.render import _needs_attention

        return [row.field_path for row in _needs_attention(rows)]

    def test_a_field_read_and_accepted_is_not_in_it(self) -> None:
        row = extraction("report.declared_total_eur", "100.00")
        assert self.picked([row]) == []

    def test_a_field_that_needs_review_is(self) -> None:
        row = extraction("invoice.base_eur", "100.00")
        row.status = FieldStatus.NEEDS_REVIEW
        assert self.picked([row]) == ["invoice.base_eur"]

    def test_a_confirmed_field_is_too(self) -> None:
        """Somebody signed it off against the document, which is a decision
        worth showing next to the one that needed making."""
        row = extraction("invoice.total_eur", "100.00")
        row.status = FieldStatus.CONFIRMED
        assert self.picked([row]) == ["invoice.total_eur"]

    def test_a_corrected_field_is_in_it_whatever_its_status_says(self) -> None:
        """The original reading is kept beside the correction, so the presence
        of one is the signal - not the status column."""
        row = extraction("timesheet.rows[0].hours", "120.00")
        row.original_value_text = "12"
        assert self.picked([row]) == ["timesheet.rows[0].hours"]

    def test_what_needs_deciding_comes_before_what_was_decided(self) -> None:
        needs = extraction("a.one", "1.00")
        needs.status = FieldStatus.NEEDS_REVIEW
        confirmed = extraction("a.two", "2.00")
        confirmed.status = FieldStatus.CONFIRMED
        corrected = extraction("a.three", "3.00")
        corrected.status = FieldStatus.CORRECTED

        assert self.picked([confirmed, corrected, needs]) == ["a.one", "a.three", "a.two"]

    def test_the_least_confident_reading_is_first(self) -> None:
        """Within a group, the row a reviewer should look at hardest."""
        sure = extraction("a.sure", "1.00")
        sure.status = FieldStatus.NEEDS_REVIEW
        sure.confidence = Decimal("0.74")
        unsure = extraction("a.unsure", "1.00")
        unsure.status = FieldStatus.NEEDS_REVIEW
        unsure.confidence = Decimal("0.41")

        assert self.picked([sure, unsure]) == ["a.unsure", "a.sure"]
