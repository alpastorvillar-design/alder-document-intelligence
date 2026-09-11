"""The order fields are read in, and the sections that repeat per document.

Three complaints from somebody actually using the screen, and all three were
the same kind of defect - a table that is technically complete and unreadable.

"Fin del periodo" sat above "Inicio del periodo" and the project title was in
the middle of the memoria, because nothing ordered the rows and the database
returned them alphabetically by label.

"Facturas y justificantes" listed sixteen fields flat: two "Base imponible",
two "Total de la factura", two of everything, with no way to tell which
belonged to which invoice. That is the one question a reviewer is there to
answer.

"Parte horario" prefixed every label with "Parte horario, fila 1 · ", nine
times per person, which pushed the actual field name off the readable part of
the column.
"""

from __future__ import annotations

from typing import Any

from iep.api import vocabulary as vocab


class Row:
    """The parts of an extraction these functions read."""

    def __init__(
        self,
        field_path: str,
        document_id: str = "doc",
        value_text: str = "",
        value_number: float | None = None,
    ) -> None:
        self.field_path = field_path
        self.document_id = document_id
        self.value_text = value_text
        self.value_number = value_number
        self.value_date = None


def paths(rows: list[Any]) -> list[str]:
    return [row.field_path for row in rows]


class TestTheOrderAFieldIsReadIn:
    def test_the_memoria_reads_identity_then_time_then_money(self) -> None:
        scrambled = [
            Row("report.declared_total_eur"),
            Row("report.period_end"),
            Row("report.title"),
            Row("report.period_start"),
            Row("report.call_code"),
            Row("report.declared_personnel_cost_eur"),
            Row("report.project_code"),
            Row("report.declared_external_cost_eur"),
            Row("report.period"),
        ]
        ((_, _, rows),) = vocab.group_extractions(scrambled)

        assert paths(rows) == [
            "report.title",
            "report.project_code",
            "report.call_code",
            "report.period",
            "report.period_start",
            "report.period_end",
            "report.declared_personnel_cost_eur",
            "report.declared_external_cost_eur",
            "report.declared_total_eur",
        ]

    def test_a_total_comes_after_the_figures_it_adds_up(self) -> None:
        """So a reviewer can watch the sum land instead of hunting for it."""
        order = paths(
            vocab.group_extractions(
                [
                    Row("report.declared_total_eur"),
                    Row("report.declared_personnel_cost_eur"),
                    Row("report.declared_external_cost_eur"),
                ]
            )[0][2]
        )
        assert order.index("report.declared_total_eur") == 2

    def test_an_invoice_reads_in_the_order_it_is_printed(self) -> None:
        rows = vocab.group_extractions(
            [
                Row("invoice.total_eur"),
                Row("invoice.base_eur"),
                Row("invoice.supplier_name"),
                Row("invoice.number"),
                Row("invoice.issue_date"),
            ]
        )[0][2]
        assert paths(rows) == [
            "invoice.number",
            "invoice.issue_date",
            "invoice.supplier_name",
            "invoice.base_eur",
            "invoice.total_eur",
        ]

    def test_a_field_nobody_ordered_sorts_last_rather_than_vanishing(self) -> None:
        """A field added to the pipeline has to appear on the screen before
        somebody decides where it belongs."""
        rows = vocab.group_extractions(
            [
                Row("report.declared_total_eur"),
                Row("report.brand_new_field"),
                Row("report.title"),
            ]
        )[0][2]
        assert paths(rows) == [
            "report.title",
            "report.declared_total_eur",
            "report.brand_new_field",
        ]

    def test_a_timesheet_row_reads_who_when_then_how_much(self) -> None:
        rows = vocab.group_extractions(
            [
                Row("timesheet.rows[0].amount_eur"),
                Row("timesheet.rows[0].hours"),
                Row("timesheet.rows[0].full_name"),
                Row("timesheet.rows[0].hourly_rate_eur"),
                Row("timesheet.rows[0].month"),
            ]
        )[0][2]
        assert paths(rows) == [
            "timesheet.rows[0].full_name",
            "timesheet.rows[0].month",
            "timesheet.rows[0].hours",
            "timesheet.rows[0].hourly_rate_eur",
            "timesheet.rows[0].amount_eur",
        ]

    def test_row_ten_comes_after_row_two(self) -> None:
        """Sorted as text, `rows[10]` lands between `rows[1]` and `rows[2]`."""
        rows = vocab.group_extractions(
            [
                Row("timesheet.rows[10].hours"),
                Row("timesheet.rows[2].hours"),
                Row("timesheet.rows[1].hours"),
            ]
        )[0][2]
        assert paths(rows) == [
            "timesheet.rows[1].hours",
            "timesheet.rows[2].hours",
            "timesheet.rows[10].hours",
        ]


class TestOneBlockPerDocument:
    def test_each_invoice_becomes_its_own_subsection(self) -> None:
        rows = [
            Row("invoice.base_eur", "b", value_number=8750.0),
            Row("invoice.number", "b", value_text="FS-2025-0588"),
            Row("invoice.base_eur", "a", value_number=12400.0),
            Row("invoice.number", "a", value_text="FS-2025-0417"),
        ]
        names = {"a": "justificante-01.jpg", "b": "justificante-02.jpg"}

        sections = vocab.subdivide(vocab.INVOICES, rows, names)

        assert [section.label for section in sections] == [
            "Factura FS-2025-0417",
            "Factura FS-2025-0588",
        ]
        assert sections[0].detail == "justificante-01.jpg"
        # And the figure now travels with the invoice it belongs to.
        assert [row.value_number for row in sections[0].rows if row.value_number] == [12400.0]

    def test_an_invoice_whose_number_could_not_be_read_still_appears(self) -> None:
        """Saying "this one is unreadable" is the screen's job, not hiding it."""
        sections = vocab.subdivide(vocab.INVOICES, [Row("invoice.base_eur", "a")], {})
        assert sections[0].label == "Factura sin número legible"
        assert len(sections[0].rows) == 1

    def test_a_timesheet_row_is_named_by_whose_hours_they_are(self) -> None:
        rows = [
            Row("timesheet.rows[1].full_name", value_text="Nerea Talvi"),
            Row("timesheet.rows[1].month", value_text="2025-03"),
            Row("timesheet.rows[0].full_name", value_text="Nerea Talvi"),
            Row("timesheet.rows[0].month", value_text="2025-02"),
        ]

        sections = vocab.subdivide(vocab.TIMESHEET, rows)

        # Same person twice, told apart by the month - which is why the detail
        # exists at all.
        assert [(s.label, s.detail) for s in sections] == [
            ("Nerea Talvi", "2025-02"),
            ("Nerea Talvi", "2025-03"),
        ]

    def test_a_row_without_a_name_falls_back_to_its_position(self) -> None:
        sections = vocab.subdivide(vocab.TIMESHEET, [Row("timesheet.rows[3].hours")])
        assert sections[0].label == "Fila 4"

    def test_a_registry_record_is_named_by_the_person(self) -> None:
        rows = [
            Row("registry.personnel[EMP-0142].full_name", value_text="Nerea Talvi"),
            Row("registry.personnel[EMP-0142].role", value_text="Investigadora"),
        ]
        sections = vocab.subdivide(vocab.REGISTRY, rows)
        assert sections[0].label == "Nerea Talvi"
        assert sections[0].detail == "EMP-0142"

    def test_a_group_that_does_not_repeat_gets_no_heading(self) -> None:
        """An empty label is how the template knows not to draw one."""
        sections = vocab.subdivide("Memoria técnica", [Row("report.title")])
        assert len(sections) == 1
        assert sections[0].label == ""

    def test_the_flat_order_is_the_nested_order_flattened(self) -> None:
        """The screen nests and the report does not, so they can disagree.

        Deriving one from the other is what stops that: whatever the screen
        shows under a heading, the report lists in the same sequence.
        """
        rows = [
            Row("invoice.base_eur", "b", value_number=8750.0),
            Row("invoice.number", "b", value_text="FS-2025-0588"),
            Row("invoice.total_eur", "a", value_number=15004.0),
            Row("invoice.number", "a", value_text="FS-2025-0417"),
        ]
        ((title, _, flat),) = vocab.group_extractions(rows)
        nested = [row for section in vocab.subdivide(title, rows) for row in section.rows]
        assert paths(flat) == paths(nested)


class TestTheLabelInsideASubsection:
    def test_the_row_prefix_is_dropped(self) -> None:
        path = "timesheet.rows[0].amount_eur"
        assert vocab.field_label(path) == "Parte horario, fila 1 · Importe imputado"
        assert vocab.field_label_short(path) == "Importe imputado"

    def test_a_registry_prefix_is_dropped_too(self) -> None:
        path = "registry.personnel[EMP-0142].role"
        assert "EMP-0142" in vocab.field_label(path)
        assert vocab.field_label_short(path) == "Categoría"

    def test_an_ordinary_field_is_unchanged(self) -> None:
        assert vocab.field_label_short("report.title") == "Título del proyecto"


class TestWhereACallPageValueCameFrom:
    def test_the_heading_on_the_page_is_named_rather_than_the_attribute(self) -> None:
        """`campo «eligible-from»` is precise and unreadable. A reviewer
        verifying the figure looks for a heading, not a data attribute."""
        locator = {
            "kind": "HTML_SELECTOR",
            "selector": '[data-field="eligible-from"]',
        }
        assert vocab.locator_summary(locator) == (
            "Página publicada · apartado «Inicio del periodo elegible»"
        )

    def test_an_unmapped_field_still_says_something(self) -> None:
        locator = {"kind": "HTML_SELECTOR", "selector": '[data-field="new-thing"]'}
        assert vocab.locator_summary(locator) == "Página publicada · apartado «new-thing»"
