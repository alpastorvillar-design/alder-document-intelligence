**English** · [Español](../es/adr/0004-recuperacion-lexica-no-rag.md)

# ADR 0004: Start with lexical evidence retrieval

**Status:** accepted

## Context

Reviewers need to locate known concepts in a dossier and receive an exact source
location. The corpus is small and domain vocabulary is stable.

## Decision

Use PostgreSQL Spanish full-text search over chunks that retain evidence
locators. Do not generate an answer from retrieved text and do not describe this
as retrieval-augmented generation.

## Consequences

The system is cheap, local, deterministic, and easy to inspect. It will miss
semantic paraphrases. Add embeddings only after a representative query set shows
material lexical failures and after privacy, lifecycle, and evaluation controls
are designed.
