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

### A learned embedding model, locally

The shipped baseline is a deterministic projection of character trigrams. It
proves the pgvector path and nothing else, and the measurement below says why.
`IEP_EMBEDDING_PROVIDER=ollama` replaces it with a real multilingual model on
the same machine - no key, no egress, no bill:

```text
IEP_EMBEDDING_PROVIDER=ollama
IEP_OLLAMA_EMBEDDING_MODEL=bge-m3
```

Then `iep reindex --reference INN-2025-042` for each dossier. Re-indexing is
keyed by a configuration hash covering provider, model and width, so old rows
are neither reused nor compared against the new ones - they simply stop being
selected.

The width comes from the model, not from configuration: `bge-m3` returns 1024
dimensions and `qwen3-embedding` 2560. The column used to be `vector(512)`,
which was the baseline's width standing in for the schema and made every
learned model unusable; it now carries no dimension modifier, so rows of
different widths coexist and `vector_dims()` reports each. What that gives up
is indexability - HNSW and IVFFlat need a fixed width - and nothing is lost
today, because this project does exact search on a sequential scan by
deliberate decision.

### The relevance floor: three measurements, two of them wrong

Vector search returns `limit` rows for any query at all, so a question about
nothing in the dossier came back looking exactly like a question about
something in it. `IEP_RETRIEVAL_MIN_SIMILARITY` is the filter for that,
applied in SQL so a filtered query does not spend its limit on rows it will
discard.

Choosing its default took three attempts, and the sequence is the finding.
Top-hit cosine similarity on `INN-2025-042`:

| Attempt | Model | Queries | Worst relevant | Best irrelevant | Margin |
| --- | --- | ---: | ---: | ---: | ---: |
| 1 | `bge-m3` | 8 | 0.5365 | 0.4376 | **+0.099** |
| 2 | `qwen3-embedding:4b` | 13 | 0.5649 | 0.5671 | −0.002 |
| 3 | `qwen3-embedding:4b` | 17 | 0.4968 | 0.5671 | −0.070 |

The first looked like a clean gap and a floor of 0.45 was set from it. The
second, after adding a relevant question that needs counting people out of
prose and two short generic Spanish phrases, closed the gap entirely - so the
criterion was weakened to "never drop a relevant hit" and the floor moved to
0.50. The third widening broke that too: `¿Cuántas personas tienen dedicación
al proyecto?` scores **0.4968**, below two irrelevant queries, so 0.50 was
cutting a legitimate question *and* passing noise.

Every widening lowered the worst relevant score. A threshold fitted to any one
sample fails on a question asked slightly differently, and here is what that
looks like in practice - the same question, floor on and floor off:

```text
suelo 0.50   -> 0 fragmentos, "la búsqueda no ha encontrado nada"
suelo 0      -> 1 cita, "No se puede determinar el número de personas
                 con dedicación al proyecto a partir de la evidencia"
```

The second is the better answer, and it is the reason the floor ships **off**.
The asymmetry is not close: noise reaching the generator is recoverable,
because it says the evidence does not support the question. Evidence removed
before the generator sees it is not recoverable by anything, because nothing
can report an absence it was never shown.

The setting stays, for a deployment that has measured its own corpus and its
own questions. What was delivered here is the filter and the measurements, not
a number.

### The embedding model

Two are available locally, and one measures better:

| Model | Params | Dims | Worst relevant (13 queries) |
| --- | ---: | ---: | ---: |
| `bge-m3` | 567M | 1024 | 0.4480 |
| `qwen3-embedding:4b` | 4B | 2560 | **0.5649** |

`qwen3-embedding:4b` scores every relevant query higher and orders them
better, for 2.5 GB and about half a second more per batch, so it is the
default. `bge-m3` remains a good smaller choice. Both are learned multilingual
models and both are enormously better than the hashing baseline, whose worst
relevant query scores 0.1626 - below half the irrelevant ones.

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

## The answer box on the screen

Both the review screen and the evidence screen carry the same panel, because
they include the same partial and send the same request to the same read-only
endpoint. It renders whether or not generation is switched on, and when it is
off it says which switch is missing. That is deliberate: a hidden feature
teaches nothing, and "off, and here is the switch" is what somebody seeing the
boundary for the first time actually needs.

![The answer box on the evidence screen](img/06-ask.png)

What the panel shows, once an answer comes back:

- whether the model considered the retrieved evidence **sufficient**, in those
  words, because an answer drawn from thin evidence is a lead rather than a
  fact;
- every citation as its own block, with the document, the place inside it, and
  the quoted fragment. The model never supplies a link - it names an evidence
  id, and the id is resolved against what was actually sent to it;
- how many retrieved segments were **withheld** because their document is
  flagged as carrying instructions aimed at an automated reader;
- the provider, the model, the retrieval mode, the citation count and the
  prompt hash, so the answer on screen can be matched to the audit row.

Asking a question is recorded. `EVIDENCE_QUESTION_ANSWERED` carries the
question, the provider and model, how many segments were retrieved and
withheld, the citations, whether the model claimed sufficiency, and the prompt
version and hash. A read-only call is the easiest one to leave untraced, and
then the one place a model touched the dossier is the only place with no
record.

## Answering through an assistant CLI

`IEP_RAG_PROVIDER=cli` answers through `claude` or `codex` running on the same
host, instead of a metered endpoint. It exists so the integration point can be
demonstrated without an API key, it is **development only**, and it is off by
default.

```text
IEP_RAG_PROVIDER=cli
IEP_RAG_CLI_TOOL=claude          # or codex
IEP_RAG_CLI_MODEL=              # empty means the CLI's own default
IEP_RAG_CLI_TIMEOUT_SECONDS=120
```

The API runs in a container and the CLI is installed on the host, so **the API
has to run on the host for this to work**. With the stack already up and
seeded:

```bash
IEP_DATABASE_URL=postgresql+psycopg://iep:iep@127.0.0.1:55432/iep IEP_STORAGE_ROOT=var/objects IEP_REPORT_ROOT=var/reports IEP_REGISTRY_API_BASE_URL=http://127.0.0.1:8080 IEP_RAG_PROVIDER=cli IEP_RAG_CLI_TOOL=claude python -m uvicorn iep.api.app:create_app --factory --host 127.0.0.1 --port 8010
```

The object store lives in a Docker volume, so copy it out once if the evidence
viewer should work in this mode too:

```bash
docker compose cp api:/var/lib/iep/objects var/
docker compose cp api:/var/lib/iep/reports var/
```

`GET /dossiers/{id}/questions` reports whether the configured CLI is on this
process's `PATH`, and the panel repeats it, so a misconfiguration reads as a
sentence rather than as a failure to interpret.

### What a subprocess costs, and what is done about it

A subprocess is a wider door than an HTTP call. What the provider keeps:

- `argv` is a list and the shell is never used, so no document text can become
  a command;
- the prompt travels on **stdin**, so nothing from a document lands in `argv`
  either;
- each call runs in an empty temporary directory, deleted afterwards - an
  assistant started inside a repository takes that repository as context;
- the CLI's own tools are disabled by flag, and its sandbox set to read-only
  where it has one;
- the call is bounded by a wall clock and the process is killed when it
  expires.

What it does not have is what a hosted endpoint gives for free: a
schema-constrained reply. Asked for "the required structured output" the CLI
answered correctly - in Markdown, with the fields written out in prose -
because nothing had told it the shape. So the shape is spelled out in the
instructions the CLI receives, and only there: the *rules* stay in the single
prompt both providers share, where they cannot drift apart. Prose instead of
JSON is then a failure rather than a guess, because accepting it would mean
inventing the citations it never gave.

### The call budget

A ceiling this application enforces on itself, counted from the audit trail
over a rolling window:

```text
IEP_RAG_CALL_BUDGET=40           # 0 means no ceiling
IEP_RAG_BUDGET_WINDOW_DAYS=7
IEP_RAG_BUDGET_STOP_FRACTION=0.90
```

At 90 % of the ceiling the next call is **refused**, not warned about, and the
check runs before retrieval and before generation - refusing afterwards would
spend the call it was meant to prevent.

It is not the remaining quota of a subscription. Neither CLI publishes that,
and a counter labelled as if it were would be worse than none, so the panel
says what it is measuring: what this application has spent.

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
