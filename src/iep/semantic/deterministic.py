"""The default semantic provider: rules, not a model.

This exists so that the demo, the tests and the evaluation harness all run
without a network call, a key or a bill, and so that a regression in the
pipeline is never confused with a change in a model's behaviour. It implements
the same protocol as the hosted adapter, produces the same grounded proposals,
and is what `IEP_SEMANTIC_PROVIDER=deterministic` selects.

It is not a stub. Classification and field location here are good enough for
the corpus, which is exactly the honest observation to make about the
technique: on documents this structured, rules beat a model on cost, latency
and determinism. The adapter earns its place on documents that are not.
"""

from __future__ import annotations

import re

from iep.domain.enums import DocumentKind
from iep.extraction import parse
from iep.semantic.protocol import (
    SemanticOutcome,
    SemanticProposal,
    SemanticRequest,
    SemanticResult,
    SemanticUsage,
    grounded_proposals,
    hash_config,
)

VERSION = "deterministic/1.1.0"

_KIND_SIGNALS: dict[DocumentKind, tuple[tuple[str, float], ...]] = {
    DocumentKind.TECHNICAL_REPORT: (
        (r"memoria\s+tecnica", 0.55),
        (r"descripcion\s+del\s+proyecto", 0.2),
        (r"resumen\s+economico", 0.2),
        (r"personal\s+investigador", 0.15),
    ),
    DocumentKind.EXPENSE_INVOICE: (
        (r"\bfactura\b", 0.5),
        (r"base\s+imponible", 0.25),
        (r"\biva\b", 0.15),
        (r"total\s+factura", 0.2),
    ),
    DocumentKind.TIMESHEET: (
        (r"partes?\s+horarios?", 0.5),
        (r"\bhoras\b", 0.2),
        (r"tarifa", 0.2),
        (r"id\s+empleado", 0.2),
    ),
}

_FIELD_PATTERNS: dict[str, str] = {
    "report.project_code": r"expediente",
    "report.title": r"titulo\s+del\s+proyecto",
    "report.call_code": r"convocatoria",
    "report.period": r"periodo\s+de\s+ejecucion",
    "report.declared_personnel_cost_eur": r"coste\s+de\s+personal\s+declarado",
    "report.declared_external_cost_eur": r"colaboraciones\s+externas\s+declaradas",
    "report.declared_total_eur": r"total\s+declarado",
    "invoice.number": r"numero",
    "invoice.issue_date": r"fecha\s+de\s+emision",
    "invoice.supplier_name": r"proveedor",
    "invoice.supplier_tax_id": r"nif(?:\s*\([^)]*\))?",
    "invoice.project_code": r"referencia\s+de\s+proyecto",
    "invoice.base_eur": r"base\s+imponible",
    "invoice.vat_eur": r"iva\s*\d{0,2}\s*%?",
    "invoice.total_eur": r"total\s+factura",
}


class DeterministicSemanticExtractor:
    name = "deterministic"
    version = VERSION

    def config_hash(self) -> str:
        return hash_config(
            {
                "version": VERSION,
                "kind_signals": {str(k): v for k, v in _KIND_SIGNALS.items()},
                "field_patterns": _FIELD_PATTERNS,
            }
        )

    def run(self, request: SemanticRequest) -> SemanticOutcome:
        text = request.truncated_text
        folded = _fold(text)

        kind, kind_confidence = self._classify(folded, request.candidate_kinds)
        proposals = tuple(self._propose(text, folded, spec.field_path) for spec in request.fields)
        found = tuple(p for p in proposals if p is not None)

        result = SemanticResult(
            document_kind=kind,
            kind_confidence=kind_confidence,
            proposals=found,
            notes=(),
        )
        # The same grounding check the hosted adapter goes through. It should
        # never reject anything here, and the test asserts that it does not.
        kept, rejected = grounded_proposals(result, text)
        return SemanticOutcome(
            result=result.model_copy(update={"proposals": kept}),
            usage=SemanticUsage(attempts=1, rejected_responses=0),
            provider=self.name,
            provider_version=self.version,
            config_hash=self.config_hash(),
            warnings=rejected,
        )

    def _classify(
        self, folded: str, candidates: tuple[DocumentKind, ...]
    ) -> tuple[DocumentKind, float]:
        scores: dict[DocumentKind, float] = {}
        for kind in candidates:
            score = 0.0
            for pattern, weight in _KIND_SIGNALS.get(kind, ()):
                if re.search(pattern, folded):
                    score += weight
            scores[kind] = min(score, 1.0)

        if not scores or max(scores.values()) == 0.0:
            return DocumentKind.UNKNOWN, 0.0

        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        best_kind, best_score = ranked[0]
        runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
        # A narrow margin between the top two is ambiguity, and ambiguity is
        # reported as low confidence so the document goes to a human.
        margin = best_score - runner_up
        confidence = min(best_score, 0.55 + margin) if margin < 0.2 else best_score
        return best_kind, round(min(confidence, 1.0), 3)

    def _propose(self, text: str, folded: str, field_path: str) -> SemanticProposal | None:
        pattern = _FIELD_PATTERNS.get(field_path)
        if pattern is None:
            return None
        found = parse.find_labelled(folded, pattern)
        if found is None:
            return None
        _, start, end = found
        # Slice the original text at the offsets found in the folded copy: the
        # fold is character-preserving, so offsets line up and the quote keeps
        # its original accents and casing.
        raw_value = parse.normalise_whitespace(text[start:end])
        if not raw_value:
            return None
        return SemanticProposal(
            field_path=field_path,
            value=raw_value[:500],
            evidence_quote=raw_value[:500],
            confidence=0.9,
        )


_ACCENTS = str.maketrans("áéíóúüñÁÉÍÓÚÜÑ", "aeiouunAEIOUUN")


def _fold(text: str) -> str:
    """Lower-case and strip accents without changing the character count."""
    return text.translate(_ACCENTS).lower()
