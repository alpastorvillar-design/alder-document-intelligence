**English** · [Español](es/demostracion-llm.md)

# Seeing the hosted-model path run

There are two ways to exercise the hosted-provider path. The first costs
nothing and needs no key. The second costs cents and uses a real model.

## What "the LLM path" actually is

The pipeline never talks to a model directly. It asks a `SemanticExtractor` for
a **classification** and for **grounded field proposals**. Two implementations
satisfy the same protocol:

| Provider | What it is | Selected with |
| --- | --- | --- |
| `deterministic` | Rules and regular expressions. The default | `--provider deterministic` |
| `llm` | A real adapter for the Anthropic API with structured outputs | `--provider llm` |

Three constraints hold whichever answers:

1. the response must validate against the declared schema or it is discarded;
2. every proposed value must quote text that appears in the source document, or
   it is discarded;
3. **nothing a provider returns is used for arithmetic.** Its field proposals
   are not even persisted as extractions - only the classification is used.
   Every amount a rule compares comes from a deterministic reader with a
   locator.

The third is what makes a document that tries to instruct the system inert, and
an integration test pins it.

## Option A - free, no key, against the local simulator

The `devsources` service answers `/v1/messages` with enough of the protocol for
the **official SDK** to talk to it. It is not a model: it replies by reading the
document with the deterministic provider. What it does exercise is everything
else - request shaping with the structured-output schema, response validation,
the grounding check, bounded retries, and token and cost accounting.

It also fails deliberately on every third call with a reply the schema rejects,
so the retry is visible rather than only asserted.

```bash
export IEP_LLM_API_KEY=local-simulator   # the simulator does not check it
docker compose up -d --force-recreate api worker devsources

docker compose exec -T api iep reset --reference INN-2025-042
docker compose exec -T api python -m corpus.generate --out /tmp/corpus
docker compose exec -T api iep seed --corpus /tmp/corpus \
  --call-page-url http://devsources:8080/public/convocatoria.html
docker compose exec -T api iep process --reference INN-2025-042 --provider llm
```

`semantic_provider: "anthropic"` in the output confirms the adapter ran. The
`semantic_config_hash` identifies the exact combination of model, prompts and
schema that produced the result: change a prompt and the hash changes, and an
older extraction still says which configuration it was written under.

The command's own log shows the retry:

```
llm_schema_violation   attempt=1  errors=3
llm_call_completed     attempts=2 input_tokens=93 output_tokens=127 0.014s
```

The model returned something that did not fit the schema, the adapter refused
it, fed the validation error back and tried again. When the attempts run out
nothing is invented: the document goes to review unclassified.

### What this demonstrates, and what it does not

| Demonstrates | Does not demonstrate |
| --- | --- |
| The official SDK used correctly | A real model's judgement on prose |
| Structured outputs and strict validation | Real network latency |
| A malformed reply refused and retried | Real billed cost |
| The grounding check | A specific model's error rates |
| Token accounting and cost estimation | |

## Option B - a real model

### Cost

Measured over the whole corpus (12 documents, ~27,600 estimated input tokens,
~800 output tokens per document):

| Model | Input/output per million | Estimated cost for the whole corpus |
| --- | --- | --- |
| `claude-haiku-4-5` | $1 / $5 | **~€0.07** |
| `claude-sonnet-5` | $2 / $10 | ~€0.14 |
| `claude-opus-5` | $5 / $25 | ~€0.35 |

Cents - but real money, and the estimate is parameterised rather than measured.

### Procedure

```bash
export IEP_LLM_API_KEY=sk-ant-...
export IEP_LLM_BASE_URL=https://api.anthropic.com
export IEP_LLM_MODEL=claude-haiku-4-5

docker compose up -d --force-recreate api worker
docker compose exec -T api iep reset --reference INN-2025-042
docker compose exec -T api iep seed --corpus /tmp/corpus \
  --call-page-url http://devsources:8080/public/convocatoria.html
docker compose exec -T api iep process --reference INN-2025-042 --provider llm
```

Never put the key in a file in this repository. `.env` is ignored by git, but a
session environment variable leaves nothing behind.

### What changes, and what does not

Token counts become real rather than estimated, and classification may differ
from the deterministic provider - compare `document_kind` across runs.

The 17 findings on `INN-2025-042` do not depend on the provider. Rules compare
amounts read by deterministic extractors. If the finding count moves when the
provider changes, it is because a document was classified differently and so a
different reader ran, not because a model decided a figure.

## Honest status

- Written against current official documentation, using `messages.parse` with a
  Pydantic model as the output schema.
- **Tested against injected doubles** and **executed against the local
  simulator**. No run against the real API is recorded in this repository.
- The provider refuses to construct without `IEP_LLM_API_KEY`, so it cannot be
  selected by accident.
- Prompts are versioned files inside the package
  (`src/iep/semantic/prompts/`), carry their own version string, and are part of
  the configuration hash.
