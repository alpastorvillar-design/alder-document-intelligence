**English** · [Español](es/seguridad-ia.md)

# AI safety

## Default path

The default semantic provider is deterministic and makes no network call. It is
a parser-shaped test and demo provider, not evidence that a hosted language model
ran. The complete core, including OCR, validation, review, reporting, and replay,
works without a hosted model, a workflow engine, or Internet access.

## Optional hosted adapter

The optional adapter is isolated behind `SemanticExtractor`. It is disabled
unless explicitly selected and refuses to start without a key. The model id,
timeout, attempt ceiling, token ceiling, prompt version, and pricing parameters
are configuration. Tests use an in-process protocol double; the repository has
no credential and the test suite makes no paid request.

The adapter requests a typed schema and validates the response again locally.
Invalid JSON, wrong types, unknown fields, and ungrounded values fail closed or
are discarded with a warning. A proposed value must be requested, must occur in
the quoted evidence, and the quote must occur in the source document.

Document text is delimited and labelled as untrusted data. It cannot select a
tool, execute an action, change validation rules, or approve a dossier. Amounts,
dates, duplicate detection, cross-source checks, state transitions, and approval
remain deterministic.

## Cost and claims

Usage and cost are estimates derived from recorded token counts and configured
published unit prices. They are labelled estimates, not incurred cost. A real
deployment would add an approved provider, data-processing terms, regional and
retention controls, redaction, per-tenant budgets, production telemetry, and a
representative evaluation before enabling this adapter.

## Retrieval and grounded generation

This pipeline remains extraction-first. Evidence chunks support lexical search,
exact pgvector cosine search and reciprocal-rank hybrid search. The offline
feature-hashing provider is not described as a semantic model. A hosted
embedding model requires a key plus `IEP_ALLOW_EXTERNAL_AI=true`; reprocessing
uses a configuration hash to prevent old and new vector spaces being mixed.

`POST /dossiers/{id}/questions` is the only RAG boundary. It is disabled by
default, read-only, top-k limited, context-capped and tool-free. Retrieved
document text is JSON-encoded as untrusted data. The versioned system prompt
requires abstention when evidence is insufficient, and the application rejects
unknown citation ids after validating the strict response schema. This reduces
prompt-injection and hallucination risk; it does not prove that a model answer
is true. Answer quality still needs a representative evaluation and human use
of citations.
