# Threat model

## Scope and assets

The reference implementation protects document bytes, extracted business data,
human decisions, reports, connector credentials, and the integrity of the audit
trail. Its actors are an API caller, a reviewer, an operator, the worker, the
local registry/page simulator, and an optional semantic provider.

The demonstration boundary is one trusted local operator and synthetic data.
This is not an authorisation or multi-tenant design.

## Principal threats and present controls

| Threat | Present control | Remaining production work |
| --- | --- | --- |
| Oversized or disguised upload | streamed request ceiling, signature sniffing, parser open, ZIP and image pixel limits | malware scanning and sandboxed conversion |
| Path traversal or filename collision | filenames are display-only; storage keys are validated SHA-256 digests | hardened object service and quotas |
| PDF/image/workbook parser abuse | bounded formats, encrypted/corrupt PDF rejection, macro rejection, OCR timeout | isolated workers, OS limits, parser patch process |
| Spreadsheet formula execution | formulas are not used as evidence; CSV cells are neutralised | safe downstream viewer policy |
| Server-side request forgery | scheme/port/host allowlist plus resolved-address checks | egress firewall, DNS pinning or proxy |
| HTML structure drift | required selectors fail visibly; raw capture is hashed | monitored contract ownership and change alerts |
| Prompt injection or invented fields | untrusted-data delimiters, typed schema, grounding, deterministic rules, human approval | provider governance, redaction, adversarial evaluation |
| Duplicate/concurrent actions | scoped idempotency keys, database uniqueness, conditional transitions, row locks, leases and fencing | distributed load testing and SLOs |
| Cross-dossier data mixing | every query and uniqueness boundary carries dossier id; dedicated tests | tenant-level database policies |
| Secret or personal-data leakage | no committed secrets, structured errors, local-only ports, synthetic corpus, history scan | secret manager, redaction/DLP, access logging |
| Audit tampering | append-only application flow and transactional writes | immutable external audit sink and retention locks |

## Privacy and retention

Real dossiers may contain employee and supplier personal data. A deployment
needs a documented lawful basis, purpose limitation, data minimisation,
role-based access, processor agreements, retention periods, deletion and legal
hold workflows, subject-right handling, breach response, and transfer assessment.
The demo implements none of those organisational controls and uses synthetic
identities only.

Object and report files are local volumes and temporary corpus files are local
runtime artefacts. Operators can remove the project volumes, but there is no
selective erasure API. Audit retention versus erasure obligations requires a
policy decision before real data is admitted.

## Authentication boundary

An optional shared API key is a demonstration guard, not production identity.
There are no users, organisations, roles, sessions, or tenant claims. Production
requires authenticated workload and reviewer identities, least privilege,
authorisation on every dossier, key rotation, rate limiting, and TLS.
