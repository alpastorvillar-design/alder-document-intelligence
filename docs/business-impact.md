**English** · [Español](es/impacto-negocio.md)

# Business impact

The business value is not “using more AI”. It is reducing the manual effort to
assemble evidence while making every decision easier to defend.

## Scenario calculator

For a period, define:

- `D`: dossiers received;
- `M`: baseline manual minutes per dossier;
- `A`: assisted minutes per dossier, including review;
- `R`: proportion that can use the assisted flow;
- `H`: loaded reviewer cost per hour;
- `I`: implementation and operating cost for the period.

Then:

```text
hours released = D × R × max(M - A, 0) / 60
gross capacity value = hours released × H
scenario return = gross capacity value - I
```

These are scenario outputs, not savings claims. Inputs must come from a sampled
baseline and a controlled pilot. Also track first-pass completeness, review
rate, false positives, false negatives, evidence-retrieval success, rework,
cycle time, user adoption, and escalations. A faster system that misses blockers
or is not trusted by reviewers has negative value.

## Adoption design

The reviewer sees the original document location, the extracted value, the rule
that raised an issue, and the correction history. The tool does not hide
uncertainty or force an automated decision. A pilot should start with shadow
mode, compare results to experienced reviewers, classify disagreements, tune
thresholds, and only then change operating procedures.

Support ownership matters as much as the model: named process owner, data owner,
technical owner, triage path, training, release notes, and feedback review are
part of the operating design.
