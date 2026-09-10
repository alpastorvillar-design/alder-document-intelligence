"""What the evaluation harness is allowed to claim.

The harness produces the only numbers this project publishes, and it had no
tests. That is how it came to report `16/21 (76.2% rule-ID recall)` from a run
where the personnel registry and the call page were simply unreachable: the
rules that cross-check against them could not fire, `EXTERNAL_SOURCE_UNAVAILABLE`
fired in their place, and the figure measured the runner's network rather than
the pipeline.

A number that means something else in some environments is worse than no
number, because a reader cannot tell which they are looking at.
"""

from __future__ import annotations

from typing import Any

from evaluation.run_eval import (
    DEGRADED_RUN_NOTE,
    _counts_verdict,
    _detection_verdict,
    _precision_verdict,
    render_summary,
)


def totals(**overrides: Any) -> dict[str, Any]:
    base = {
        "documents_processed": 24,
        "input_bytes": 4_154_026,
        "fields_checked": 55,
        "fields_correct": 53,
        "field_accuracy": 0.9636,
        "ocr_fields_checked": 78,
        "ocr_fields_correct": 72,
        "ocr_field_accuracy": 0.9231,
        "expected_findings": 21,
        "findings_missed": 0,
        "findings_unexpected": 0,
        "finding_true_positives": 21,
        "finding_false_positives": 0,
        "finding_false_negatives": 0,
        "rule_id_precision": 1.0,
        "rule_id_recall": 1.0,
        "sources_complete": True,
        "retrieval_probes": 15,
        "retrieval_correct_targets": 15,
        "replay_stable": True,
        "replay_comparable": True,
    }
    base.update(overrides)
    return base


class TestDetectionFiguresAreWithheldWhenTheyWouldMislead:
    def test_a_complete_run_states_the_rate(self) -> None:
        verdict = _detection_verdict(totals())
        assert "21/21" in verdict
        assert "100.0% rule-ID recall" in verdict

    def test_a_run_missing_a_source_states_no_rate(self) -> None:
        """The numbers that were published: 16/21, and 5 false positives.

        Every one of those five was `EXTERNAL_SOURCE_UNAVAILABLE`, and every
        missed rule needed the registry or the call page. Reporting a recall
        figure from that run puts a number in front of a reader that is about
        the environment.
        """
        degraded = totals(
            sources_complete=False,
            findings_missed=5,
            finding_true_positives=16,
            finding_false_positives=5,
            finding_false_negatives=5,
            rule_id_recall=0.7619,
            rule_id_precision=0.7619,
        )
        for verdict in (
            _detection_verdict(degraded),
            _precision_verdict(degraded),
            _counts_verdict(degraded),
        ):
            assert "not measurable" in verdict
            assert "76" not in verdict
            assert "16/21" not in verdict

    def test_the_counts_are_still_stated_on_a_complete_run(self) -> None:
        assert _counts_verdict(totals()) == "0 / 0"
        assert _precision_verdict(totals()).startswith("21/21")


class TestTheSummarySaysWhichKindOfRunItIs:
    def _payload(self, **overrides: Any) -> dict[str, Any]:
        return {
            "generated_at": "2026-09-10T00:00:00+00:00",
            "run_id": "abc123",
            "total_seconds": 13.8,
            "semantic_provider": "deterministic",
            "dossiers": [],
            "totals": totals(**overrides),
            "savings_scenarios": [],
        }

    def test_a_degraded_run_carries_the_warning(self) -> None:
        summary = render_summary(self._payload(sources_complete=False))
        assert DEGRADED_RUN_NOTE in summary
        # And it explains that the missing source is correct behaviour, so a
        # reader does not read it as the pipeline being broken.
        assert "blocker and not an empty dataset" in summary

    def test_a_complete_run_does_not(self) -> None:
        summary = render_summary(self._payload())
        assert DEGRADED_RUN_NOTE not in summary
        assert "21/21" in summary
