**English** · [Español](../es/adr/0005-sin-agente-en-la-aprobacion.md)

# ADR 0005: No autonomous agent in the approval path

**Status:** accepted

## Context

An open-ended agent can choose tools and next actions, which is useful for some
exploratory tasks but conflicts with a bounded evidence-control workflow.

## Decision

Use an explicit state machine and a fixed pipeline. Optional workflow automation
may start and route work but cannot change validation logic or approve a dossier.
Human actions are explicit, reasoned, and audited.

## Consequences

The system gives up autonomous flexibility. It gains a finite attack surface,
repeatable replay, predictable error paths, and a clear accountability boundary.
