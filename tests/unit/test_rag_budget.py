"""The ceiling the application enforces on its own use of a model.

The arithmetic is worth pinning because getting it wrong fails in the
expensive direction: a threshold that rounds up spends past the stop, and a
window that counts everything ever recorded stops working after a week.

What this is *not* is the remaining quota of a subscription. That number is
not published by either CLI, and a counter labelled as if it were would be
worse than none - so the label is part of what is tested.
"""

from __future__ import annotations

from iep.config import Settings
from iep.retrieval.budget import Budget


def budget(used: int, ceiling: int = 40, fraction: float = 0.90) -> Budget:
    stop_at = 0 if ceiling == 0 else max(int(ceiling * fraction), 1)
    return Budget(used=used, ceiling=ceiling, window_days=7, stop_at=stop_at)


class TestTheStopIsBeforeTheCeilingNotAtIt:
    def test_ninety_percent_of_forty_stops_at_thirty_six(self) -> None:
        assert budget(0).stop_at == 36

    def test_it_runs_up_to_the_stop(self) -> None:
        assert not budget(35).exhausted

    def test_it_refuses_at_the_stop(self) -> None:
        """ "At 90% do not let it run" means the call at 36 is refused, not the
        one after it."""
        assert budget(36).exhausted

    def test_it_stays_refused_past_the_stop(self) -> None:
        assert budget(39).exhausted
        assert budget(100).exhausted

    def test_a_small_ceiling_still_leaves_one_call(self) -> None:
        """`int(1 * 0.9)` is 0, which would refuse before the first call."""
        assert budget(0, ceiling=1).stop_at == 1
        assert not budget(0, ceiling=1).exhausted
        assert budget(1, ceiling=1).exhausted

    def test_a_stricter_fraction_stops_earlier(self) -> None:
        assert budget(0, ceiling=40, fraction=0.5).stop_at == 20
        assert budget(20, ceiling=40, fraction=0.5).exhausted


class TestNoCeilingMeansNoCeiling:
    def test_zero_never_refuses(self) -> None:
        unlimited = budget(10_000, ceiling=0)
        assert unlimited.unlimited
        assert not unlimited.exhausted

    def test_it_reports_no_remaining_rather_than_a_negative(self) -> None:
        assert budget(10_000, ceiling=0).remaining == 0
        assert budget(0, ceiling=0).fraction == 0.0


class TestWhatItReportsToTheScreen:
    def test_remaining_never_goes_below_zero(self) -> None:
        assert budget(50).remaining == 0
        assert budget(30).remaining == 10

    def test_the_fraction_is_capped_at_one(self) -> None:
        assert budget(80).fraction == 1.0
        assert budget(20).fraction == 0.5


class TestTheDefaultsAreDeliberate:
    def test_the_shipped_ceiling_is_modest_and_the_window_is_a_week(self) -> None:
        """A demonstration left running should not be able to spend an
        afternoon's worth of calls in a loop."""
        settings = Settings()
        assert settings.rag_call_budget == 40
        assert settings.rag_budget_window_days == 7
        assert settings.rag_budget_stop_fraction == 0.90

    def test_generation_is_off_until_somebody_turns_it_on(self) -> None:
        assert Settings().rag_provider == "disabled"
