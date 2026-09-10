**English** · [Español](es/modelo-de-dominio.md)

# Domain model

Every object below is a Pydantic contract in
[`src/iep/domain/contracts.py`](../src/iep/domain/contracts.py) and a table in
[`src/iep/db/models.py`](../src/iep/db/models.py). `CONTRACT_VERSION` is stamped
onto every stored extraction, so a row can always say which shape it was
written under.

## Entities

### Dossier

One funding claim under review. Carries the business reference
(`INN-YYYY-NNN`), the project period, the claimed total, an optional published
call page, and its state. The reference is unique: re-submitting one is a
replay of an existing dossier, never a second one.

### Document

Anything the conclusions can be traced to, whatever its origin: an upload, a
registry snapshot, or a captured page. Records the caller's filename (display
only), the declared media type (recorded, not trusted), the media kind decided
by signature and parser, the classified kind, size, SHA-256, storage key, page
count, and — when it was refused — why.

A refused document has no bytes in the object store. It is recorded so a
reviewer sees what was submitted rather than wondering what is missing.

### Extraction

One field, one value, one place it came from:

| Field | Why it exists |
| --- | --- |
| `field_path` | what was read, e.g. `invoice.total_eur`, `timesheet.rows[3].hours` |
| `value_text` / `value_number` / `value_date` | the typed value; amounts are `Decimal` |
| `locator` | the exact place — see below |
| `method` | PDF text, OCR, workbook cell, HTTP, HTML, aggregated, human |
| `extractor_version` | which reader produced it |
| `contract_version` | which shape it was written under |
| `confidence` | what routes it to a person |
| `status` | extracted, needs review, confirmed, corrected, rejected |
| `original_value_text` | what the machine read, kept when a human disagrees |
| `corrected_by` / `corrected_at` / `correction_reason` | who, when, why |
| `dedup_key` | makes reprocessing an update rather than a duplicate |

### Evidence locator

A discriminated union, so an impossible combination cannot be constructed:

| Kind | Carries |
| --- | --- |
| `PDF_PAGE` | page, character span, snippet |
| `OCR_WORD_BOX` | page, box, the engine's word confidence, snippet |
| `EXCEL_CELL` | sheet, A1 reference, row, column |
| `API_FIELD` | endpoint, record id, JSON path, contract version |
| `HTML_SELECTOR` | URL, CSS selector, capture time, snippet |
| `DERIVED` | the extraction ids that were combined, and the rule |

### Validation finding

What a rule concluded about the dossier: rule id, rule version, severity
(`BLOCKER` / `WARNING` / `INFO`), message, structured detail, the extractions
and documents it points at, status, and how a reviewer resolved it. Its
`fingerprint` is a hash of rule plus subject, so re-running validation refreshes
the same row instead of appending a near-duplicate.

### Review decision

An append-only record of a human action: correct, confirm, accept, dismiss,
approve, reject — with actor, reason and timestamp.

### Processing job

A queue row: dossier, type, status, attempts, ceiling, payload, idempotency
key, availability time, lease and holder, last error.

### Audit event

Append-only. Action, actor, correlation id, structured payload, timestamp.
Written inside the transaction of the change it describes, so a rollback cannot
leave a record claiming something happened.

### Report

A rendered HTML report with its content hash, the dossier state at the time, and
the counts it was generated from.

### Document chunk

A bounded text segment tied to a dossier and document, with its original
locator, Spanish full-text search vector and an optional dimensionless
`vector` embedding.
Provider, model, configuration hash and timestamp make re-indexing inspectable
and prevent queries from mixing incompatible vector spaces.

### API error

The only failure shape a client sees: `error`, `message`, `correlation_id`, and
an optional `detail` object. No stack traces, ever.

## Field paths in this domain

| Prefix | Source | Examples |
| --- | --- | --- |
| `report.*` | technical report, PDF text | `project_code`, `period_start`, `declared_total_eur` |
| `invoice.*` | scanned receipt, OCR | `number`, `issue_date`, `base_eur`, `vat_eur`, `total_eur` |
| `timesheet.rows[n].*` | workbook cells | `employee_id`, `month`, `hours`, `hourly_rate_eur`, `amount_eur` |
| `invoices.*` | derived | `total_eur`, `count` |
| `timesheet.*` | derived | `total_amount_eur`, `row_count` |
| `call.*` | published page | `eligible_from`, `eligible_to`, `max_funding_eur` |

## Deliberate omissions

There is no `user`, no `organisation` and no `tenant`. Reviewer identity is a
string supplied by the caller, which is honest about there being no
authentication: see [threat-model.md](threat-model.md) and
[production-gap.md](production-gap.md). An unauthenticated `user` table would
look like access control without being any.
