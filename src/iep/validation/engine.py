"""Persisting findings.

Re-running validation must not multiply findings, and must not erase a
reviewer's decision. Both follow from the fingerprint: a finding is upserted by
`(dossier_id, fingerprint)`, an existing row keeps its status and its
resolution, and a finding that the evidence no longer supports is closed as
RESOLVED rather than deleted, so the audit trail still shows it was raised.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from iep.db.models import Finding
from iep.domain.enums import FindingStatus, Severity
from iep.validation.rules import RULES_VERSION, RuleFinding


@dataclass(frozen=True)
class ValidationSummary:
    created: int
    refreshed: int
    auto_resolved: int
    open_blockers: int
    open_warnings: int
    open_total: int

    @property
    def needs_review(self) -> bool:
        return self.open_total > 0


def persist(
    session: Session, dossier_id: uuid.UUID, findings: list[RuleFinding]
) -> ValidationSummary:
    existing = {
        row.fingerprint: row
        for row in session.execute(
            select(Finding).where(Finding.dossier_id == dossier_id)
        ).scalars()
    }
    produced = {finding.fingerprint: finding for finding in findings}

    created = 0
    refreshed = 0
    for fingerprint, finding in produced.items():
        row = existing.get(fingerprint)
        if row is None:
            session.add(
                Finding(
                    id=uuid.uuid4(),
                    dossier_id=dossier_id,
                    rule_id=finding.rule_id,
                    rule_version=RULES_VERSION,
                    severity=str(finding.severity),
                    status=FindingStatus.OPEN,
                    message=finding.message,
                    detail=dict(finding.detail),
                    extraction_ids=[str(i) for i in finding.extraction_ids],
                    document_ids=[str(i) for i in finding.document_ids],
                    fingerprint=fingerprint,
                )
            )
            created += 1
            continue

        # Refresh the evidence but leave a reviewer's decision alone.
        row.message = finding.message
        row.detail = dict(finding.detail)
        row.extraction_ids = [str(i) for i in finding.extraction_ids]
        row.document_ids = [str(i) for i in finding.document_ids]
        row.rule_version = RULES_VERSION
        if row.status == FindingStatus.RESOLVED:
            # It came back, so it is open again.
            row.status = FindingStatus.OPEN
            row.resolved_by = None
            row.resolved_at = None
            row.resolution_note = None
        refreshed += 1

    auto_resolved = 0
    for fingerprint, row in existing.items():
        if fingerprint in produced or row.status != FindingStatus.OPEN:
            continue
        row.status = FindingStatus.RESOLVED
        row.resolution_note = "No longer reported by the current evidence."
        auto_resolved += 1

    session.flush()

    open_rows = list(
        session.execute(
            select(Finding).where(
                Finding.dossier_id == dossier_id,
                Finding.status.in_([FindingStatus.OPEN, FindingStatus.ACCEPTED]),
            )
        ).scalars()
    )
    return ValidationSummary(
        created=created,
        refreshed=refreshed,
        auto_resolved=auto_resolved,
        open_blockers=sum(1 for r in open_rows if r.severity == Severity.BLOCKER),
        open_warnings=sum(1 for r in open_rows if r.severity == Severity.WARNING),
        open_total=len(open_rows),
    )
