**English** · [Español](es/README.md)

# Documentation index

Every document exists in both languages, and each one links to its counterpart
on its first line.

## Start here

| Document | What it answers |
| --- | --- |
| [Guided walkthrough](walkthrough.md) | What is running, what every endpoint does, how to launch the demo, how to open n8n, where retrieval fits |
| [LLM demonstration](llm-demo.md) | How to watch the hosted-model path run — free against a local simulator, or against a real model for cents |
| [Five-minute demo script](demo.md) | What to show, in what order, and what to say about it |

## How it is built

| Document | What it answers |
| --- | --- |
| [Architecture](architecture.md) | The shape of the system, its modules, and why it is a monolith |
| [Domain model](domain-model.md) | Every entity, every field, and why each one exists |
| [Workflow and state machine](workflow.md) | The states, the transition that is missing on purpose, job lifecycle and recovery |
| [Ingestion and provenance](ingestion-and-provenance.md) | How untrusted files are accepted, and how every value keeps its source |
| [Validation strategy](validation-strategy.md) | What the rules check, and where the human gate sits |

## What it does and does not claim

| Document | What it answers |
| --- | --- |
| [Measured results](measured-results.md) | What the evaluation reports, and what those numbers do not mean |
| [AI safety](ai-safety.md) | What the semantic provider may and may not touch |
| [Threat model](threat-model.md) | Assets, threats, present controls, remaining work |
| [Limitations](limitations.md) | The honest list |
| [Production gap](production-gap.md) | What would have to change before real dossiers |
| [Operations](operations.md) | Starting, inspecting, failure and recovery, observability |
| [Business impact](business-impact.md) | A scenario calculator, not a savings claim |

## Decision records

| ADR | Decision |
| --- | --- |
| [0001](adr/0001-evidence-locators.md) | Every value carries where it came from |
| [0002](adr/0002-postgres-job-queue.md) | The queue is a table, not a broker |
| [0003](adr/0003-deterministic-rules-not-a-model.md) | The model does not do arithmetic |
| [0004](adr/0004-lexical-retrieval-not-rag.md) | Lexical search, and what would change that |
| [0005](adr/0005-no-agent-in-the-approval-path.md) | No agent between a document and an approval |

Also: [`automation/n8n/README.md`](../automation/n8n/README.md) for the optional
workflow.
