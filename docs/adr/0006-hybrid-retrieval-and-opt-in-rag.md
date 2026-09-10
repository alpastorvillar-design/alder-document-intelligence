**English** · [Español](../es/adr/0006-recuperacion-hibrida-y-rag-opcional.md)

# ADR 0006: Add hybrid retrieval and opt-in grounded generation

**Status:** accepted

## Context

Spanish full-text search remains a strong baseline for references, invoice
numbers and known domain terms. It misses misspellings and semantically related
wording. A reviewer may also need a short draft answer that points back to the
source rather than another untraceable summary.

## Decision

Keep full-text search and add a nullable 512-dimensional vector to the existing
evidence chunks. PostgreSQL uses pgvector 0.8.6. Exact cosine search is used at
this corpus size; an approximate HNSW or IVFFlat index would add tuning and
filtered-search trade-offs without a measured latency need.

Hybrid search fuses lexical and vector positions with reciprocal-rank fusion.
Every query filters by dossier id and by embedding configuration hash before
ranking, so neither another dossier nor vectors from an old model can enter the
result.

The offline default is a deterministic feature-hashing vectorizer. It exercises
storage, migrations, re-indexing, distance queries and hybrid ranking, but it is
not a learned embedding model and is labelled accordingly in the API. The
optional hosted provider uses the documented embeddings contract and is disabled
until both a key and an explicit data-egress opt-in are supplied. A model or
dimension change gives a new configuration hash and reprocessing replaces only
stale vectors.

Grounded generation is a separate, read-only endpoint. It retrieves top-k
chunks, serialises them as untrusted data, sends no tools, requests a strict
answer schema, and accepts only citation ids that were in the retrieved set. It
cannot edit, approve or transition a dossier. It is disabled by default.

## Consequences

The extraction, validation and review core still works without Internet or a
language model. PostgreSQL now requires the pgvector extension. Enabling a
hosted provider introduces data-transfer, retention, residency, cost, latency
and model-evaluation obligations. Adapter tests do not establish answer quality;
a representative retrieval and grounded-answer evaluation is still required
before production use.
