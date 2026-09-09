**English** · [Español](es/limitaciones.md)

# Limitations

- The corpus is small, synthetic, Spanish, and deliberately regular. It proves
  paths and controls, not real-world generalisation.
- OCR uses one engine and language pack. Layout diversity, handwriting,
  multi-page tables, rotations, and poor mobile photos need a broader benchmark.
- Extraction patterns target this fixture contract. There is no generic document
  understanding claim.
- Lexical retrieval is measured on a few known queries. There is no vector index
  and no retrieval-augmented generation claim.
- The deterministic semantic provider is not a hosted model run. The optional
  hosted adapter has only local contract tests.
- The API key is a shared-secret demo guard; there is no user, role, tenant, or
  dossier authorisation model.
- Storage and reports are local files. Selective deletion, encryption, backup,
  immutability, retention, and legal hold are not implemented.
- Parser isolation, malware scanning, host egress enforcement, and production
  resource limits are absent.
- Metrics remain in process; there is no durable telemetry backend or alerting.
- The optional workflow lacks a hard overall polling-age ceiling and production
  authentication.
- Dependency locks improve reproducibility but do not replace vulnerability,
  licence, image, and provenance governance.
- No throughput, concurrency ceiling, availability, recovery-time, or cost claim
  is established by the demo.
