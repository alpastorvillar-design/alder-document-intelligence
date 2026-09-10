**English** · [Español](README.es.md)

# Innovation Evidence Pipeline

Reviewing an innovation funding claim is a document problem before it is a data
problem. A single dossier arrives as a technical report in PDF, a pile of
scanned expense receipts, a timesheet workbook, a record in a corporate system,
and a published call for proposals on a web page. Someone has to decide whether
the declared spend is actually supported — and, if a claim is later challenged,
show where every figure came from.

This repository is a **production-oriented reference implementation** of that
review step: it ingests heterogeneous documents, extracts fields while keeping a
locator back to the exact page, cell or bounding box they came from, cross-checks
the sources against each other with deterministic rules, routes what it cannot
settle to a human, and produces an auditable report.

It is a reference implementation, not a deployed system. See
[docs/production-gap.md](docs/production-gap.md) for what would have to change
before it ran against real dossiers.

## The design decision that matters

A language model is genuinely useful here — classifying documents, pulling a
project title out of prose, spotting that two sections contradict each other.
It is also the wrong tool for deciding whether €184,320 of declared personnel
cost matches the timesheet.

So the pipeline splits the work:

| Concern | Handled by |
| --- | --- |
| Locating text, cells, and words on a scan | Deterministic extractors (PyMuPDF, openpyxl, Tesseract) |
| Interpreting prose, classifying, proposing candidate fields | A pluggable semantic provider |
| Finding supporting passages | Lexical, exact pgvector, or hybrid retrieval |
| Drafting an answer from retrieved passages | Optional read-only RAG provider with verified citation ids |
| Arithmetic, eligibility, duplicates, cross-source reconciliation | Deterministic, versioned rules |
| Anything ambiguous, low-confidence or contradictory | A human reviewer, with the evidence in front of them |
| Approving or rejecting | A human, recorded in an append-only audit trail |

Every extracted field carries its source document, its locator, the extractor
that produced it, that extractor's version, the contract version, a confidence
score, and any human correction. Nothing in the pipeline can approve a dossier.

## Status

The vertical slice is implemented and exercised by unit, PostgreSQL integration,
migration, recovery, and container smoke tests. Published measurements come
from the evaluation harness rather than being copied into this page; see
[measured results](docs/measured-results.md).

## Quickstart

Prerequisites are Docker Engine with Compose v2 and enough free space for the
pinned base images. No external service or model credential is required.

```bash
cp .env.example .env
docker compose up -d --build --wait postgres devsources api worker
docker compose exec -T api python -m corpus.generate --out /tmp/corpus
docker compose exec -T api iep seed --corpus /tmp/corpus \
  --call-page-url http://devsources:8080/public/convocatoria.html
docker compose exec -T api iep process --reference INN-2025-042
docker compose exec -T api iep report --reference INN-2025-042
```

On Windows, [`scripts/demo.ps1`](scripts/demo.ps1) performs those steps for
both the consistent and deliberately defective dossiers; add `-Fresh` to delete
them first, since a dossier under review refuses new documents by design. Stop
only this stack with `docker compose --profile n8n down`; add `--volumes` when
its local data is no longer needed.

The API, review screen, and local source simulator bind only to loopback:
`http://127.0.0.1:8000/docs`, `http://127.0.0.1:8000/ui/dossiers`, and
`http://127.0.0.1:8080`. The optional workflow UI is described in
[`automation/n8n/README.md`](automation/n8n/README.md).

## Documentation

Every document exists in English and Spanish, with a switcher on its first line.
The full index is [docs/README.md](docs/README.md).

New here? Start with the **[guided walkthrough](docs/walkthrough.md)** — what is
running, what every endpoint does, how to launch the demo, how to open n8n, and
where retrieval and a language model do and do not fit.

- [Guided walkthrough](docs/walkthrough.md) and
  [LLM demonstration](docs/llm-demo.md), plus
  [hybrid retrieval and optional RAG](docs/rag.md)
- [Architecture](docs/architecture.md), [domain model](docs/domain-model.md),
  and [workflow](docs/workflow.md)
- [Ingestion and provenance](docs/ingestion-and-provenance.md),
  [validation](docs/validation-strategy.md), and [AI safety](docs/ai-safety.md)
- [Threat model](docs/threat-model.md), [operations](docs/operations.md), and
  [production gap](docs/production-gap.md)
- [Measurements](docs/measured-results.md), [business impact](docs/business-impact.md),
  [limitations](docs/limitations.md), and [demo guide](docs/demo.md)

## Licence

MIT. See [LICENSE](LICENSE).
