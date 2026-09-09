**English** · [Español](es/recorrido.md)

# Guided walkthrough

This document assumes no prior knowledge of the project. It explains what the
system does, which pieces run, what every endpoint is for, how to launch the
demonstration, how to look at the n8n workflow, and where retrieval and a
language model do - and do not - fit.

---

## 1. The problem, in one sentence

A consultancy justifying innovation grants receives, per dossier, a technical
report as a PDF, a handful of scanned invoices, a timesheet workbook, a
personnel record in a corporate system and a published call for proposals. Somebody
has to decide **whether the declared spend is supported**, and if it is
questioned a year later, **show where every figure came from**.

This project automates that review step. It does not decide: it prepares the
decision, contradicts what does not add up, and leaves approval to a person.

## 2. What is running

Five containers. Four by default, one optional.

| Service | What it is | Why it exists |
| --- | --- | --- |
| `postgres` | PostgreSQL 17.11 | State, job queue, audit trail and text search |
| `api` | FastAPI | Accepts documents and answers questions. This is what you open in a browser |
| `worker` | Python loop | Does the slow work: opening PDFs, calling Tesseract, reading workbooks, querying the registry |
| `devsources` | FastAPI (development only) | **Pretends** to be the corporate systems: a personnel API and a public page. It fails and rate-limits deliberately so the connector's behaviour is observable |
| `n8n` | Workflow engine | Optional. Orchestrates the process by calling the API. Only starts under `--profile n8n` |

`api` and `worker` run **the same image**. That matters: it guarantees the
extractor version that measured the accuracy is the version that processes.

**`devsources` is a simulator, not part of the product.** It is a separate
application (`src/devsources/`) precisely so that no test endpoint can exist
inside the real API.

## 3. A dossier's journey

```
  1. create dossier            POST /dossiers
            ↓
  2. upload documents          POST /dossiers/{id}/documents   (one per file)
            ↓
  3. enqueue processing        POST /dossiers/{id}/process
            ↓
  4. the worker claims it      (PostgreSQL queue, with a lease)
            ↓
  5. capture external sources  personnel registry + call page
            ↓
  6. read each document        native PDF → text; scan → OCR; workbook → cells
            ↓
  7. classify                  report / invoice / timesheet
            ↓
  8. aggregate                 sum invoices, sum hours
            ↓
  9. validate                  11 rule functions (26 possible rule IDs)
            ↓
 10. NEEDS_REVIEW              always. It never approves by itself
            ↓
 11. a person reviews          corrects, confirms, accepts, dismisses
            ↓
 12. a person decides          approves or rejects, with a reason, in the audit trail
```

**Step 10 is the central design decision.** A successful run always ends in
`NEEDS_REVIEW`, whether or not it found anything. The state machine does not
even admit the `PROCESSING → APPROVED` transition, and a test pins that.

## 4. What every endpoint is for

Open `http://127.0.0.1:8000/docs`. Every group and every endpoint there now
carries its own explanation - what it is for, what the response means, and the
rule behind it - so the page reads on its own. The summary below is the same
thing in one screen:

### `system` - is it alive?

| Endpoint | What it is for |
| --- | --- |
| `GET /healthz` | Is the process responding? It touches nothing else. This is what Docker checks |
| `GET /readyz` | Can it *work*? Checks the database and the object store |
| `GET /metrics` | Prometheus-format counters: jobs, findings, errors |

They are separate on purpose: conflate "alive" with "ready" and an orchestrator
restarts a healthy process because the database blinked for a second.

### `dossiers` - the dossier and its documents

| Endpoint | What it is for |
| --- | --- |
| `POST /dossiers` | Creates the dossier: reference, period, claimed amount |
| `GET /dossiers` | Lists them. Accepts `?reference=INN-2025-042` to look one up by business reference |
| `GET /dossiers/{id}` | One dossier and its status |
| `POST /dossiers/{id}/documents` | Uploads **one** file. Checks size, signature and parser before storing anything |
| `GET /dossiers/{id}/documents` | What was delivered, including what was rejected and why |
| `POST /dossiers/{id}/process` | Enqueues processing. Returns the job |

### `jobs` - is it done?

| Endpoint | What it is for |
| --- | --- |
| `GET /jobs` | Jobs, filterable by status |
| `GET /jobs/{id}` | One of them: status, attempts, last error |

A failed job is **visible**, with its error and its attempt count. `FAILED` and
`DEAD_LETTER` are inspectable states, not a silent drop.

### `review` - what a person does

| Endpoint | What it is for |
| --- | --- |
| `GET /dossiers/{id}/extractions` | Every extracted field, with its locator and its confidence |
| `GET /dossiers/{id}/findings` | The findings the rules raised |
| `GET /dossiers/{id}/decisions` | What each person decided and why |
| `POST /extractions/{id}/correct` | "This was misread, the correct value is X" |
| `POST /extractions/{id}/confirm` | "I checked it against the document, it is right" |
| `POST /findings/{id}/resolve` | Accept or dismiss a finding, with a reason |
| `POST /dossiers/{id}/approve` | Approves. **Refuses** while a blocking finding is open |
| `POST /dossiers/{id}/reject` | Rejects, with a reason |

A correction **never erases** what the machine read: it stores the original
value alongside, with who changed it, when and why.

### `artifacts` - what you take away

| Endpoint | What it is for |
| --- | --- |
| `POST /dossiers/{id}/reports` | Generates the HTML report |
| `GET /dossiers/{id}/reports/latest.html` | The latest report, to read |
| `GET /dossiers/{id}/export.json` | Everything as JSON, for another system |
| `GET /dossiers/{id}/export.csv` | Everything as CSV, with formula-injection guarding |
| `GET /dossiers/{id}/audit` | The append-only trail: everything that happened |
| `GET /dossiers/{id}/evidence?q=...` | Searches for a phrase inside the dossier's documents |

### `ui` - the review screen

| Endpoint | What it is for |
| --- | --- |
| `GET /ui/dossiers` | The list. **This is the page to start from** |
| `GET /ui/dossiers/{id}` | One dossier's review: findings, fields, buttons |

## 5. Running the demonstration

From the repository, in PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/demo.ps1 -Fresh
```

`-Fresh` deletes the two demonstration dossiers first. Without it, the second
run reports that they are already in review and does not reload them - a dossier
in review refuses new documents by design.

The script, in order:

1. validates the Compose model;
2. builds the image (skippable with `-SkipBuild`);
3. starts `postgres`, `devsources`, `api` and `worker` and waits for health;
4. generates the synthetic corpus inside the container;
5. creates both dossiers and uploads their documents;
6. processes both;
7. generates both reports;
8. prints the inventory and the links.

### What you should see

```
INN-2025-041: 4 accepted, 0 duplicate, 0 rejected
  rejected justificante-danado.pdf: PDF has no pages
  rejected notas-internas.txt: unrecognised file signature
INN-2025-042: 8 accepted, 1 duplicate, 2 rejected
```

- `INN-2025-041` is the **clean path**: 0 findings.
- `INN-2025-042` carries deliberately seeded defects: 17 findings, 11 blocking.
- The two rejections and the duplicate **are part of the script**: a truncated
  PDF, a `.txt` that is not an accepted format, and a byte-for-byte copy of the
  report.

### Where to look next

1. `http://127.0.0.1:8000/ui/dossiers` - the list. Click *review* on `INN-2025-042`.
2. On that screen the findings are at the top and every field below, with its
   **evidence**: "page 1, characters 120-141", or "sheet 'Partes horarios',
   cell E7", or "page 1, box (243,801) 512x28, OCR confidence 64 %".
3. Type your name in the box, type a reason on a finding and press *Dismiss*.
   Reload: you will see who and why.
4. Press *Generate report*: the HTML report opens.
5. Try *Approve* with blocking findings open: **it will refuse you.** That is the
   correct behaviour and the thing worth showing.

## 6. Opening n8n and seeing the workflow

n8n **does not start by default**. It comes up under its profile:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/demo.ps1 -SkipBuild -WithN8n
```

Or by hand, in PowerShell:

```powershell
docker compose --profile n8n run --rm --no-deps n8n import:workflow --input=/workflows/dossier-review.json
docker compose --profile n8n run --rm --no-deps n8n update:workflow --id=iep-dossier-review --active=true
docker compose --profile n8n up -d --wait n8n
```

Use PowerShell rather than Git Bash for these three: Git Bash rewrites the
container path `/workflows/...` into a Windows path and the import fails with
`ENOENT`.

The import happens **before** the server starts on purpose: n8n keeps its state
in SQLite, and two processes writing at once produce `SQLITE_BUSY`.

Then open `http://127.0.0.1:5678`. The first time it asks you to create a local
account - it belongs to your instance and goes nowhere. Inside you will find the
*Dossier review orchestration* workflow. Click any node to see what it does.

To trigger it:

```bash
curl -X POST http://127.0.0.1:5678/webhook/dossier-review -H "content-type: application/json" -d "{\"reference\":\"INN-2025-042\"}"
```

It replies with the simulated notification: subject, number of open findings,
blockers, and a link to the review screen.

**What it demonstrates and what it does not.** It demonstrates that the pipeline
can be orchestrated from outside over HTTP, with an idempotency key and a
correlation id. It contains no business logic: what counts as a finding, and
whether a dossier may be approved, are decided in Python where they are
versioned and tested.

## 7. Tesseract locally (optional)

Without Tesseract installed, 13 tests are **skipped** on your machine. CI
installs Tesseract and runs them. The production image also ships Tesseract so
the application can perform real OCR, but deliberately does not ship pytest or
the other development tools. To run the OCR tests on the host:

```powershell
winget install --id UB-Mannheim.TesseractOCR
```

Then add the install folder to `PATH` (usually `C:\Program Files\Tesseract-OCR`)
and **open a new terminal**. Check:

```powershell
tesseract --version
tesseract --list-langs
```

You need the `spa` language. The UB-Mannheim installer offers it under
*Additional language data*; if you did not tick it, reinstall and tick it. Those
13 tests then stop skipping.

**It is not required for the demonstration**: the application runs inside the
container, which already has the OCR engine and Spanish language data.

## 8. Where retrieval fits (and why this is not called RAG)

**RAG** = *Retrieval-Augmented Generation*: you retrieve relevant fragments and
hand them to a model **so that it generates** an answer grounded in them.

This project does the first half and **not** the second:

- It splits each document into segments, **each with its locator**.
- It indexes them with PostgreSQL full-text search (`tsvector`, Spanish).
- `GET /dossiers/{id}/evidence?q=periodo de ejecucion` returns the top-k
  segments, reproducibly.

Nothing generates text from that. So the document calls it *evidence retrieval*
rather than RAG: calling it RAG would claim something the code does not do, and
anyone reading the code would catch it in one question.

**When it would genuinely be RAG here.** If we added "draft the justification
report citing the evidence": retrieve the segments, pass them to the model, and
the model writes prose **citing** locators. That is retrieval-augmented
generation, and it is a natural extension.

**When embeddings would be justified.** Lexical search fails when the user asks
with different words from the document ("plazo de ejecución" against "periodo de
ejecución"). With a controlled corpus and stable administrative vocabulary,
lexical is enough and is cheaper, faster and explainable. As soon as there are
natural-language questions across thousands of dossiers with heterogeneous
wording, embeddings earn their place - and `pgvector` would put them in the same
database, with no new infrastructure.

The reasoning is in [ADR 0004](adr/0004-lexical-retrieval-not-rag.md).

## 9. Seeing the language model run

There are **two semantic providers** behind one protocol:

| Provider | What it does | Cost |
| --- | --- | --- |
| `deterministic` (default) | Classifies and locates fields with rules and regular expressions | €0 |
| `llm` | A real adapter for the Anthropic API with structured outputs | Cents |

The `llm` adapter has **never made a billable call**: it is tested against
injected doubles and executed against a local simulator.
[`docs/llm-demo.md`](llm-demo.md) has the exact procedure, the cost, and what to
watch.

The point that matters: **the model only classifies.** Its field proposals are
not persisted as extractions, and no rule ever compares against something a
model produced. That is why a document that tries to instruct the system cannot
move a figure - a test pins it.

## 10. If something goes wrong

| Symptom | What to look at |
| --- | --- |
| The list is empty | Did you run `seed`? `docker compose exec -T api iep status` |
| `iep seed` says "already NEEDS_REVIEW" | Correct. Use `-Fresh`, or `iep reset --reference <ref>` |
| The n8n webhook returns 404 | n8n has not registered the webhook yet; wait a few seconds and retry |
| A container will not start | `docker compose logs --tail=100 api worker` |
| I want to start completely over | `docker compose --profile n8n down --volumes`, then run the demo again |

Every API error response carries a `correlation_id`. That identifier appears in
the `api` and `worker` logs, so one request can be followed end to end.
