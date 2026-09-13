**English** · [Español](README.es.md)

# n8n orchestration

This directory contains three importable workflows. They are optional:
`docker compose up` does not start n8n, and the core, tests and evaluation
harness run without it.

| File | Pattern demonstrated | Outcome |
| --- | --- | --- |
| `dossier-review.json` | webhook, idempotency, asynchronous work, polling and shared error handling | processes and routes a dossier for review |
| `review-queue-digest.json` | schedule, collection read, aggregation and simulated output | daily digest of the pending queue |
| `approved-dossier-handoff.json` | webhook, state guard and contract mapping | prepares an approved dossier for handoff |

## 1. Processing and routing

```
webhook (or manual trigger)
  -> find the dossier by its business reference
  -> POST /dossiers/{id}/process        (with an Idempotency-Key)
  -> wait, then poll GET /jobs/{id} until the job leaves PENDING/RUNNING
  -> GET /dossiers/{id}
  -> if NEEDS_REVIEW: list the open findings and raise a notification
     otherwise: report that no reviewer action is needed
  -> any HTTP failure on any step lands on one error branch that answers the
     caller with the correlation id
```

The business logic is not here. The workflow decides *who to tell*; what counts
as a finding, what a document says and whether a dossier may be approved are all
decided in Python, behind the API, where they are versioned and tested. That
split is the point of using a workflow tool at all — moving a validation rule
into a node would put it somewhere with no tests and no history.

## 2. Daily review digest

`review-queue-digest.json` can run manually and carries an 08:00 daily trigger.
It reads only dossiers in `NEEDS_REVIEW`, totals their claimed amount and builds
an ordered list with links to the review screen. The output resembles an email
or chat message but calls no external service and contains no credentials. In a
deployment, the last node would be replaced by an authorised corporate
connector.

## 3. Approved dossier handoff

`approved-dossier-handoff.json` accepts a reference, reads its state and fetches
`export.json` only when the dossier is `APPROVED`. A dossier under review or
already rejected takes the blocked branch. The final mapping produces an
`innovation.dossier.approved.v1` contract, but the ERP or document-management
delivery is simulated: this repository does not claim that integration.

The two new workflows are deliberately read-only. They demonstrate
orchestration, prioritisation and a safety gate without copying domain rules or
state transitions out of the backend.

## Why the pieces are the way they are

- **Idempotency-Key on the process call.** n8n retries. Without the key, a retry
  after a timeout would queue the dossier twice.
- **Correlation id passed through.** Every request carries
  `X-Correlation-ID: n8n-<execution id>`, so a workflow execution can be found
  in the API and worker logs.
- **Polling rather than a callback.** A webhook back into n8n would be less
  code here and more coupling: the pipeline would need to know about n8n. The
  job endpoint already exposes what an orchestrator needs.
- **One error branch.** Each HTTP node continues on its error output into a
  single node that reports the failure with the correlation id, instead of the
  execution stopping silently.
- **No credentials.** The only host it talks to is the API on the compose
  network. Nothing external is configured, and the notification step is a Set
  node that formats the message it *would* send.

## Importing and running them

The development script selects the correct API address for n8n:

```powershell
# API and worker in containers
powershell -ExecutionPolicy Bypass -File scripts/dev.ps1 -Mode container -WithN8n

# API and worker on Windows (required by the local CLI adapters)
powershell -ExecutionPolicy Bypass -File scripts/dev.ps1 -Mode host -WithN8n
```

Import and activate before starting the server so the demo's SQLite file has a
single writer. Then open http://localhost:5678 or call the webhook:

```bash
docker compose --profile n8n stop n8n
docker compose --profile n8n run --rm --no-deps n8n \
  import:workflow --input=/workflows/dossier-review.json
docker compose --profile n8n run --rm --no-deps n8n \
  import:workflow --input=/workflows/review-queue-digest.json
docker compose --profile n8n run --rm --no-deps n8n \
  import:workflow --input=/workflows/approved-dossier-handoff.json
docker compose --profile n8n run --rm --no-deps n8n \
  update:workflow --id=iep-dossier-review --active=true
docker compose --profile n8n run --rm --no-deps n8n \
  update:workflow --id=iep-approved-dossier-handoff --active=true
docker compose --profile n8n up -d --wait n8n
curl -X POST http://localhost:5678/webhook/dossier-review \
     -H 'content-type: application/json' \
     -d '{"reference":"INN-2025-042"}'
curl -X POST http://localhost:5678/webhook/approved-dossier-handoff \
     -H 'content-type: application/json' \
     -d '{"reference":"INN-2025-041"}'
```

Each workflow carries a stable `id`, so re-importing updates it in place rather
than leaving copies. Activation is deliberately not embedded in the files: a
workflow that arrives listening or running a schedule is a surprise, not a
feature. The digest is run manually in the demonstration and is activated only
after its schedule has been accepted by the process operator.

## Verification

The Python suite validates stable unique node ids, absence of embedded
credentials and local paths, and routing of failed jobs out of the polling loop.
The container CI job imports the workflow into the pinned n8n image and executes
it against a freshly seeded local stack. Treat only the matching CI run as proof
of runtime behaviour; finding counts belong to the generated evaluation output,
not this document.

## Limits

- The first workflow waits a fixed four seconds before polling and the loop has no attempt
  ceiling. For a real deployment that becomes a bounded retry with a maximum
  age, and the job endpoint gains a "give up after" contract.
- The digest and handoff end in simulated nodes. Adding email, Teams, an ERP or
  a document manager requires credentials, authorisation and a real contract.
- n8n stores its own state in a SQLite volume here. A real deployment would
  point it at PostgreSQL and put it behind authentication.
- The structural tests do not replace a real import and execution. CI performs
  that runtime smoke check against the pinned image.
