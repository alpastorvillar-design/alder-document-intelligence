# Validation strategy

## Separation of responsibilities

Extractors answer “what value appears where?” Semantic providers may classify a
document and propose grounded candidates. Versioned deterministic rules answer
“is this dossier internally consistent?” Only a reviewer may approve it.

Rules cover required evidence, OCR confidence, formula-bearing workbooks,
duplicate submissions and invoice numbers, invoice arithmetic and dates,
timesheet limits and arithmetic, registry identity/rate/contract checks, call
eligibility and maximum funding, cross-document totals, ambiguous readings,
malicious-instruction indicators, and unavailable external sources.

An external connector failure is a blocker, not an empty dataset. A failed
document is a blocker, not an absent one. Rejected extraction candidates do not
participate in arithmetic. When several live readings disagree, the ambiguity
is visible and blocks approval.

## Human gate

Every successful processing run ends in `NEEDS_REVIEW`. A reviewer can confirm
or correct fields and accept or dismiss findings, always with actor and reason.
Corrections preserve the original value. Revision numbers make stale field edits
fail with a conflict instead of silently overwriting a concurrent decision.

Approval fails while any field needs review or any blocker is open or accepted.
An accepted blocker means the issue is real; it is not a waiver. Dismissing a
false-positive blocker requires a reason. Approved dossiers are immutable.

## Tests and metrics

Unit tests pin each rule's intended result. PostgreSQL tests cover constraints,
conditional transitions, idempotency, competing workers, lease fencing, and
recovery. The OCR path is tested with Tesseract in CI. The evaluation harness
compares extracted fields and finding rule ids to independently written ground
truth, reporting field accuracy and incident precision/recall, including false
positives and false negatives.
