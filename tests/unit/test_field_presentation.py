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


class TestTheBriefLocator:
    """The filed report lists every field, and one column was 72 characters.

    In the personnel section every row carried
    `API de personal · $.pages[*].items[employee_id=EMP-0142].hourly_rate_eur`
    - which wraps onto three printed lines and repeats, on all 31 rows, the
    employee id that the block heading above them already states. Thirty-one
    rows at three lines each is most of a page.
    """

    def test_an_api_path_keeps_only_the_field_it_names(self) -> None:
        locator = {
            "kind": "API_FIELD",
            "endpoint": "/api/v1/personnel",
            "json_path": "$.pages[*].items[employee_id=EMP-0142].hourly_rate_eur",
        }
        assert vocab.locator_brief(locator) == "API de personal · hourly_rate_eur"
        # And the full path is still available for the screen, where there is
        # room for it and no block heading to lean on.
        assert "employee_id=EMP-0142" in vocab.locator_summary(locator)

    def test_a_path_with_nothing_to_trim_is_left_alone(self) -> None:
        locator = {"kind": "API_FIELD", "endpoint": "/x", "json_path": "$"}
        assert vocab.locator_brief(locator) == "API de personal · $"

    def test_a_cell_drops_the_sheet_the_section_already_names(self) -> None:
        locator = {
            "kind": "EXCEL_CELL",
            "sheet": "Partes horarios",
            "cell": "G5",
            "row": 5,
            "column": "G",
        }
        assert vocab.locator_brief(locator) == "Excel · celda G5"

    def test_every_kind_gets_shorter_or_stays_the_same(self) -> None:
        """A brief form that is longer than the full one is a bug, not a
        shortening."""
        cases = [
            {"kind": "PDF_PAGE", "page": 1, "char_start": 105, "char_end": 168},
            {"kind": "PDF_PAGE", "page": 2},
            {"kind": "OCR_WORD_BOX", "page": 1},
            {"kind": "EXCEL_CELL", "sheet": "Hoja", "cell": "A5", "row": 5, "column": "A"},
            {"kind": "API_FIELD", "endpoint": "/x", "json_path": "$.a[b=c].d"},
            {"kind": "HTML_SELECTOR", "selector": '[data-field="status"]'},
            {"kind": "DERIVED", "inputs": [1, 2, 3]},
        ]
        for locator in cases:
            brief = vocab.locator_brief(locator)
            assert len(brief) <= len(vocab.locator_summary(locator)), locator["kind"]
            assert brief, locator["kind"]

    def test_an_unknown_kind_falls_back_to_the_full_summary(self) -> None:
        locator = {"kind": "SOMETHING_NEW"}
        assert vocab.locator_brief(locator) == vocab.locator_summary(locator)


class TestALongPathBreaksWhereAReaderExpects:
    """A captured URL is one unbreakable token in a fixed-width column.

    With nothing allowed to break it, it ran out of the Origen column of the
    documents table and printed on top of the digest beside it. With
    `overflow-wrap: anywhere` it broke mid-word instead -
    "http://devsources:80 / 80/public/convocator / ia.html". The filter marks
    the places a reader would break a path, and the stylesheet prefers them.
    """

    def test_it_breaks_after_each_separator(self) -> None:
        marked = str(vocab.breakable("http://devsources:8080/public/convocatoria.html"))
        assert marked == (
            "http:<wbr>/<wbr>/<wbr>devsources:<wbr>8080/<wbr>public/<wbr>convocatoria.html"
        )
        # A break opportunity is not a character: dropping the tags gives the
        # original back, which is what makes the text still copy as one URL.
        assert marked.replace("<wbr>", "") == "http://devsources:8080/public/convocatoria.html"

    def test_it_leaves_text_without_separators_alone(self) -> None:
        assert str(vocab.breakable("registry-personnel.json")) == "registry-personnel.json"
        assert str(vocab.breakable("")) == ""

    def test_it_escapes_the_value_it_marks_up(self) -> None:
        """The filter returns markup, so the value must not be able to add any.

        `source_detail` is a captured URL or endpoint - data from outside this
        process - and the template renders the filter's output unescaped
        because it is `Markup`. `Markup.join` escapes each piece it joins, so
        the only markup in the result is the literal `<wbr>`.
        """
        marked = str(vocab.breakable("<script>alert(1)</script>"))
        assert "<script>" not in marked
        assert "&lt;script&gt;" in marked
        marked = str(vocab.breakable("a & b"))
        assert "&amp;" in marked
        # And an entity is never split: the separators it inserts after cannot
        # appear inside one.
        assert "&<wbr>" not in marked
        assert "&am" not in marked.replace("&amp;", "")
