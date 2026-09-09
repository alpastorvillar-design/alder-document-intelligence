# ADR 0002: Use PostgreSQL as the job queue

**Status:** accepted for this workload

## Context

Enqueueing and dossier state must commit together. Jobs are expected in modest
numbers and each OCR operation is relatively slow.

## Decision

Store jobs in PostgreSQL. Workers claim with `FOR UPDATE SKIP LOCKED`, maintain a
lease, and fence success or failure by worker identity. Expired jobs are reclaimed
until their bounded attempt ceiling is reached. Idempotency is unique per dossier.

## Consequences

The design avoids a broker and a dual-write problem and supports several workers.
It also places polling and queue load on PostgreSQL. Measured contention,
throughput, isolation, or delivery needs may justify a broker later.
