# Five-minute local demonstration

## Prepare

Run `scripts/demo.ps1` from PowerShell or follow the README quickstart. Keep the
architecture document, API docs, review queue, and one generated report open.
The corpus is synthetic and the demonstration needs no Internet after the images
are available locally.

## Route

1. **Problem (30 seconds).** Show the dossier as PDF, scanned receipt, workbook,
   registry response, and local call page. Explain that the hard requirement is
   traceable reconciliation, not document summarisation.
2. **Architecture (45 seconds).** Point to one image, separate API/worker
   processes, PostgreSQL queue, content-addressed objects, and optional workflow.
3. **Happy evidence path (60 seconds).** Open the consistent dossier. Show a PDF
   character span, an OCR word box with confidence, an Excel cell, an API JSON
   path, an HTML selector, and a derived total.
4. **Review path (90 seconds).** Open the defective dossier, correct one field
   with a reason, resolve a finding, and show that unresolved blockers or pending
   fields prevent approval.
5. **Reliability and safety (60 seconds).** Show idempotent replay, job lease and
   recovery tests, rejected malicious/invalid inputs, and structured errors.
6. **Report and limits (45 seconds).** Open the report and JSON/CSV export. Finish
   with the production-gap document and measured-results command.

## Ninety-second route

State the reconciliation problem; show one OCR-located value, one workbook cell,
one deterministic mismatch, one human correction preserving the original, and
the resulting report. Close with: the model may help interpret text, but rules
and people retain financial validation and approval.

## No-execution fallback

Use the architecture diagram, a small reviewed `evaluation-results` summary from
the matching commit, the OpenAPI document, and a generated report. Do not quote
metrics from another revision. Explain the failed local prerequisite plainly and
offer the exact reproduction command.
