"""The synthetic dataset, defined once.

Everything downstream is generated from this module: the PDFs, the scanned
receipts, the workbook, the records the local registry service serves and the
published call page. Keeping one definition is what makes the seeded defects
meaningful — the workbook disagrees with the report because this file says it
does, not because two generators drifted apart.

Nothing here corresponds to a real company, person, tax identifier or project.
Names are constructed, supplier names are explicitly synthetic, and the tax
identifiers use a shape (`ES-SYN-nnnn`) that no real registry issues.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

CORPUS_VERSION = "1.1.0"

# The eligible window published by the (synthetic) funding call.
CALL_CODE = "CALL-SYN-2025-A"
CALL_PERIOD_START = date(2025, 1, 1)
CALL_PERIOD_END = date(2025, 12, 31)
CALL_MAX_FUNDING = Decimal("400000.00")

# Regulatory-ish ceilings the rules check against. They are plausible rather
# than authoritative, and the rule catalogue says so.
MAX_ANNUAL_HOURS_PER_PERSON = Decimal("1720")
MAX_MONTHLY_HOURS_PER_PERSON = Decimal("180")


@dataclass(frozen=True)
class Person:
    employee_id: str
    full_name: str
    role: str
    hourly_rate_eur: Decimal
    contract_start: date
    contract_end: date | None = None


@dataclass(frozen=True)
class TimesheetRow:
    employee_id: str
    full_name: str
    role: str
    month: str  # YYYY-MM
    hours: Decimal
    hourly_rate_eur: Decimal

    @property
    def amount_eur(self) -> Decimal:
        return (self.hours * self.hourly_rate_eur).quantize(Decimal("0.01"))


@dataclass(frozen=True)
class Invoice:
    invoice_number: str
    supplier_name: str
    supplier_tax_id: str
    issue_date: date
    base_eur: Decimal
    vat_rate: Decimal
    project_code: str | None
    concept: str
    # Rendering knobs for the scanned image, used to create a genuinely hard
    # OCR case rather than a clean one.
    blur: float = 0.4
    noise: int = 8
    rotation: float = 0.3
    jpeg_quality: int = 88

    @property
    def vat_eur(self) -> Decimal:
        return (self.base_eur * self.vat_rate).quantize(Decimal("0.01"))

    @property
    def total_eur(self) -> Decimal:
        return (self.base_eur + self.vat_eur).quantize(Decimal("0.01"))


@dataclass(frozen=True)
class DossierSpec:
    reference: str
    title: str
    summary: str
    period_start: date
    period_end: date
    personnel: tuple[Person, ...]
    timesheet: tuple[TimesheetRow, ...]
    invoices: tuple[Invoice, ...]
    # What the technical report claims, which may deliberately disagree with
    # what the evidence adds up to.
    declared_personnel_cost_eur: Decimal
    declared_external_cost_eur: Decimal
    # What the claim form says, which is not always what the report adds up to.
    # None means the two agree.
    claimed_total_override_eur: Decimal | None = None
    expected_findings: tuple[str, ...] = ()
    include_duplicate_report: bool = False
    include_corrupt_pdf: bool = False
    include_unsupported_file: bool = False
    include_injection_page: bool = False
    notes: str = ""
    extra_report_lines: tuple[str, ...] = field(default_factory=tuple)

    @property
    def declared_total_eur(self) -> Decimal:
        return self.declared_personnel_cost_eur + self.declared_external_cost_eur

    @property
    def claimed_total_eur(self) -> Decimal:
        return self.claimed_total_override_eur or self.declared_total_eur

    @property
    def timesheet_total_eur(self) -> Decimal:
        return sum((row.amount_eur for row in self.timesheet), Decimal("0.00"))

    @property
    def invoice_total_eur(self) -> Decimal:
        return sum((inv.total_eur for inv in self.invoices), Decimal("0.00"))


# --------------------------------------------------------------------------
# Registry: the corporate system of record for eligible personnel.
# --------------------------------------------------------------------------

REGISTRY_PEOPLE: tuple[Person, ...] = (
    Person(
        "EMP-0142", "Nerea Talvi", "Investigadora principal", Decimal("42.50"), date(2021, 3, 1)
    ),
    Person("EMP-0187", "Iker Rondel", "Ingeniero de datos", Decimal("36.00"), date(2022, 9, 12)),
    Person("EMP-0203", "Marta Uxeli", "Ingeniera de software", Decimal("34.75"), date(2023, 1, 9)),
    Person(
        "EMP-0244", "Pablo Vindel", "Tecnico de laboratorio", Decimal("28.20"), date(2023, 6, 1)
    ),
    Person(
        "EMP-0261",
        "Sofia Merque",
        "Analista de calidad",
        Decimal("31.40"),
        date(2024, 2, 5),
        contract_end=date(2025, 6, 30),
    ),
    Person(
        "EMP-0299", "Hector Balze", "Ingeniero de procesos", Decimal("38.10"), date(2020, 11, 3)
    ),
)

REGISTRY_BY_ID = {p.employee_id: p for p in REGISTRY_PEOPLE}

# --------------------------------------------------------------------------
# Dossier A: consistent. Every rule should pass.
# --------------------------------------------------------------------------

_A_TIMESHEET = tuple(
    TimesheetRow(p.employee_id, p.full_name, p.role, month, hours, p.hourly_rate_eur)
    for p, month, hours in (
        (REGISTRY_BY_ID["EMP-0142"], "2025-02", Decimal("120")),
        (REGISTRY_BY_ID["EMP-0142"], "2025-03", Decimal("136")),
        (REGISTRY_BY_ID["EMP-0187"], "2025-02", Decimal("152")),
        (REGISTRY_BY_ID["EMP-0187"], "2025-03", Decimal("160")),
        (REGISTRY_BY_ID["EMP-0203"], "2025-03", Decimal("144")),
        (REGISTRY_BY_ID["EMP-0203"], "2025-04", Decimal("120")),
        (REGISTRY_BY_ID["EMP-0299"], "2025-04", Decimal("96")),
    )
)

_A_INVOICES = (
    Invoice(
        invoice_number="FS-2025-0417",
        supplier_name="Proveedor Sintetico Alfa S.L.",
        supplier_tax_id="ES-SYN-0001",
        issue_date=date(2025, 3, 18),
        base_eur=Decimal("12400.00"),
        vat_rate=Decimal("0.21"),
        project_code="INN-2025-041",
        concept="Ensayos de caracterizacion de sensores",
    ),
    Invoice(
        invoice_number="FS-2025-0588",
        supplier_name="Proveedor Sintetico Beta S.L.",
        supplier_tax_id="ES-SYN-0002",
        issue_date=date(2025, 5, 6),
        base_eur=Decimal("8750.00"),
        vat_rate=Decimal("0.21"),
        project_code="INN-2025-041",
        concept="Servicios de validacion metrologica",
    ),
)

DOSSIER_A = DossierSpec(
    reference="INN-2025-041",
    title="Trazabilidad automatizada de lotes en linea de envasado",
    summary=(
        "El proyecto desarrolla un sistema de trazabilidad de lotes basado en sensores de vision "
        "y en un modelo de reconciliacion de eventos de planta. El objetivo es reducir el tiempo "
        "de conciliacion documental entre partes de produccion y albaranes de expedicion."
    ),
    period_start=date(2025, 1, 1),
    period_end=date(2025, 12, 31),
    personnel=(
        REGISTRY_BY_ID["EMP-0142"],
        REGISTRY_BY_ID["EMP-0187"],
        REGISTRY_BY_ID["EMP-0203"],
        REGISTRY_BY_ID["EMP-0299"],
    ),
    timesheet=_A_TIMESHEET,
    invoices=_A_INVOICES,
    declared_personnel_cost_eur=sum((r.amount_eur for r in _A_TIMESHEET), Decimal("0.00")),
    declared_external_cost_eur=sum((i.total_eur for i in _A_INVOICES), Decimal("0.00")),
    expected_findings=(),
    notes="Happy path. Used to show that a clean dossier produces no findings.",
)

# --------------------------------------------------------------------------
# Dossier B: every seeded defect the rule catalogue is meant to catch.
# --------------------------------------------------------------------------

_B_TIMESHEET = (
    # Rate disagrees with the registry (35.00 declared vs 42.50 on record).
    TimesheetRow(
        "EMP-0142",
        "Nerea Talvi",
        "Investigadora principal",
        "2025-02",
        Decimal("150"),
        Decimal("35.00"),
    ),
    # Hours above the monthly ceiling.
    TimesheetRow(
        "EMP-0187", "Iker Rondel", "Ingeniero de datos", "2025-03", Decimal("245"), Decimal("36.00")
    ),
    # Negative hours: a correction the client made by hand in the spreadsheet.
    TimesheetRow(
        "EMP-0187", "Iker Rondel", "Ingeniero de datos", "2025-04", Decimal("-12"), Decimal("36.00")
    ),
    # Not in the registry at all.
    TimesheetRow(
        "EMP-0777", "Lucia Fenner", "Consultora externa", "2025-03", Decimal("88"), Decimal("45.00")
    ),
    # Charged after the contract end date on record (2025-06-30).
    TimesheetRow(
        "EMP-0261",
        "Sofia Merque",
        "Analista de calidad",
        "2025-09",
        Decimal("64"),
        Decimal("31.40"),
    ),
    TimesheetRow(
        "EMP-0203",
        "Marta Uxeli",
        "Ingeniera de software",
        "2025-05",
        Decimal("120"),
        Decimal("34.75"),
    ),
)

_B_INVOICES = (
    Invoice(
        invoice_number="FS-2025-0901",
        supplier_name="Proveedor Sintetico Gamma S.L.",
        supplier_tax_id="ES-SYN-0003",
        issue_date=date(2025, 4, 22),
        base_eur=Decimal("15200.00"),
        vat_rate=Decimal("0.21"),
        project_code="INN-2025-042",
        concept="Desarrollo de banco de pruebas",
    ),
    # Same invoice number and amount, but a separately rendered reissue.
    Invoice(
        invoice_number="FS-2025-0901",
        supplier_name="Proveedor Sintetico Gamma S.L.",
        supplier_tax_id="ES-SYN-0003",
        issue_date=date(2025, 4, 22),
        base_eur=Decimal("15200.00"),
        vat_rate=Decimal("0.21"),
        project_code="INN-2025-042",
        concept="Desarrollo de banco de pruebas (reemision)",
    ),
    # Dated before the eligible period opens.
    Invoice(
        invoice_number="FS-2024-1180",
        supplier_name="Proveedor Sintetico Delta S.L.",
        supplier_tax_id="ES-SYN-0004",
        issue_date=date(2024, 11, 14),
        base_eur=Decimal("6300.00"),
        vat_rate=Decimal("0.21"),
        project_code="INN-2025-042",
        concept="Suministro de instrumentacion",
    ),
    # No project code printed on the document at all.
    Invoice(
        invoice_number="FS-2025-1042",
        supplier_name="Proveedor Sintetico Epsilon S.L.",
        supplier_tax_id="ES-SYN-0005",
        issue_date=date(2025, 7, 9),
        base_eur=Decimal("4180.00"),
        vat_rate=Decimal("0.21"),
        project_code=None,
        concept="Consultoria de integracion",
    ),
    # Deliberately hard to read: this is the low-confidence OCR case.
    Invoice(
        invoice_number="FS-2025-1177",
        supplier_name="Proveedor Sintetico Zeta S.L.",
        supplier_tax_id="ES-SYN-0006",
        issue_date=date(2025, 8, 27),
        base_eur=Decimal("9640.00"),
        vat_rate=Decimal("0.21"),
        project_code="INN-2025-042",
        concept="Ensayos acelerados de durabilidad",
        blur=1.9,
        noise=42,
        rotation=1.6,
        jpeg_quality=28,
    ),
)

DOSSIER_B = DossierSpec(
    reference="INN-2025-042",
    title="Plataforma de inspeccion asistida para linea de montaje",
    summary=(
        "El proyecto aborda la inspeccion asistida de subconjuntos mediante vision artificial y "
        "reglas de decision deterministas, con revision humana de los casos dudosos. Se busca "
        "reducir el retrabajo y documentar la evidencia de cada rechazo."
    ),
    period_start=date(2025, 1, 1),
    period_end=date(2025, 12, 31),
    personnel=(
        REGISTRY_BY_ID["EMP-0142"],
        REGISTRY_BY_ID["EMP-0187"],
        REGISTRY_BY_ID["EMP-0203"],
        REGISTRY_BY_ID["EMP-0261"],
    ),
    timesheet=_B_TIMESHEET,
    invoices=_B_INVOICES,
    # Deliberately inconsistent with the workbook and with the receipts.
    declared_personnel_cost_eur=Decimal("31500.00"),
    declared_external_cost_eur=Decimal("52000.00"),
    # The claim form was filled in before the report was finalised.
    claimed_total_override_eur=Decimal("85000.00"),
    expected_findings=(
        "PERSONNEL_RATE_MISMATCH",
        "HOURS_ABOVE_MONTHLY_CEILING",
        "NEGATIVE_HOURS",
        "UNKNOWN_PERSON",
        "PERSON_OUTSIDE_CONTRACT",
        "DUPLICATE_INVOICE_NUMBER",
        "EXPENSE_OUTSIDE_ELIGIBLE_PERIOD",
        "MISSING_PROJECT_CODE",
        "PERSONNEL_COST_MISMATCH",
        "EXTERNAL_COST_MISMATCH",
        "CLAIMED_TOTAL_MISMATCH",
        "LOW_OCR_CONFIDENCE",
        "UNSUPPORTED_DOCUMENT",
        "CORRUPT_DOCUMENT",
        "DUPLICATE_DOCUMENT",
        "PROMPT_INJECTION_ATTEMPT",
        "INSUFFICIENT_EVIDENCE",
    ),
    include_duplicate_report=True,
    include_corrupt_pdf=True,
    include_unsupported_file=True,
    include_injection_page=True,
    notes="Seeded dossier. Each defect maps to exactly one rule in the catalogue.",
)

# --------------------------------------------------------------------------
# Dossier C: one defect, of the kind that actually turns up most often.
#
# A and B are the two extremes - nothing wrong, and everything wrong. Neither
# looks like an ordinary claim, and a reviewer's judgement is mostly spent on
# claims with one or two problems. This is that case.
# --------------------------------------------------------------------------

_C_TIMESHEET = (
    # Charged at 40,00 while the registry holds 42,50: the single most common
    # real finding, and it under-claims rather than over-claims.
    TimesheetRow(
        "EMP-0142",
        "Nerea Talvi",
        "Investigadora principal",
        "2025-03",
        Decimal("120"),
        Decimal("40.00"),
    ),
    TimesheetRow(
        "EMP-0203",
        "Marta Uxeli",
        "Ingeniera de software",
        "2025-03",
        Decimal("140"),
        Decimal("34.75"),
    ),
    TimesheetRow(
        "EMP-0203",
        "Marta Uxeli",
        "Ingeniera de software",
        "2025-04",
        Decimal("150"),
        Decimal("34.75"),
    ),
    TimesheetRow(
        "EMP-0299",
        "Hector Balze",
        "Ingeniero de procesos",
        "2025-04",
        Decimal("110"),
        Decimal("38.10"),
    ),
)

_C_INVOICES = (
    Invoice(
        invoice_number="FS-2025-2210",
        supplier_name="Laboratorio Sintetico Iota S.L.",
        supplier_tax_id="ES-SYN-0011",
        issue_date=date(2025, 3, 18),
        base_eur=Decimal("8400.00"),
        vat_rate=Decimal("0.21"),
        project_code="INN-2025-043",
        concept="Ensayos de caracterizacion de materiales",
        blur=0.35,
        noise=6,
        rotation=0.2,
        jpeg_quality=90,
    ),
    Invoice(
        invoice_number="FS-2025-2311",
        supplier_name="Ingenieria Sintetica Kappa S.L.",
        supplier_tax_id="ES-SYN-0012",
        issue_date=date(2025, 5, 6),
        base_eur=Decimal("12750.00"),
        vat_rate=Decimal("0.21"),
        project_code="INN-2025-043",
        concept="Desarrollo de firmware para el banco de ensayo",
        blur=0.4,
        noise=7,
        rotation=-0.25,
        jpeg_quality=89,
    ),
)

DOSSIER_C = DossierSpec(
    reference="INN-2025-043",
    title="Recubrimientos funcionales para componentes de alta temperatura",
    summary=(
        "El proyecto desarrolla un recubrimiento ceramico de bajo espesor para componentes "
        "sometidos a ciclos termicos severos, con un banco de ensayo instrumentado para medir "
        "la degradacion en condiciones controladas."
    ),
    period_start=date(2025, 1, 1),
    period_end=date(2025, 12, 31),
    personnel=(
        REGISTRY_BY_ID["EMP-0142"],
        REGISTRY_BY_ID["EMP-0203"],
        REGISTRY_BY_ID["EMP-0299"],
    ),
    timesheet=_C_TIMESHEET,
    invoices=_C_INVOICES,
    declared_personnel_cost_eur=sum((r.amount_eur for r in _C_TIMESHEET), Decimal("0.00")),
    declared_external_cost_eur=sum((i.total_eur for i in _C_INVOICES), Decimal("0.00")),
    expected_findings=("PERSONNEL_RATE_MISMATCH",),
    notes=(
        "One rate that disagrees with the registry, everything else consistent. "
        "The realistic middle case between A and B."
    ),
)


# --------------------------------------------------------------------------
# Dossier D: the two ceiling rules, which no other dossier reached.
#
# CLAIM_ABOVE_CALL_MAXIMUM and HOURS_ABOVE_ANNUAL_CEILING were in the
# catalogue and covered by unit tests, but nothing in the corpus made them
# fire end to end. A rule that has never fired against a real document is a
# rule nobody has watched work.
# --------------------------------------------------------------------------

# 1.800 hours across twelve months: 150 a month stays under the monthly
# ceiling, so only the annual one fires and the finding is unambiguous.
_D_HEAVY_MONTHS = tuple(f"2025-{month:02d}" for month in range(1, 13))
_D_TIMESHEET = (
    *(
        TimesheetRow(
            "EMP-0299",
            "Hector Balze",
            "Ingeniero de procesos",
            month,
            Decimal("150"),
            Decimal("38.10"),
        )
        for month in _D_HEAVY_MONTHS
    ),
    TimesheetRow(
        "EMP-0244",
        "Pablo Vindel",
        "Tecnico de laboratorio",
        "2025-02",
        Decimal("160"),
        Decimal("28.20"),
    ),
    TimesheetRow(
        "EMP-0244",
        "Pablo Vindel",
        "Tecnico de laboratorio",
        "2025-03",
        Decimal("160"),
        Decimal("28.20"),
    ),
)

_D_INVOICES = (
    Invoice(
        invoice_number="FS-2025-3401",
        supplier_name="Centro Sintetico de Investigacion Lambda",
        supplier_tax_id="ES-SYN-0013",
        issue_date=date(2025, 2, 11),
        base_eur=Decimal("148000.00"),
        vat_rate=Decimal("0.21"),
        project_code="INN-2025-044",
        concept="Colaboracion externa con centro de investigacion",
        blur=0.38,
        noise=6,
        rotation=0.15,
        jpeg_quality=90,
    ),
    Invoice(
        invoice_number="FS-2025-3512",
        supplier_name="Planta Piloto Sintetica Mu S.L.",
        supplier_tax_id="ES-SYN-0014",
        issue_date=date(2025, 7, 24),
        base_eur=Decimal("142000.00"),
        vat_rate=Decimal("0.21"),
        project_code="INN-2025-044",
        concept="Construccion de planta piloto y puesta en marcha",
        blur=0.42,
        noise=8,
        rotation=-0.2,
        jpeg_quality=88,
    ),
)

DOSSIER_D = DossierSpec(
    reference="INN-2025-044",
    title="Planta piloto de recuperacion de disolventes por membranas",
    summary=(
        "El proyecto escala a planta piloto un proceso de separacion por membranas para "
        "recuperar disolventes de un efluente industrial, con el objetivo de reducir el "
        "consumo de materia prima virgen y el volumen de residuo peligroso."
    ),
    period_start=date(2025, 1, 1),
    period_end=date(2025, 12, 31),
    personnel=(
        REGISTRY_BY_ID["EMP-0244"],
        REGISTRY_BY_ID["EMP-0299"],
    ),
    timesheet=_D_TIMESHEET,
    invoices=_D_INVOICES,
    declared_personnel_cost_eur=sum((r.amount_eur for r in _D_TIMESHEET), Decimal("0.00")),
    declared_external_cost_eur=sum((i.total_eur for i in _D_INVOICES), Decimal("0.00")),
    expected_findings=(
        "HOURS_ABOVE_ANNUAL_CEILING",
        "CLAIM_ABOVE_CALL_MAXIMUM",
    ),
    notes=(
        "Claims above the published maximum and imputes more hours to one person "
        "than a year holds. Both rules existed and neither had ever fired against "
        "a document."
    ),
)


# --------------------------------------------------------------------------
# Dossier E: an invoice that belongs to another claim.
#
# PROJECT_CODE_MISMATCH is a different failure from MISSING_PROJECT_CODE: the
# traceability is not absent, it points somewhere else. In a consultancy running
# many claims at once for the same client, that is the likelier mistake.
# --------------------------------------------------------------------------

_E_TIMESHEET = (
    TimesheetRow(
        "EMP-0187",
        "Iker Rondel",
        "Ingeniero de datos",
        "2025-06",
        Decimal("130"),
        Decimal("36.00"),
    ),
    TimesheetRow(
        "EMP-0187",
        "Iker Rondel",
        "Ingeniero de datos",
        "2025-07",
        Decimal("120"),
        Decimal("36.00"),
    ),
    TimesheetRow(
        "EMP-0203",
        "Marta Uxeli",
        "Ingeniera de software",
        "2025-07",
        Decimal("145"),
        Decimal("34.75"),
    ),
)

_E_INVOICES = (
    Invoice(
        invoice_number="FS-2025-4102",
        supplier_name="Consultoria Sintetica Nu S.L.",
        supplier_tax_id="ES-SYN-0015",
        issue_date=date(2025, 6, 30),
        base_eur=Decimal("9600.00"),
        vat_rate=Decimal("0.21"),
        project_code="INN-2025-045",
        concept="Analisis de requisitos y arquitectura de datos",
        blur=0.36,
        noise=6,
        rotation=0.18,
        jpeg_quality=90,
    ),
    Invoice(
        invoice_number="FS-2025-4188",
        supplier_name="Consultoria Sintetica Nu S.L.",
        supplier_tax_id="ES-SYN-0015",
        issue_date=date(2025, 8, 12),
        base_eur=Decimal("7400.00"),
        vat_rate=Decimal("0.21"),
        # Charged to the wrong claim: this reference belongs to dossier A.
        project_code="INN-2025-041",
        concept="Integracion con el sistema de planta",
        blur=0.4,
        noise=7,
        rotation=-0.22,
        jpeg_quality=89,
    ),
)

DOSSIER_E = DossierSpec(
    reference="INN-2025-045",
    title="Gemelo digital de la red de aire comprimido de una planta",
    summary=(
        "El proyecto construye un modelo en tiempo casi real de la red de aire comprimido de "
        "una planta para detectar fugas y desviaciones de consumo antes de que se traduzcan en "
        "coste energetico, cruzando telemetria con los partes de mantenimiento."
    ),
    period_start=date(2025, 1, 1),
    period_end=date(2025, 12, 31),
    personnel=(
        REGISTRY_BY_ID["EMP-0187"],
        REGISTRY_BY_ID["EMP-0203"],
    ),
    timesheet=_E_TIMESHEET,
    invoices=_E_INVOICES,
    declared_personnel_cost_eur=sum((r.amount_eur for r in _E_TIMESHEET), Decimal("0.00")),
    declared_external_cost_eur=sum((i.total_eur for i in _E_INVOICES), Decimal("0.00")),
    expected_findings=("PROJECT_CODE_MISMATCH",),
    notes=(
        "One invoice charged to a different claim. Traceability is present but "
        "points elsewhere, which MISSING_PROJECT_CODE would not catch."
    ),
)


DOSSIERS: tuple[DossierSpec, ...] = (
    DOSSIER_A,
    DOSSIER_B,
    DOSSIER_C,
    DOSSIER_D,
    DOSSIER_E,
)


def dossier_by_reference(reference: str) -> DossierSpec:
    for spec in DOSSIERS:
        if spec.reference == reference:
            return spec
    raise KeyError(reference)
