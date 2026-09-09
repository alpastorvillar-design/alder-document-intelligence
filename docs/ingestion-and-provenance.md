**English** · [Español](es/ingesta-y-procedencia.md)

# Ingestion and provenance

## Trust boundary

Every upload is untrusted. The caller's media type and filename are retained as
provenance but never choose a parser or a storage path. The intake sequence is:

1. cap bytes read from the request;
2. identify the signature;
3. apply format-specific structural and expansion limits;
4. open the content with its real parser;
5. hash accepted bytes with SHA-256;
6. store by content digest;
7. write the document and audit rows in the database transaction.

PDFs are opened by PyMuPDF, images are verified by Pillow with a pixel ceiling,
and `.xlsx` files are inspected as bounded ZIP containers before openpyxl reads
them. Macro-enabled workbooks are rejected. Formulas are recorded as warnings
and their cached values are not evidence. A rejected submission remains visible
as metadata, but its bytes are not stored.

## Evidence paths

Native PDF text and raster OCR are separate paths. A usable text layer produces
page-and-character-span locators. A scan is rasterised at the configured DPI and
sent to Tesseract with a hard timeout; accepted words retain page, bounding box,
and engine confidence. Low-confidence words route the value to review.

Workbook values retain the sheet and A1 cell. Registry values retain endpoint,
record id, JSON path, and contract version. Local HTML values retain the URL,
CSS selector, capture time, and source snippet. Aggregates retain the input
extraction ids and rule name. The external response or page is also stored as a
content-addressed document.

The page reader is demonstrated only against the synthetic local fixture. Using
it elsewhere requires an identified owner, an explicit allowlist, review of
robots.txt and site terms, a lawful purpose, data minimisation, a documented
request rate, and a change contract. Redirects are disabled, requests are paced,
and missing required selectors produce a visible error rather than silent data.

## Isolation and replay

Document uniqueness is `(dossier_id, content_sha256)`: duplicate bytes within a
dossier are one document, while two dossiers remain isolated. Display names may
collide without becoming paths. Extractions upsert by a stable deduplication key,
chunks by document and ordinal, and findings by rule fingerprint. Human-confirmed
or corrected values are fenced from automated overwrite.

The synthetic corpus writes ground truth beside generated inputs, outside the
repository tree. Its PDFs and workbooks are normalised so two independent builds
are byte-identical; the unit suite asserts this rather than trusting the claim.
