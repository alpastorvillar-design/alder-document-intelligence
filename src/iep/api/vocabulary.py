"""Human-readable Spanish labels for the review screen.

The domain is Spanish innovation-funding justification, the corpus is Spanish
and the people who would use the review screen are Spanish-speaking, so the
interface is Spanish. Identifiers stay in English everywhere else - route
paths, field paths, rule ids, enum members - because they are keys, not prose,
and translating a key breaks every caller.

Keeping the vocabulary in one module rather than in the templates means a rule
gains its explanation once, and the report, the screen and any future client
say the same thing about it.

Each rule carries three things: what it is called in plain language, what it
actually checks, and the requirement it comes from. The third is what makes a
finding arguable rather than an opinion - a reviewer who disagrees with
`INVOICE_MISSING_PROJECT_CODE` is disagreeing with a documented rule about
traceability, not with a program.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Protocol

# --------------------------------------------------------------------------
# States
# --------------------------------------------------------------------------

DOSSIER_STATUS = {
    "DRAFT": ("Borrador", "Creado, todavía sin documentos"),
    "INGESTED": ("Documentos recibidos", "Tiene documentos y puede procesarse"),
    "QUEUED": ("En cola", "Esperando a que un worker lo coja"),
    "PROCESSING": ("Procesando", "Un worker está leyendo los documentos"),
    "NEEDS_REVIEW": ("Pendiente de revisión", "Procesado. Necesita que una persona decida"),
    "APPROVED": ("Aprobado", "Aprobado por una persona. No admite cambios"),
    "REJECTED": ("Rechazado", "Devuelto con motivo. Puede recibir documentos nuevos"),
    "FAILED": ("Error", "El procesamiento falló. Se puede reencolar"),
}

DOCUMENT_STATUS = {
    "RECEIVED": ("Recibido", "Aceptado y almacenado, aún sin leer"),
    "EXTRACTED": ("Leído", "Se le han extraído campos"),
    "UNSUPPORTED": ("Formato no admitido", "No se guardó el contenido"),
    "CORRUPT": ("Fichero dañado", "No se pudo abrir con su parser"),
    "FAILED": ("Error al leer", "Se aceptó pero la extracción falló"),
}

FIELD_STATUS = {
    "EXTRACTED": ("Leído por la máquina", "neutral"),
    "NEEDS_REVIEW": ("Necesita revisión", "warning"),
    "CONFIRMED": ("Confirmado por una persona", "ok"),
    "CORRECTED": ("Corregido por una persona", "ok"),
    "REJECTED": ("Descartado", "muted"),
}

FINDING_STATUS = {
    "OPEN": ("Abierta", "warning"),
    "ACCEPTED": ("Aceptada: la incidencia es real", "blocker"),
    "DISMISSED": ("Descartada como falso positivo", "muted"),
    "RESOLVED": ("Resuelta", "ok"),
}

SEVERITY = {
    "BLOCKER": ("Bloqueante", "Impide aprobar el expediente"),
    "WARNING": ("Aviso", "No impide aprobar, pero conviene mirarlo"),
    "INFO": ("Informativo", "Sólo para que conste"),
}

JOB_STATUS = {
    "PENDING": "En espera",
    "RUNNING": "Ejecutándose",
    "SUCCEEDED": "Terminado",
    "FAILED": "Fallido",
    "DEAD_LETTER": "Agotó los intentos",
}

DOCUMENT_KIND = {
    "TECHNICAL_REPORT": "Memoria técnica",
    "EXPENSE_INVOICE": "Factura o justificante de gasto",
    "TIMESHEET": "Parte horario",
    "PERSONNEL_REGISTRY": "Registro de personal",
    "CALL_PAGE": "Página de convocatoria",
    "UNKNOWN": "Sin clasificar",
}

MEDIA_KIND = {
    "PDF": "PDF",
    "PNG": "Imagen PNG",
    "JPEG": "Imagen JPEG (escaneo)",
    "XLSX": "Libro de Excel",
    "JSON": "Respuesta JSON",
    "HTML": "Página HTML",
    "UNSUPPORTED": "No admitido",
}

SOURCE_KIND = {
    "UPLOAD": "Subido por una persona",
    "REGISTRY_API": "Capturado de la API de personal",
    "PUBLIC_PAGE": "Capturado de una página pública",
    "DERIVED": "Calculado por el sistema",
}

# --------------------------------------------------------------------------
# Rules
#
# `base` cites the requirement the rule enforces. The wording of the
# requirements comes from the published CDTI technical-economic justification
# instructions, which are a public document; nothing here reproduces its text.
# --------------------------------------------------------------------------


class Rule:
    __slots__ = ("base", "explains", "title")

    def __init__(self, title: str, explains: str, base: str) -> None:
        self.title = title
        self.explains = explains
        self.base = base


RULES: dict[str, Rule] = {
    "CLAIMED_TOTAL_MISMATCH": Rule(
        "El importe reclamado no cuadra con la memoria",
        "Compara el total que el expediente declara reclamar con el total que "
        "figura en la memoria técnica.",
        "La cuenta justificativa y la memoria tienen que declarar la misma cifra: "
        "si difieren, una de las dos es incorrecta y el gasto no está acreditado.",
    ),
    "PERSONNEL_COST_MISMATCH": Rule(
        "El gasto de personal declarado no cuadra con los partes horarios",
        "Suma, en todas las filas del parte horario, las horas multiplicadas por "
        "su coste horario, y lo "
        "compara con el gasto de personal que declara la memoria.",
        "El gasto de personal se calcula a partir de las horas dedicadas "
        "multiplicadas por el coste horario. Si la suma de los partes no llega al "
        "importe declarado, la diferencia no está justificada.",
    ),
    "EXTERNAL_COST_MISMATCH": Rule(
        "El gasto de colaboraciones externas no cuadra con las facturas",
        "Suma los totales de las facturas aportadas y lo compara con el gasto "
        "externo declarado en la memoria.",
        "Los gastos cuya justificación requiere factura sólo son elegibles por el "
        "importe efectivamente facturado y pagado.",
    ),
    "DUPLICATE_INVOICE_NUMBER": Rule(
        "El mismo número de factura aparece en dos documentos",
        "Agrupa las facturas por su número y avisa si el mismo número aparece en "
        "más de un documento del expediente.",
        "Una misma factura no puede imputarse dos veces. Es uno de los controles "
        "antifraude más básicos en la justificación de ayudas.",
    ),
    "DUPLICATE_DOCUMENT": Rule(
        "El mismo fichero se ha entregado más de una vez",
        "Compara el hash SHA-256 del contenido. Si dos entregas tienen los mismos "
        "bytes, es un solo documento entregado con dos nombres.",
        "No es necesariamente un error —suele ser un reenvío— pero conviene que "
        "conste para que nadie cuente el gasto dos veces.",
    ),
    "MISSING_PROJECT_CODE": Rule(
        "La factura no menciona el proyecto",
        "Busca la referencia del expediente en el texto de la factura.",
        "No se admiten facturas en las que la vinculación del gasto con el proyecto "
        "sea incierta. Si el concepto no cita el proyecto, hay que aportar "
        "información adicional que acredite la trazabilidad.",
    ),
    "PROJECT_CODE_MISMATCH": Rule(
        "La factura cita otro proyecto",
        "Compara la referencia que aparece en la factura con la del expediente.",
        "Un gasto imputado a un proyecto tiene que estar vinculado a ese proyecto, no a otro.",
    ),
    "EXPENSE_OUTSIDE_ELIGIBLE_PERIOD": Rule(
        "Gasto fuera del periodo elegible",
        "Compara la fecha de emisión de cada factura con la ventana de "
        "elegibilidad publicada en la convocatoria.",
        "Las inversiones y gastos tienen que realizarse dentro del periodo de "
        "ejecución del proyecto. Un gasto anterior o posterior no es elegible.",
    ),
    "CLAIM_ABOVE_CALL_MAXIMUM": Rule(
        "Lo reclamado supera el máximo de la convocatoria",
        "Compara el importe reclamado con el máximo financiable que publica la convocatoria.",
        "La ayuda no puede exceder el límite establecido en las bases, por mucho "
        "que el gasto esté justificado.",
    ),
    "INVOICE_ARITHMETIC_MISMATCH": Rule(
        "La aritmética de la factura no cuadra",
        "Comprueba que base imponible + IVA = total, con una tolerancia de redondeo de un céntimo.",
        "Una factura cuyos importes no suman está mal emitida o mal leída. En "
        "ambos casos no sirve como justificante hasta aclararlo.",
    ),
    "TIMESHEET_ROW_ARITHMETIC": Rule(
        "Una fila del parte horario no cuadra",
        "Comprueba que el importe de cada fila coincide con sus horas "
        "multiplicadas por su coste horario.",
        "El importe imputado se calcula a partir de las horas y el coste horario. "
        "Si la fila no cuadra, el importe no está soportado por sus propios datos.",
    ),
    "NEGATIVE_HOURS": Rule(
        "Horas negativas",
        "Detecta filas con un número de horas menor que cero.",
        "Es materialmente imposible. Suele indicar un ajuste manual sobre la hoja "
        "de cálculo en lugar de una corrección documentada.",
    ),
    "HOURS_ABOVE_MONTHLY_CEILING": Rule(
        "Demasiadas horas en un mes",
        "Suma las horas imputadas por persona y mes y las compara con un techo mensual razonable.",
        "No se aceptan horas imputadas en periodos de vacaciones, baja, excedencia, "
        "ERTE ni festivos. Una sobreimputación respecto a las horas de convenio "
        "activa además medidas antifraude.",
    ),
    "HOURS_ABOVE_ANNUAL_CEILING": Rule(
        "Demasiadas horas en el año",
        "Acumula las horas de cada persona a lo largo de todos los meses del expediente.",
        "Una persona no puede dedicar al proyecto más horas de las que tiene en su jornada anual.",
    ),
    "PERSONNEL_RATE_MISMATCH": Rule(
        "La tarifa no coincide con el registro de personal",
        "Compara el coste horario del parte horario con el que devuelve el sistema de personal.",
        "El coste horario se calcula desde el salario bruto y la Seguridad Social "
        "del periodo. Si el parte usa otra tarifa, el importe imputado es "
        "incorrecto.",
    ),
    "UNKNOWN_PERSON": Rule(
        "Persona no registrada",
        "Comprueba que cada identificador del parte horario existe en el sistema de personal.",
        "Sólo es elegible el gasto de personal con contrato laboral con la entidad "
        "beneficiaria. Se verifica contra los documentos de la Seguridad Social.",
    ),
    "PERSON_OUTSIDE_CONTRACT": Rule(
        "Horas fuera del contrato de la persona",
        "Compara el mes imputado con las fechas de inicio y fin de contrato que "
        "consta en el registro.",
        "No se pueden imputar horas de un periodo en el que la persona no estaba de "
        "alta en la entidad beneficiaria.",
    ),
    "INSUFFICIENT_EVIDENCE": Rule(
        "Falta evidencia obligatoria",
        "Comprueba que el expediente aporta cada documento y cada campo que las "
        "reglas necesitan para poder concluir.",
        "La entidad beneficiaria está obligada a acreditar el gasto. Un campo que "
        "no se puede leer no es un gasto justificado: es un gasto sin acreditar.",
    ),
    "LOW_OCR_CONFIDENCE": Rule(
        "Escaneo de baja calidad",
        "Mira la confianza media por palabra que devuelve el motor de OCR sobre el documento.",
        "Si el sistema no está seguro de haber leído bien una cifra, no debe "
        "usarla: la deriva a una persona en lugar de arriesgarse.",
    ),
    "AMBIGUOUS_FIELD": Rule(
        "Lecturas contradictorias del mismo campo",
        "Detecta cuando dos lecturas vivas del mismo campo dan valores distintos.",
        "Ante una ambigüedad, el sistema no elige: la hace visible y bloquea la "
        "aprobación hasta que una persona decida.",
    ),
    "CORRUPT_DOCUMENT": Rule(
        "Documento dañado",
        "El fichero se aceptó por su firma pero su parser no pudo abrirlo.",
        "Un justificante que no se puede abrir no acredita nada. Hay que pedirlo de nuevo.",
    ),
    "UNSUPPORTED_DOCUMENT": Rule(
        "Formato no admitido",
        "La firma del fichero no corresponde a ninguno de los formatos aceptados.",
        "Se comprueba la firma real del fichero, no su extensión: un `.pdf` que en "
        "realidad es texto plano se rechaza.",
    ),
    "DOCUMENT_EXTRACTION_FAILED": Rule(
        "Error al leer un documento",
        "El documento se abrió pero la extracción falló.",
        "Un documento fallido es un bloqueante, no un documento ausente: la "
        "diferencia importa, porque el gasto sigue reclamado.",
    ),
    "UNTRUSTED_EXCEL_FORMULA": Rule(
        "El Excel trae fórmulas en vez de valores",
        "Detecta celdas cuyo contenido es una fórmula y no un valor literal.",
        "El valor en caché de una fórmula lo escribió Excel, no la persona que "
        "firmó el parte. No se usa como evidencia.",
    ),
    "PROMPT_INJECTION_ATTEMPT": Rule(
        "El documento contiene instrucciones dirigidas al sistema",
        "Busca patrones de texto que intentan dar órdenes al procesamiento.",
        "Se reporta y se ignora. Ninguna cifra que compara una regla proviene de un "
        "modelo de lenguaje, así que un documento no puede alterar un resultado.",
    ),
    "EXTERNAL_SOURCE_UNAVAILABLE": Rule(
        "No se pudo consultar una fuente externa",
        "El registro de personal o la página de la convocatoria no respondieron.",
        "Es un bloqueante, no un conjunto de datos vacío. Sin la fuente no se puede "
        "afirmar que las comprobaciones que dependen de ella se hayan hecho.",
    ),
}


def rule(rule_id: str) -> Rule:
    return RULES.get(
        rule_id,
        Rule(rule_id.replace("_", " ").capitalize(), "", ""),
    )


# --------------------------------------------------------------------------
# Field paths
# --------------------------------------------------------------------------

_FIELD_LABELS: dict[str, str] = {
    "report.project_code": "Referencia del proyecto",
    "report.call_code": "Código de la convocatoria",
    "report.title": "Título del proyecto",
    "report.period": "Periodo de ejecución (texto)",
    "report.period_start": "Inicio del periodo de ejecución",
    "report.period_end": "Fin del periodo de ejecución",
    "report.declared_total_eur": "Total declarado en la memoria",
    "report.declared_personnel_cost_eur": "Gasto de personal declarado",
    "report.declared_external_cost_eur": "Colaboraciones externas declaradas",
    "invoice.number": "Número de factura",
    "invoice.issue_date": "Fecha de emisión",
    "invoice.supplier_name": "Proveedor",
    "invoice.supplier_tax_id": "NIF del proveedor",
    "invoice.project_code": "Proyecto citado en la factura",
    "invoice.base_eur": "Base imponible",
    "invoice.vat_eur": "IVA",
    "invoice.total_eur": "Total de la factura",
    "invoices.total_eur": "Suma de todas las facturas",
    "invoices.count": "Número de facturas",
    "timesheet.total_amount_eur": "Suma del parte horario",
    "timesheet.row_count": "Filas del parte horario",
    "call.code": "Código de la convocatoria",
    "call.status": "Estado de la convocatoria",
    "call.eligible_from": "Gasto elegible desde",
    "call.eligible_to": "Gasto elegible hasta",
    "call.max_funding_eur": "Máximo financiable",
}

_ROW_LABELS = {
    "employee_id": "Identificador de la persona",
    "full_name": "Nombre",
    "role": "Categoría",
    "month": "Mes imputado",
    "hours": "Horas",
    "hourly_rate_eur": "Coste horario",
    "amount_eur": "Importe imputado",
    "contract_start": "Inicio de contrato",
    "contract_end": "Fin de contrato",
}


def unit_of(field_path: str) -> str:
    """The unit a value is measured in, or nothing.

    A money figure printed as `400.000,00` next to a date and a code reads as
    an unlabelled number. The field path already says which it is - anything
    ending in `_eur` is euros, `hours` is hours - so the unit does not need a
    second table to fall out of step with the first.
    """
    if field_path.endswith("_eur"):
        return "€"
    if field_path.endswith(".hours"):
        return "h"
    return ""


_ROW_RE = re.compile(r"^timesheet\.rows\[(\d+)\]\.(\w+)$")
_REGISTRY_RE = re.compile(r"^registry\.personnel\[([\w-]+)\]\.(\w+)$")


def field_label(field_path: str) -> str:
    """A field path said the way a reviewer would say it."""
    if field_path in _FIELD_LABELS:
        return _FIELD_LABELS[field_path]
    row = _ROW_RE.match(field_path)
    if row:
        label = _ROW_LABELS.get(row.group(2), row.group(2))
        return f"Parte horario, fila {int(row.group(1)) + 1} · {label}"
    registry = _REGISTRY_RE.match(field_path)
    if registry:
        label = _ROW_LABELS.get(registry.group(2), registry.group(2))
        return f"Registro de personal, {registry.group(1)} · {label}"
    return field_path


GROUPS = (
    ("report.", "Memoria técnica", "Lo que la empresa declara en su memoria"),
    ("invoice.", "Facturas y justificantes", "Leído de los escaneos, con OCR"),
    ("invoices.", "Totales de facturas", "Sumas calculadas por el sistema"),
    ("timesheet.rows", "Parte horario", "Celda a celda, del libro de Excel"),
    ("timesheet.", "Totales del parte horario", "Sumas calculadas por el sistema"),
    ("registry.", "Registro de personal", "Consultado por API al sistema corporativo"),
    ("call.", "Convocatoria", "Capturado de la página publicada"),
)


def group_of(field_path: str) -> str:
    for prefix, title, _ in GROUPS:
        if field_path.startswith(prefix):
            return title
    return "Otros"


class HasFieldPath(Protocol):
    field_path: str


def group_extractions[Row: HasFieldPath](
    rows: Iterable[Row],
) -> list[tuple[str, str, list[Row]]]:
    """Fields in the order a reviewer would read them: by where they came from.

    The review screen and the report both need this, and they need it to come
    out the same: a field that sits under "Parte horario" on screen has to sit
    under "Parte horario" in the artefact somebody files.
    """
    buckets: dict[str, list[Row]] = {}
    for row in rows:
        buckets.setdefault(group_of(row.field_path), []).append(row)
    ordered: list[tuple[str, str, list[Row]]] = []
    for _, title, subtitle in GROUPS:
        if title in buckets:
            ordered.append((title, subtitle, buckets.pop(title)))
    for title, remaining in buckets.items():
        ordered.append((title, "", remaining))
    return ordered


class HasIdAndPath(Protocol):
    field_path: str

    @property
    def id(self) -> Any: ...

    @property
    def document_id(self) -> Any: ...


def evidence_links[Row: HasIdAndPath](
    rows: Sequence[Row], document_names: Mapping[str, str]
) -> list[tuple[Row, str]]:
    """Label each evidence link so two of them are never the same word.

    `DUPLICATE_INVOICE_NUMBER` points at the invoice number on two different
    documents, and both links read "Número de factura". A reviewer could not
    tell which was which, so the document name is added exactly where a label
    would otherwise repeat - and nowhere else, because a filename appended to
    every link is noise.
    """
    labels = [field_label(row.field_path) for row in rows]
    repeated = {label for label in labels if labels.count(label) > 1}
    out: list[tuple[Row, str]] = []
    for row, label in zip(rows, labels, strict=True):
        if label in repeated:
            name = document_names.get(str(row.document_id), "")
            if name:
                label = f"{label} · {name}"
        out.append((row, label))
    return out


# --------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------

# The check a person does by hand: for each concepto de gasto, what the report
# declares against what the supporting documents actually add up to. Naming
# both sides here - rather than listing figures one under the other - is what
# turns a list of numbers into the comparison the justification rests on.
RECONCILIATION = (
    (
        "Gastos de personal",
        "report.declared_personnel_cost_eur",
        "timesheet.total_amount_eur",
        "Frente a la suma, parte horario a parte horario, de horas por coste horario.",
    ),
    (
        "Colaboraciones externas",
        "report.declared_external_cost_eur",
        "invoices.total_eur",
        "Frente a la suma de las bases imponibles de los justificantes aportados.",
    ),
    (
        "Total del proyecto",
        "report.declared_total_eur",
        None,
        "Frente al importe que la entidad reclama en la cuenta justificativa.",
    ),
)


# --------------------------------------------------------------------------
# Finding detail
# --------------------------------------------------------------------------

# Every key a rule puts in `detail` needs an entry. A missing one renders as
# its own snake_case name next to a Spanish label, which is how "FOUND" and
# "EXPECTED" ended up on the review screen. A test walks the processed corpus
# and fails on any key that has no label here.
_DETAIL_LABELS = {
    "found": "Encontrado",
    "base_eur": "Base imponible",
    "vat_eur": "IVA",
    "stated_total_eur": "Total impreso en la factura",
    "computed_total_eur": "Total calculado (base + IVA)",
    "formula_cells": "Celdas con fórmula",
    "missing_kind": "Tipo de documento que falta",
    "readings": "Lecturas encontradas",
    "field_path": "Campo",
    "error": "Error",
    "expected": "Se esperaba",
    "also_submitted_as": "Entregado también como",
    "content_sha256": "Huella del contenido",
    "matched_phrases": "Frases detectadas",
    "mean_word_confidence": "Confianza media por palabra",
    "year": "Año",
    "declared_eur": "Declarado",
    "evidence_eur": "Según la evidencia",
    "difference_eur": "Diferencia",
    "claimed_total_eur": "Reclamado",
    "report_total_eur": "En la memoria",
    "invoice_number": "Número de factura",
    "occurrences": "Veces que aparece",
    "issue_date": "Fecha de emisión",
    "eligible_from": "Elegible desde",
    "eligible_to": "Elegible hasta",
    "window_source": "Ventana tomada de",
    "window_source_url": "Página consultada",
    "employee_id": "Persona",
    "month": "Mes",
    "declared_rate_eur": "Tarifa en el parte",
    "registry_rate_eur": "Tarifa en el registro",
    "contract_start": "Inicio de contrato",
    "contract_end": "Fin de contrato",
    "missing_field": "Campo que falta",
    "hours": "Horas",
    "ceiling": "Techo",
    "source": "Fuente",
    "description": "Descripción",
    "sheet": "Hoja",
    "cell": "Celda",
    "values": "Valores encontrados",
    "reason": "Motivo",
    "document": "Documento",
    "max_funding_eur": "Máximo financiable",
}

_EUR_KEYS = tuple(k for k in _DETAIL_LABELS if k.endswith("_eur"))

# Some detail values are keys rather than numbers, and a key printed raw is
# the same defect as a missing label: English in the middle of a Spanish
# screen. These two sets say which keys hold what.
_WINDOW_SOURCE = {
    "CALL_PAGE": "La convocatoria publicada",
    "CALL_PAGE_UNAVAILABLE": "El periodo del expediente (la convocatoria no estaba accesible)",
    "NO_CALL_PAGE": "El periodo del expediente (no se registró ninguna convocatoria)",
}
_FIELD_PATH_KEYS = ("field_path", "missing_field")


def detail_rows(detail: dict[str, Any] | None) -> list[tuple[str, str]]:
    """A finding's structured detail as label/value pairs a person can read."""
    if not detail:
        return []
    rows: list[tuple[str, str]] = []
    for key, value in detail.items():
        label = _DETAIL_LABELS.get(key, key.replace("_", " "))
        rows.append((label, _format_detail(key, value)))
    return rows


def _format_detail(key: str, value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "sí" if value else "no"
    if isinstance(value, (list, tuple)):
        return ", ".join(_format_detail(key, v) for v in value)
    if key == "window_source":
        return _WINDOW_SOURCE.get(str(value), str(value))
    if key in _FIELD_PATH_KEYS:
        return field_label(str(value))
    if key in _EUR_KEYS or key.endswith("_eur"):
        try:
            return f"{money(Decimal(str(value)))} €"
        except Exception:
            return str(value)
    return str(value)


def money(value: Decimal | float | int | None) -> str:
    """Spanish money formatting: thousands with dots, decimals with a comma."""
    if value is None:
        return "—"
    quantised = Decimal(str(value)).quantize(Decimal("0.01"))
    whole, _, fraction = f"{quantised:.2f}".partition(".")
    negative = whole.startswith("-")
    digits = whole.lstrip("-")
    grouped = ""
    while len(digits) > 3:
        grouped = "." + digits[-3:] + grouped
        digits = digits[:-3]
    return ("-" if negative else "") + digits + grouped + "," + fraction


def spanish_date(value: date | datetime | str | None) -> str:
    """`dd/mm/aaaa`, which is how a date is written and read in Spanish."""
    if value is None:
        return "—"
    if isinstance(value, str):
        try:
            parsed: date = date.fromisoformat(value[:10])
        except ValueError:
            # Not a date after all: show whatever was stored rather than hide it.
            return value
    elif isinstance(value, datetime):
        parsed = value.date()
    else:
        parsed = value
    return parsed.strftime("%d/%m/%Y")


# --------------------------------------------------------------------------
# Locators
# --------------------------------------------------------------------------


def locator_summary(locator: dict[str, Any]) -> str:
    """One short line naming where a value came from, in Spanish."""
    kind = locator.get("kind")
    if kind == "PDF_PAGE":
        page = locator.get("page")
        if locator.get("char_start") is not None:
            return f"PDF · página {page}, caracteres {locator['char_start']}-{locator['char_end']}"
        return f"PDF · página {page}"
    if kind == "OCR_WORD_BOX":
        # The confidence has its own column on the review screen, so repeating
        # it here only made the cell long enough to wrap onto two lines.
        return f"Escaneo · página {locator.get('page')}"
    if kind == "EXCEL_CELL":
        return f"Excel · hoja «{locator.get('sheet')}», celda {locator.get('cell')}"
    if kind == "API_FIELD":
        return f"API de personal · {locator.get('json_path')}"
    if kind == "HTML_SELECTOR":
        return f"Página de convocatoria · campo «{_selector_field(locator)}»"
    if kind == "DERIVED":
        count = len(locator.get("inputs") or ())
        return f"Calculado por el sistema desde {count} valores"
    return str(kind)


LOCATOR_KIND = {
    "PDF_PAGE": "Texto nativo de un PDF",
    "OCR_WORD_BOX": "OCR sobre un escaneo",
    "EXCEL_CELL": "Celda de un libro de Excel",
    "API_FIELD": "Campo de una respuesta de API",
    "HTML_SELECTOR": "Selector sobre una página HTML",
    "DERIVED": "Valor calculado a partir de otros",
}


def _selector_field(locator: dict[str, Any]) -> str:
    selector = str(locator.get("selector") or "")
    match = re.search(r'data-field="([^"]+)"', selector)
    return match.group(1) if match else selector
