# Workflow and state machine

## States

```
        ┌────────┐
        │ DRAFT  │  created, no documents yet
        └───┬────┘
            │ first accepted document
            ▼
    ┌───────────────┐  more documents may keep arriving
    │   INGESTED    │◀─┐
    └───────┬───────┘  │
            │ process  │
            ▼          │
      ┌──────────┐     │
      │  QUEUED  │     │
      └────┬─────┘     │
           │ worker claims
           ▼           │
    ┌──────────────┐   │
    │  PROCESSING  │   │
    └──────┬───────┘   │
           │ always    │
           ▼           │
  ┌──────────────────┐ │  a correction re-runs validation
  │   NEEDS_REVIEW   │─┘
  └───┬───────────┬──┘
      │ human     │ human
      ▼           ▼
 ┌──────────┐ ┌──────────┐
 │ APPROVED │ │ REJECTED │──▶ INGESTED (resubmitted with new documents)
 └──────────┘ └──────────┘
   terminal

  FAILED is reachable from every working state, and can be requeued.
```

Declared once in [`src/iep/domain/states.py`](../src/iep/domain/states.py) and
asserted in `tests/unit/test_domain.py`.

## The transition that is missing on purpose

`PROCESSING` cannot reach `APPROVED`. A successful run ends in `NEEDS_REVIEW`
whether or not anything was found, so approving a dossier is always a human
action with a recorded reason. The test that pins this is
`test_processing_cannot_approve`.

`APPROVED` has no outgoing transitions. An approved claim is what was submitted;
changing it afterwards would make the audit trail a story rather than a record.
Correcting an approved dossier means a new dossier.

Approval is refused while a blocking finding is open. A blocker has to be
dismissed with a reason first, and the dismissal is recorded — an automated
check that can be walked past silently is not a check.

## How a transition is applied

Two guards, and both are needed:

1. the state machine refuses a move it does not declare, before any write;
2. the write is a conditional `UPDATE ... WHERE id = ? AND status = ?` with
   `RETURNING`. If a concurrent writer already moved the row, nothing comes
   back, and the caller is told the dossier changed underneath it rather than
   overwriting the winner.

`test_two_writers_cannot_both_win_a_transition` drives this with two sessions.

## Job lifecycle

```
PENDING ──claim──▶ RUNNING ──▶ SUCCEEDED
   ▲                  │
   │                  ├─ recoverable failure, attempts left  ─▶ PENDING (backoff)
   │                  ├─ recoverable failure, attempts spent ─▶ DEAD_LETTER
   │                  └─ unrecoverable failure ──────────────▶ FAILED
   │
   └── lease expired: reclaimed by any worker
```

A malformed document does not become valid on a second attempt, so it is not
retried; a database blip or a timed-out connector is. `DEAD_LETTER` and
`FAILED` are inspectable states with the error attached, not a silent drop.

## Recovery

A worker killed mid-job leaves its row `RUNNING` with a lease it will never
renew. Any worker sweeps expired leases back to `PENDING` and records a
`JOB_RECLAIMED` audit event. Restarting the work is safe because the pipeline
is idempotent: extractions upsert on a key derived from document, field and
locator; chunks are rebuilt per document; findings upsert on their fingerprint;
and a human correction is never overwritten by a re-run.

`tests/e2e/test_worker_and_migrations.py` covers the reclaim, the audit event
and a full worker run.
