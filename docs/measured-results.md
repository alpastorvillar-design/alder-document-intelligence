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

It needs the development source simulator running: unlike the tests, which
inject doubles, the harness uses the real connectors. Pass
`--call-page-url http://127.0.0.1:8080/public/convocatoria.html` when the
simulator is on the host rather than in Compose.

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

## What it refuses to report

Two figures are withheld rather than printed when they would measure something
other than the pipeline.

**Detection, when a source was unavailable.** Several rules cross-check the
timesheet against the personnel registry, or a claim against the published
call. If those cannot be reached, `EXTERNAL_SOURCE_UNAVAILABLE` is raised -
which is the correct behaviour, because a missing source is a blocker and not
an empty dataset - and those rules cannot fire. Recall and precision then drop
for a reason that has nothing to do with detection, so the summary says the
figures are not measurable in that run and why. A number that silently means
something else in some environments is worse than no number, because a reader
cannot tell which one they have.

**Replay, when the two runs saw different sources.** The simulator fails and
rate-limits on a deliberate schedule. If one run lost a source and the other
did not, the states legitimately differ and the comparison says nothing about
idempotence, so it is reported as not comparable rather than as a failure.

Results depend on the recorded commit, runner, CPU, Tesseract package/language
data, and corpus version. Latency and memory figures are observations of that run,
not capacity or service-level claims. A green test proves the fixture behaviour,
not general accuracy on unseen real documents.
