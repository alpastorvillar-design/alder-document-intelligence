"""The answer a simulated hosted model gives.

Kept next to the corpus, not inside `iep`: nothing in the product may depend on
it. The development source simulator imports it to answer requests that the
official SDK sends to its `/v1/messages` endpoint.

It is honest about what it is. The reply is produced by reading the document
with the deterministic provider, so it is derived from the document rather than
canned - which makes the whole hosted-provider path exercisable for free. It
cannot demonstrate a model's judgement on prose that rules cannot parse; that
needs a real key, and `docs/llm-demo.md` gives the procedure.
"""

from __future__ import annotations

import json

from iep.domain.enums import DocumentKind
from iep.semantic.deterministic import DeterministicSemanticExtractor
from iep.semantic.protocol import SemanticFieldSpec, SemanticRequest

_FIELDS = (
    SemanticFieldSpec(
        field_path="report.project_code",
        description="the dossier reference the report belongs to",
        value_type="text",
    ),
    SemanticFieldSpec(
        field_path="report.title", description="the project title", value_type="text"
    ),
    SemanticFieldSpec(
        field_path="invoice.number", description="the invoice number", value_type="text"
    ),
    SemanticFieldSpec(
        field_path="invoice.total_eur",
        description="the total amount of the invoice as printed",
        value_type="number",
    ),
)

_CANDIDATE_KINDS = (
    DocumentKind.TECHNICAL_REPORT,
    DocumentKind.EXPENSE_INVOICE,
    DocumentKind.TIMESHEET,
)


def answer(document_text: str, *, malformed: bool = False) -> str:
    """A structured reply for `document_text`, as JSON text.

    `malformed` returns something the response schema rejects, so the adapter's
    validation and bounded retry can be watched rather than only asserted.
    """
    if malformed:
        return json.dumps(
            {
                "document_kind": "A FILE OF SOME SORT",
                "kind_confidence": "very",
                "proposals": "none",
            }
        )

    outcome = DeterministicSemanticExtractor().run(
        SemanticRequest(
            document_id="simulated",
            text=document_text,
            fields=_FIELDS,
            candidate_kinds=_CANDIDATE_KINDS,
        )
    )
    result = outcome.result
    return json.dumps(
        {
            "document_kind": str(result.document_kind),
            "kind_confidence": result.kind_confidence,
            "proposals": [
                {
                    "field_path": proposal.field_path,
                    "value": proposal.value,
                    "evidence_quote": proposal.evidence_quote,
                    "confidence": proposal.confidence,
                }
                for proposal in result.proposals
            ],
            "notes": ["answered by the local simulator, not by a hosted model"],
        },
        ensure_ascii=False,
    )
