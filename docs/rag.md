**English** · [Español](es/rag.md)

# Hybrid retrieval and optional RAG

## What exists

Each `document_chunks` row stores text, its evidence locator, PostgreSQL's
Spanish `tsvector`, and a nullable `vector(512)`. Three retrieval modes share the
same endpoint:

| Mode | Signal | Best fit |
| --- | --- | --- |
| `lexical` | terms and stems in PostgreSQL FTS | exact references and known vocabulary |
| `vector` | exact cosine similarity in pgvector | wording similarity when a learned provider is enabled |
| `hybrid` | reciprocal-rank fusion of both result lists | exact identifiers plus natural-language questions |

Every SQL query filters by `dossier_id`. Vector queries also require the same
embedding configuration hash used for the question. A vector from another model
or dimension is therefore not silently mixed into a result.

The default `hashing` provider is a deterministic character/word feature
projection. It makes migrations, storage, idempotent re-indexing and vector SQL
fully runnable offline. It is useful for engineering tests and some typo
tolerance; it is **not a learned semantic embedding model**. The API returns
`learned_model: false` so that distinction is observable.

## Run the offline retrieval path

Process the corpus normally. New chunks receive the hashing baseline. Existing
chunks can be refreshed without re-running OCR or validation:

```bash
docker compose exec -T api iep reindex --reference INN-2025-042
```

Open `/docs`, select `GET /dossiers/{dossier_id}/evidence`, and compare the same
query with `mode=lexical`, `mode=vector`, and `mode=hybrid`. The response always
returns document id, text and the original locator.

The database representation is inspectable:

```sql
SELECT document_id,
       ordinal,
       embedding_provider,
       embedding_model,
       vector_dims(embedding),
       embedded_at
FROM document_chunks
ORDER BY document_id, ordinal
LIMIT 10;
```

No approximate index is present. Exact search is simpler and adequate for this
small corpus. HNSW or IVFFlat should be introduced only after a representative
volume and filtered-query benchmark establishes a latency need.

## What makes the questions endpoint RAG

`POST /dossiers/{dossier_id}/questions` performs all three RAG stages:

1. retrieve top-k evidence chunks using lexical, vector or hybrid search;
2. augment a versioned system instruction with those chunks as untrusted JSON;
3. ask a hosted generator for a structured answer and verified citation ids.

It is not an approval path. It has no tools, makes no database change, caps both
top-k and context, requests abstention when evidence is insufficient, validates
the output schema, and rejects a citation that was not retrieved. The answer is
still a model-generated draft; citation validation does not prove every claim is
entailed by the cited text.

## Optional hosted test

The default is `IEP_RAG_PROVIDER=disabled`; no test or normal demo calls a paid
endpoint. A deliberate hosted run needs all of the following:

```text
IEP_EMBEDDING_PROVIDER=openai
IEP_RAG_PROVIDER=openai
IEP_ALLOW_EXTERNAL_AI=true
IEP_OPENAI_API_KEY=<supplied outside Git>
IEP_OPENAI_EMBEDDING_MODEL=text-embedding-3-small
IEP_OPENAI_RAG_MODEL=gpt-4o-mini
```

Set a provider-side spend cap first. Use only synthetic data. Never paste the key
into source, documentation, a command committed to shell history, or an issue.
An untracked `.env` is acceptable for this local demonstration but environment
variables are visible through local container inspection; production should use
a real secret manager or mounted secret.

After changing the embedding provider, run `iep reindex` for each dossier before
querying. Re-indexing is idempotent: rows with the current configuration hash are
not billed or written again. Changing model or dimensions is a data migration
and requires a fresh representative retrieval evaluation.

## What is and is not demonstrated

Demonstrated locally: the pgvector extension and migration, 512-dimensional
storage, exact cosine SQL, dossier isolation, deterministic hybrid fusion,
configuration-aware re-indexing, hosted API contracts against doubles, strict
answers, citation allowlisting, prompt version/hash, and disabled-by-default
egress.

Not demonstrated by those tests: semantic quality of a hosted embedding model,
answer correctness on real dossiers, general prompt-injection resistance,
production privacy terms, throughput, an approximate-index benefit, or a real
business outcome.
