"""Operator entry points.

    iep seed --corpus corpus/out          create dossiers and upload their documents
    iep process --reference INN-2025-042  run the pipeline synchronously
    iep report --reference INN-2025-042   render and store the HTML report
    iep reset --reference INN-2025-042    delete one dossier so it can be seeded again
    iep status                            what is in the database right now

`process` runs the same pipeline the worker runs, in the foreground. It exists
so the pipeline can be exercised and timed without a worker, which is what the
evaluation harness and the smoke test use.
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select

from iep.config import get_settings
from iep.db.models import Document, Dossier, Extraction, Finding, ProcessingJob
from iep.db.session import session_scope
from iep.domain.contracts import DossierCreate
from iep.domain.enums import DossierStatus
from iep.dossiers import service as dossiers
from iep.ingestion.service import IngestionRejectedError, accepts_documents, ingest_upload
from iep.logging import configure_logging
from iep.pipeline.processor import finalise_state, process_dossier
from iep.reporting import render
from iep.semantic.protocol import SemanticProviderError
from iep.storage.local import LocalObjectStore
from iep.worker.runner import build_semantic_provider

MEDIA_TYPES = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".txt": "text/plain",
}


def seed(corpus_dir: Path, *, call_page_url: str | None) -> int:
    settings = get_settings()
    store = LocalObjectStore(settings.storage_root)
    manifest = json.loads((corpus_dir / "manifest.json").read_text(encoding="utf-8"))

    created = 0
    for entry in manifest["dossiers"]:
        reference = entry["reference"]
        with session_scope() as session:
            dossier = dossiers.get_by_reference(session, reference)
            if dossier is None:
                dossier = dossiers.create(
                    session,
                    DossierCreate(
                        reference=reference,
                        title=entry["title"],
                        period_start=entry["period_start"],
                        period_end=entry["period_end"],
                        claimed_total_eur=Decimal(entry["claimed_total_eur"]),
                        call_page_url=call_page_url,
                    ),
                )
                created += 1

            if not accepts_documents(dossier):
                # A dossier under review or already approved refuses new
                # documents by design. Seeding twice is a normal thing to do
                # while demonstrating, so say so and move on rather than
                # failing with a traceback.
                print(
                    f"{reference}: already {dossier.status}; documents were not re-submitted. "
                    "Reset with `iep reset --reference <ref>` to seed it again."
                )
                continue

            accepted = rejected = duplicates = 0
            for path in sorted((corpus_dir / reference).iterdir()):
                if path.name == "ground_truth.json" or not path.is_file():
                    continue
                try:
                    result = ingest_upload(
                        session,
                        store,
                        settings,
                        dossier=dossier,
                        filename=path.name,
                        declared_media_type=MEDIA_TYPES.get(path.suffix.lower()),
                        data=path.read_bytes(),
                    )
                except IngestionRejectedError as exc:
                    rejected += 1
                    print(f"  rejected {path.name}: {exc.reason}")
                    continue
                if result.is_duplicate:
                    duplicates += 1
                else:
                    accepted += 1
            print(f"{reference}: {accepted} accepted, {duplicates} duplicate, {rejected} rejected")
    print(f"{created} dossier(s) created")
    return 0


def reset(reference: str) -> int:
    """Delete a dossier and everything traced to it.

    Demonstrations get run more than once, and a dossier under review refuses
    new documents by design. This is the deliberate way back to a clean start;
    it is destructive and names exactly one dossier so it cannot be mistaken
    for a cleanup.
    """
    with session_scope() as session:
        dossier = dossiers.get_by_reference(session, reference)
        if dossier is None:
            print(f"no dossier with reference {reference}", file=sys.stderr)
            return 1
        session.delete(dossier)
    print(f"{reference}: deleted; seed it again to start from a clean state")
    return 0


def process(reference: str, *, provider: str | None) -> int:
    settings = get_settings()
    if provider:
        settings = settings.model_copy(update={"semantic_provider": provider})
    store = LocalObjectStore(settings.storage_root)
    try:
        semantic = build_semantic_provider(settings)
    except SemanticProviderError as exc:
        # Selecting the hosted provider without a key is a configuration
        # mistake, not a crash. Say what is missing.
        print(f"cannot use the '{settings.semantic_provider}' provider: {exc}", file=sys.stderr)
        return 3

    with session_scope() as session:
        dossier = dossiers.get_by_reference(session, reference)
        if dossier is None:
            print(f"no dossier with reference {reference}", file=sys.stderr)
            return 1
        # Walk the same states the worker walks, so a foreground run and a
        # queued run leave the dossier in the same place.
        if DossierStatus(dossier.status) not in (
            DossierStatus.INGESTED,
            DossierStatus.NEEDS_REVIEW,
            DossierStatus.FAILED,
        ):
            print(
                f"dossier {reference} cannot be processed from {dossier.status}",
                file=sys.stderr,
            )
            return 2
        if DossierStatus(dossier.status) in (
            DossierStatus.INGESTED,
            DossierStatus.NEEDS_REVIEW,
            DossierStatus.FAILED,
        ):
            dossiers.transition(session, dossier, DossierStatus.QUEUED, reason="cli process")
        if DossierStatus(dossier.status) is DossierStatus.QUEUED:
            dossiers.transition(session, dossier, DossierStatus.PROCESSING, reason="cli process")

        result = process_dossier(session, store, settings, dossier=dossier, semantic=semantic)
        status = finalise_state(session, dossier)

    print(
        json.dumps(
            {
                "reference": reference,
                "status": str(status),
                "documents_processed": result.documents_processed,
                "documents_failed": result.documents_failed,
                "extractions": result.extractions_written,
                "chunks": result.chunks_written,
                "findings_open": result.summary.open_total,
                "findings_blockers": result.summary.open_blockers,
                "findings_created": result.summary.created,
                "registry_people": result.registry_people,
                "semantic_provider": result.semantic_provider,
                "semantic_config_hash": result.semantic_config_hash,
                "stage_seconds": result.per_stage_seconds,
                "warnings": result.warnings,
            },
            indent=2,
        )
    )
    return 0


def report(reference: str) -> int:
    settings = get_settings()
    with session_scope() as session:
        dossier = dossiers.get_by_reference(session, reference)
        if dossier is None:
            print(f"no dossier with reference {reference}", file=sys.stderr)
            return 1
        rendered = render.render_html(session, dossier.id)
        row = render.persist(session, dossier.id, rendered, report_root=settings.report_root)
        print(
            json.dumps(
                {
                    "reference": reference,
                    "report_id": str(row.id),
                    "file": str(settings.report_root / row.storage_key),
                    "content_sha256": rendered.content_sha256,
                    "findings": rendered.finding_count,
                    "blockers": rendered.blocker_count,
                },
                indent=2,
            )
        )
    return 0


def status() -> int:
    with session_scope() as session:
        counts = {
            "dossiers": session.execute(select(func.count()).select_from(Dossier)).scalar_one(),
            "documents": session.execute(select(func.count()).select_from(Document)).scalar_one(),
            "extractions": session.execute(
                select(func.count()).select_from(Extraction)
            ).scalar_one(),
            "findings": session.execute(select(func.count()).select_from(Finding)).scalar_one(),
            "jobs": session.execute(select(func.count()).select_from(ProcessingJob)).scalar_one(),
        }
        by_status: dict[str, int] = {
            str(value): int(count)
            for value, count in session.execute(
                select(Dossier.status, func.count()).group_by(Dossier.status)
            ).all()
        }
    print(json.dumps({"counts": counts, "dossiers_by_status": by_status}, indent=2, default=str))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="iep", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    seed_parser = sub.add_parser("seed", help="create dossiers and upload corpus documents")
    seed_parser.add_argument("--corpus", type=Path, default=Path("corpus/out"))
    seed_parser.add_argument(
        "--call-page-url",
        default=None,
        help="URL of the published call page; must be in the scraper allowlist",
    )

    process_parser = sub.add_parser("process", help="run the pipeline in the foreground")
    process_parser.add_argument("--reference", required=True)
    process_parser.add_argument("--provider", choices=("deterministic", "llm"), default=None)

    report_parser = sub.add_parser("report", help="render and store the HTML report")
    report_parser.add_argument("--reference", required=True)

    reset_parser = sub.add_parser(
        "reset", help="delete one dossier so it can be seeded again (destructive)"
    )
    reset_parser.add_argument("--reference", required=True)

    sub.add_parser("status", help="counts currently in the database")

    args = parser.parse_args()
    configure_logging(get_settings().log_level)

    if args.command == "seed":
        return seed(args.corpus, call_page_url=args.call_page_url)
    if args.command == "process":
        return process(args.reference, provider=args.provider)
    if args.command == "report":
        return report(args.reference)
    if args.command == "reset":
        return reset(args.reference)
    return status()


if __name__ == "__main__":
    sys.exit(main())
