# ADR 0003: Keep decisions in deterministic rules

**Status:** accepted

## Context

Document prose benefits from semantic interpretation, but financial arithmetic,
eligibility, and approval require repeatability and explainable failure modes.

## Decision

Use a semantic provider only for classification and grounded proposals. Persisted
business values come from deterministic readers. Versioned rules perform all
arithmetic and cross-source validation. Only a human may approve or reject.

## Consequences

More explicit parsing and rule code is required. The outcome is reproducible,
testable, and safe when the semantic provider is absent, slow, or wrong.
