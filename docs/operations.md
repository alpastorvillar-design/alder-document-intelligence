# Operations

## Local topology

Compose starts PostgreSQL, a local source simulator, the API, and the worker.
The API and worker reuse `innovation-evidence-pipeline:local`; the simulator is
the same project image with a different command. The optional `n8n` profile adds
one third-party image. Named volumes and the Compose project name isolate state.

Only loopback ports are published. The application image runs as uid 10001 and
has writable access only to `/var/lib/iep` and its home. Health checks cover
PostgreSQL, API readiness, simulator readiness, and worker-process liveness.

## Start, inspect, and stop

```bash
docker compose config --quiet
docker compose up -d --build --wait postgres devsources api worker
docker compose ps
docker compose logs --tail=100 api worker
docker compose --profile n8n down
```

Add `--volumes` to the final command only when the local demo data should be
deleted. Never use a global Docker cleanup command for this project.

The API process runs `alembic upgrade head` before starting. Readiness checks
both database connectivity and the migration head. A migration gate also builds
and tears down the schema in an isolated PostgreSQL schema.

## Failure and recovery

Workers claim jobs with a lease and renew it while processing. A stopped worker
leaves a recoverable `RUNNING` row; another worker returns an expired job to
`PENDING`, unless its attempt ceiling was already reached. Completion and failure
updates are conditional on the current owner, so a late worker cannot overwrite
the replacement's result.

Recoverable connector, OCR, semantic, operating-system, and database-adjacent
errors use bounded attempts and exponential backoff. Invalid input fails without
pointless retries. Jobs retain a capped error message and correlation id; public
API failures contain the same correlation id and never a traceback.

## Observability

Logs are structured JSON with correlation, dossier, job, and worker context.
In-process counters and duration observations support the demo; a deployment
needs exported metrics, central log redaction, traces, dashboards, alerts, and
explicit SLOs. Do not log document bodies, bearer tokens, or API keys.
