**English** · [Español](../es/adr/0001-localizadores-de-evidencia.md)

# ADR 0001: Evidence locators are part of every value

**Status:** accepted

## Context

A number without its source cannot be reviewed, corrected, or defended. Storing
only raw documents and final fields makes every dispute a manual search.

## Decision

Every extraction carries a typed locator appropriate to its source: page span,
OCR word box, workbook cell, API field, HTML selector, or derived input ids. The
document hash, extraction and contract versions, confidence, and correction
history travel with it.

## Consequences

Extractors have a stricter contract and reports are more verbose. In return,
review, testing, replay comparison, and audit can point to the evidence rather
than merely assert a value.
