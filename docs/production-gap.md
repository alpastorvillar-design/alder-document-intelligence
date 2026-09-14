**English** · [Español](es/brecha-produccion.md)

# Production gap

This repository is a production-oriented reference implementation, not a
deployed service. It demonstrates the business control flow and exposes the
work still required instead of hiding it behind architectural vocabulary.

## Must close before real data

- Add organisation and user identity, dossier-level authorisation, TLS, rate
  limits, secret management, and an external immutable audit sink.
- Establish privacy roles, lawful basis, processor terms, regional constraints,
  retention, selective deletion, legal hold, and incident response.
- Isolate untrusted parsers in restricted workers with CPU, memory, file,
  process, and network limits; add malware scanning and patched-image policy.
- Enforce connector egress outside application code and remove DNS time-of-check
  versus time-of-use risk through a controlled proxy or pinned connection.
- Replace local object storage with encrypted, versioned storage and atomic
  report publication; add backup, restore, and disaster-recovery tests.
- Define SLOs, capacity targets, alerting, runbooks, support ownership, and a
  representative performance and adversarial evaluation.

## Scale and availability

API and worker share one image but are separate processes. Workers may scale
horizontally because claims use PostgreSQL `SKIP LOCKED`; leases and ownership
fencing recover abandoned work. No benchmark here establishes a safe production
concurrency, document-size distribution, latency SLO, or database ceiling.

The table queue is appropriate while transactional consistency and modest job
volume matter more than broker throughput. A broker becomes justified if
measured contention, isolation requirements, or independent delivery semantics
outgrow PostgreSQL. That decision needs load evidence, not fashion.

## Hosted semantics and orchestration

The hosted semantic adapter is contract-tested only against a local protocol
double. Enabling it requires a real-data evaluation, privacy approval, budgets,
redaction, provider observability, and failover policy. The model must remain
outside arithmetic and approval.

The optional workflow engine uses a local SQLite volume and no login in the
demo. Production needs its own database, authentication, execution retention,
bounded polling, error routing to an owned channel, and change-controlled import.
It is deliberately absent from the core processing path.

## Known engineering follow-ups

- Bind review decisions to authenticated immutable actor ids rather than strings.
- Add finding-level optimistic concurrency, not only field-level revisions.
- Add owner-token fencing and an operator recovery path for request-idempotency
  reservations left in flight by a process crash.
- Make report-file publication transactional with database state.
- Add hard end-to-end workflow age/attempt limits.
- Extend the Python dependency-vulnerability gate with licence, container-image,
  and software-bill-of-materials gates under an agreed remediation policy.
- Exercise large parallel workloads and controlled process termination in a
  production-like container runtime.
