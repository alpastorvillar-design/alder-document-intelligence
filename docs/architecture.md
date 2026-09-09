**English** · [Español](es/arquitectura.md)

# Architecture

## Shape

A modular monolith. One codebase, one container image, two processes:

```
                         ┌──────────────┐
   HTTP client ─────────▶│     api      │──┐
   (portal, n8n, curl)   │  (FastAPI)   │  │
                         └──────────────┘  │
                                           │   same image,
                         ┌──────────────┐  │   same extractors,
                         │    worker    │◀─┘   different command
                         │ (poll loop)  │
                         └──────┬───────┘
                                │
        ┌───────────────────────┼───────────────────────┐
        ▼                       ▼                       ▼
 ┌─────────────┐        ┌──────────────┐        ┌──────────────┐
 │ PostgreSQL  │        │ object store │        │  external    │
 │ state, queue│        │ content-     │        │  sources     │
 │ audit, FTS  │        │ addressed    │        │ registry API │
 └─────────────┘        └──────────────┘        │ public page  │
                                                └──────────────┘
```

The API accepts work and answers questions. The worker does the slow parts:
opening PDFs, shelling out to Tesseract, reading workbooks, calling the
registry. They share the image so an extractor version is identical in both,
which is what makes a measured accuracy figure mean anything.

### Why a monolith

The pieces of this system change together. A new field means a new extractor,
a new contract, a new rule and a new column in the report; splitting those
across services would turn one commit into four deployments and a compatibility
window, and would buy nothing. Independent scaling would be the argument for
splitting, and the only component that would need it is the worker — which is
already a separate process that can be run N times against the same queue.

## Modules

| Module | Responsibility | Depends on |
| --- | --- | --- |
| `iep.domain` | contracts, enums, state machine | nothing |
| `iep.db` | tables, session, enum round-tripping | domain |
| `iep.storage` | object store interface and local backend | nothing |
| `iep.ingestion` | size, signature and parser checks; provenance | domain, db, storage |
| `iep.extraction` | PDF text, OCR, workbook, parsing, field readers | domain |
| `iep.semantic` | provider protocol, deterministic provider, hosted adapter | domain |
| `iep.connectors` | registry client, controlled page capture | domain |
| `iep.validation` | rule catalogue and finding persistence | domain, db, connectors |
| `iep.pipeline` | the per-dossier orchestration | everything above |
| `iep.review` | human decisions | domain, db |
| `iep.reporting` | HTML report, JSON and CSV export | domain, db |
| `iep.retrieval` | lexical evidence lookup | db |
| `iep.worker` | queue and the worker loop | pipeline |
| `iep.api` | HTTP surface, errors, idempotency, minimal review view | everything |

Dependencies point one way. `iep.domain` imports nothing from the project, so a
contract change is visible everywhere it matters and nowhere it does not.

## The path a dossier takes

1. **Create.** `POST /dossiers` with a reference, a period and a claimed total.
2. **Ingest.** Each upload is size-checked, signature-checked and opened by its
   parser before anything is stored. Accepted bytes go to the object store
   under their SHA-256; the row records size, type, hash, origin and page
   count. A refused file is still recorded, with its reason, without storing
   its bytes.
3. **Enqueue.** `POST /dossiers/{id}/process` inserts a job whose idempotency
   key defaults to the dossier plus the set of document digests it holds, so
   pressing the button twice with nothing changed is a no-op.
4. **Claim.** A worker takes the job with `SELECT ... FOR UPDATE SKIP LOCKED`
   and a lease.
5. **Capture.** The personnel registry is read over HTTP and the published call
   page is captured. Both are stored as documents with hashes, so a figure that
   came from a corporate system is as traceable as one read off a page.
6. **Read.** Per document: native PDF text if there is a usable text layer,
   otherwise OCR; workbooks through openpyxl. Each value is emitted with a
   locator — page and character span, sheet and cell, or bounding box and word
   confidence.
7. **Classify.** The semantic provider says what the document is. Only the
   classification is used; the provider's field proposals are not persisted as
   extractions.
8. **Aggregate.** Sums are computed from stored extractions and recorded with a
   derived locator naming the rule and the inputs.
9. **Validate.** Deterministic rules compare the report, the workbook, the
   receipts, the registry and the call page against each other.
10. **Review.** The dossier moves to `NEEDS_REVIEW` — always, whether or not
    anything was found. A person corrects, confirms, accepts, dismisses,
    approves or rejects, each with a reason, each recorded.
11. **Report.** HTML for a human, JSON and CSV for a system, both showing the
    evidence and any correction next to the original reading.

## Decisions worth arguing about

Recorded as ADRs in [`adr/`](adr/):

- [0001](adr/0001-evidence-locators.md) — every value carries where it came from
- [0002](adr/0002-postgres-job-queue.md) — the queue is a table, not a broker
- [0003](adr/0003-deterministic-rules-not-a-model.md) — the model does not do arithmetic
- [0004](adr/0004-lexical-retrieval-not-rag.md) — lexical search, and what would change that
- [0005](adr/0005-no-agent-in-the-approval-path.md) — no agent between a document and an approval

## What is not here

No message broker, no vector database, no orchestration engine in the critical
path, no microservices, no cloud. Each of those was considered and rejected in
the ADRs or in [limitations.md](limitations.md), and adding one without a
measured need would make the system harder to explain for no gain.
