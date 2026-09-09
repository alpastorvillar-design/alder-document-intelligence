"""Generate the synthetic corpus and its ground truth.

    python -m corpus.generate --out corpus/out

Output is deterministic: the same command produces byte-identical files, which
is what lets the evaluation harness compare a measured result against a fixed
expectation rather than against whatever was generated this morning.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from corpus import documents
from corpus.dataset import (
    CALL_CODE,
    CALL_MAX_FUNDING,
    CALL_PERIOD_END,
    CALL_PERIOD_START,
    CORPUS_VERSION,
    DOSSIERS,
    REGISTRY_PEOPLE,
    DossierSpec,
)


@dataclass(frozen=True)
class Artefact:
    filename: str
    media_type: str
    data: bytes
    role: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


def build_artefacts(spec: DossierSpec) -> list[Artefact]:
    # Rendered once and reused: the duplicate below has to be byte-identical,
    # or it is a different document rather than a re-submission.
    report_pdf = documents.technical_report(spec)
    artefacts: list[Artefact] = [
        Artefact("memoria-tecnica.pdf", "application/pdf", report_pdf, "technical_report"),
        Artefact(
            "partes-horarios.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            documents.timesheet_workbook(spec),
            "timesheet",
        ),
    ]

    for index, invoice in enumerate(spec.invoices, start=1):
        data = documents.scanned_invoice(invoice, reference=spec.reference, seed=index * 7919)
        suffix = "png" if invoice.jpeg_quality >= 90 else "jpg"
        media = "image/png" if suffix == "png" else "image/jpeg"
        artefacts.append(
            Artefact(
                f"justificante-{index:02d}-{invoice.invoice_number}.{suffix}",
                media,
                data,
                "expense_invoice",
            )
        )

    if spec.include_injection_page:
        artefacts.append(
            Artefact(
                "anexo-iv-notas-proveedor.pdf",
                "application/pdf",
                documents.injection_document(spec),
                "adversarial_annex",
            )
        )
    if spec.include_duplicate_report:
        artefacts.append(
            Artefact(
                "memoria-tecnica-copia.pdf",
                "application/pdf",
                report_pdf,
                "duplicate_of_technical_report",
            )
        )
    if spec.include_corrupt_pdf:
        artefacts.append(
            Artefact(
                "justificante-danado.pdf", "application/pdf", documents.corrupt_pdf(), "corrupt"
            )
        )
    if spec.include_unsupported_file:
        artefacts.append(
            Artefact(
                "notas-internas.txt", "text/plain", documents.unsupported_file(), "unsupported"
            )
        )
    return artefacts


def ground_truth(spec: DossierSpec, artefacts: list[Artefact]) -> dict[str, object]:
    """What a correct run should find, expressed independently of the code."""
    return {
        "reference": spec.reference,
        "title": spec.title,
        "period_start": spec.period_start.isoformat(),
        "period_end": spec.period_end.isoformat(),
        "claimed_total_eur": str(spec.claimed_total_eur),
        "report_declared_total_eur": str(spec.declared_total_eur),
        "fields": {
            "report.project_code": spec.reference,
            "report.title": spec.title,
            "report.period_start": spec.period_start.isoformat(),
            "report.period_end": spec.period_end.isoformat(),
            "report.declared_personnel_cost_eur": str(spec.declared_personnel_cost_eur),
            "report.declared_external_cost_eur": str(spec.declared_external_cost_eur),
            "report.declared_total_eur": str(spec.declared_total_eur),
            "timesheet.total_amount_eur": str(spec.timesheet_total_eur),
            "timesheet.row_count": str(len(spec.timesheet)),
            "invoices.total_eur": str(spec.invoice_total_eur),
            "invoices.count": str(len(spec.invoices)),
        },
        "invoices": [
            {
                "invoice_number": invoice.invoice_number,
                "issue_date": invoice.issue_date.isoformat(),
                "supplier_name": invoice.supplier_name,
                "base_eur": str(invoice.base_eur),
                "vat_eur": str(invoice.vat_eur),
                "total_eur": str(invoice.total_eur),
                "project_code": invoice.project_code,
                "hard_to_read": invoice.jpeg_quality < 60,
            }
            for invoice in spec.invoices
        ],
        "timesheet": [
            {
                "employee_id": row.employee_id,
                "full_name": row.full_name,
                "month": row.month,
                "hours": str(row.hours),
                "hourly_rate_eur": str(row.hourly_rate_eur),
                "amount_eur": str(row.amount_eur),
            }
            for row in spec.timesheet
        ],
        "expected_findings": sorted(spec.expected_findings),
        "documents": [
            {
                "filename": a.filename,
                "media_type": a.media_type,
                "role": a.role,
                "sha256": a.sha256,
                "size_bytes": len(a.data),
            }
            for a in artefacts
        ],
        "notes": spec.notes,
    }


def registry_snapshot() -> dict[str, object]:
    return {
        "contract_version": "registry/v1",
        "people": [
            {
                "employee_id": p.employee_id,
                "full_name": p.full_name,
                "role": p.role,
                "hourly_rate_eur": str(p.hourly_rate_eur),
                "contract_start": p.contract_start.isoformat(),
                "contract_end": p.contract_end.isoformat() if p.contract_end else None,
            }
            for p in REGISTRY_PEOPLE
        ],
    }


def call_snapshot() -> dict[str, object]:
    return {
        "call_code": CALL_CODE,
        "eligible_from": CALL_PERIOD_START.isoformat(),
        "eligible_to": CALL_PERIOD_END.isoformat(),
        "max_funding_eur": str(CALL_MAX_FUNDING),
        "status": "OPEN",
    }


def write_corpus(out_dir: Path) -> dict[str, object]:
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, object] = {
        "corpus_version": CORPUS_VERSION,
        "call": call_snapshot(),
        "registry": registry_snapshot(),
        "dossiers": [],
    }

    for spec in DOSSIERS:
        target = out_dir / spec.reference
        target.mkdir(parents=True, exist_ok=True)
        artefacts = build_artefacts(spec)
        for artefact in artefacts:
            (target / artefact.filename).write_bytes(artefact.data)

        truth = ground_truth(spec, artefacts)
        (target / "ground_truth.json").write_text(
            json.dumps(truth, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
        )
        dossiers_list = manifest["dossiers"]
        assert isinstance(dossiers_list, list)
        dossiers_list.append(truth)

    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("corpus/out"))
    args = parser.parse_args()

    manifest = write_corpus(args.out)
    dossiers = manifest["dossiers"]
    assert isinstance(dossiers, list)
    total_bytes = 0
    for entry in dossiers:
        docs = entry["documents"]
        assert isinstance(docs, list)
        size = sum(int(d["size_bytes"]) for d in docs)
        total_bytes += size
        print(f"{entry['reference']}: {len(docs)} documents, {size} bytes")
    print(f"total: {total_bytes} bytes -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
