**English** · [Español](es/resultados-medidos.md)

# Measured results

No fixed result is copied into the repository. The canonical evidence is the
output of a fresh run:

```bash
python -m corpus.generate --out corpus/out
python -m evaluation.run_eval --corpus corpus/out --out evaluation/out
```

The evaluation rebuilds dossiers, runs the deterministic semantic provider and
real Tesseract OCR, and writes machine-readable JSON plus a readable summary. CI
publishes those files as the `evaluation-results` artefact.

It reports separately:

- field accuracy against independent ground truth;
- expected-incident true positives, false positives, false negatives, precision,
  and recall by rule id;
- OCR word confidence and field outcomes for the scanned receipts;
- retrieval target accuracy for fixed evidence queries;
- per-stage and total wall time;
- input bytes and peak Python allocation observed by `tracemalloc`;
- replay equality, including ids, values, locators, statuses, and finding detail;
- an explicitly labelled hosted-token and cost estimate;
- parameterised impact scenarios rather than claimed savings.

Results depend on the recorded commit, runner, CPU, Tesseract package/language
data, and corpus version. Latency and memory figures are observations of that run,
not capacity or service-level claims. A green test proves the fixture behaviour,
not general accuracy on unseen real documents.
