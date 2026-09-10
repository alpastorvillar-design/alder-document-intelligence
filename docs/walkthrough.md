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

`-Fresh` deletes the demonstration dossiers first. Without it, the second run
reports that they are already in review and does not reload them - a dossier in
review refuses new documents by design.

The script, in order:

1. validates the Compose model;
2. builds the image (skippable with `-SkipBuild`);
3. starts `postgres`, `devsources`, `api` and `worker` and waits for health;
4. generates the synthetic corpus inside the container;
5. creates the dossiers and uploads their documents;
6. processes them;
7. generates their reports;
8. prints the inventory and the links.

### What you should see

```
INN-2025-041: 4 accepted, 0 duplicate, 0 rejected
  rejected justificante-danado.pdf: PDF has no pages
  rejected notas-internas.txt: unrecognised file signature
INN-2025-042: 8 accepted, 1 duplicate, 2 rejected
```

| Dossier | What it is for |
| --- | --- |
| `INN-2025-041` | The **clean path**: 0 findings. Proof the rules do not fire on a consistent claim |
| `INN-2025-042` | Every seeded defect: **17 findings, 11 blocking** |
| `INN-2025-043` | One rate that disagrees with the registry. The realistic middle case |
| `INN-2025-044` | Claims above the call's maximum, and more hours in a year than a year holds |
| `INN-2025-045` | An invoice charged to a different claim |

The two rejections and the duplicate on `INN-2025-042` **are part of the
script**: a truncated PDF, a `.txt` that is not an accepted format, and a
byte-for-byte copy of the report.

`041` and `042` are the two extremes, and neither looks like an ordinary claim.
`043`, `044` and `045` each carry one or two problems, which is what a reviewer
actually spends the day on - and between them they make three rules fire that
had only ever been exercised by unit tests.

### The five screens

Everything worth showing starts at `http://127.0.0.1:8000/ui/dossiers`, and it
is five screens in the order a dossier moves through them.

| Screen | Where | What it does |
| --- | --- | --- |
| **Queue** | `/ui/dossiers` | What is waiting, what each dossier holds, and whether anything blocks it |
| **Intake** | `/ui/dossiers/new` | The claim's own fields and a **drop zone** for its documents |
| **Progress** | `/ui/dossiers/{id}/progress` | The pipeline's stages while the worker runs |
| **Review** | `/ui/dossiers/{id}` | The findings and every field, with the actions |
| **Evidence** | `/ui/evidence/{id}` | **The document with the place marked** |

The intake screen makes the **same three calls** an integration would: create
the dossier, upload each file, enqueue the run. So a file refused here is
refused identically anywhere else, and it says so by name:
*"justificante-danado.pdf - PDF has no pages"*.

### The route worth walking

1. Open the **queue** and click *Revisar* on `INN-2025-042`.
2. The top of the screen says **the dossier cannot be approved**, with the
   count, and the approve button is disabled. That is the correct behaviour:
   the system refuses rather than failing when pressed.
3. Each finding says what is wrong in plain language, shows the figures it
   compared, and expands to the **justification requirement** it enforces.
   That is what makes a finding arguable rather than an opinion.
4. Press the evidence button on a finding: the scan opens with a **box around
   the words the engine read**, drawn from the coordinates stored during
   extraction. This is where the project explains itself.
5. Further down, try the evidence link on a workbook cell (it comes back with
   its neighbours and its header row) and on a call-page field (the matched
   fragment and the selector).
6. Type your name, give a reason on a finding and dismiss it. Reload: who and
   why are recorded.
7. Generate the report.
8. Then open `INN-2025-043`, which has exactly **one** finding. That is the one
   that looks like an ordinary claim.

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

## 8. Retrieval and the optional RAG boundary

**RAG** = *Retrieval-Augmented Generation*: you retrieve relevant fragments and
hand them to a model **so that it generates** an answer grounded in them.

The evidence index supports three retrieval modes:

- It splits each document into segments, **each with its locator**.
- PostgreSQL Spanish full-text search for exact terms;
- exact cosine search over a `vector(512)` stored by pgvector;
- reciprocal-rank fusion of the lexical and vector lists.

`GET /dossiers/{id}/evidence?q=periodo de ejecucion&mode=hybrid` returns the
top-k segments with locators. It is retrieval only.

`POST /dossiers/{id}/questions` is RAG: it retrieves evidence and passes those
bounded fragments to a hosted generator, which returns a draft plus citation
ids. It is disabled by default, read-only, tool-free and cannot approve or change
an expediente.

The default feature-hashing vectors prove the database and ranking path offline,
but are not a learned semantic model. A hosted embedding provider is separately
opted in and must be evaluated on representative paraphrases before any quality
claim. Exact search is used because this corpus does not justify an approximate
index.

The commands, controls and honest claim boundary are in
[Hybrid retrieval and optional RAG](rag.md); the change is recorded by
[ADR 0006](adr/0006-hybrid-retrieval-and-opt-in-rag.md).

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

The semantic-extraction model only classifies; its proposed fields are not
persisted and no rule compares model-produced figures. The separate RAG model
may draft a cited answer, but it has no tools and no write path. Neither model
can move a figure or approve a dossier.

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
