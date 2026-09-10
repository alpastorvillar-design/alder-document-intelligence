"""A ceiling this process enforces on its own use of a language model.

What it is: a count of grounded answers this application produced in a rolling
window, read from the append-only audit trail, against a configured ceiling.
Past a fraction of that ceiling the next call is refused rather than warned
about, so an unattended loop cannot quietly spend a week's allowance.

What it is **not**: the remaining quota of somebody's subscription. No API
reports that, for either assistant CLI. A screen that displayed a number
called "remaining weekly usage" while actually showing this counter would be
worse than one that says what it is measuring - so this one says it, and the
screen repeats it.

The count comes from the trail rather than from a counter of its own for two
reasons: the trail cannot drift from what actually happened, and it survives a
restart without anywhere to persist to.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from iep.audit import service as audit
from iep.config import Settings
from iep.domain.enums import AuditAction


@dataclass(frozen=True)
class Budget:
    used: int
    ceiling: int
    window_days: int
    stop_at: int

    @property
    def unlimited(self) -> bool:
        return self.ceiling == 0

    @property
    def remaining(self) -> int:
        return 0 if self.unlimited else max(self.ceiling - self.used, 0)

    @property
    def fraction(self) -> float:
        return 0.0 if self.unlimited else min(self.used / self.ceiling, 1.0)

    @property
    def exhausted(self) -> bool:
        """At or past the stop threshold, so the next call is refused."""
        return not self.unlimited and self.used >= self.stop_at


class BudgetExhaustedError(RuntimeError):
    def __init__(self, budget: Budget) -> None:
        self.budget = budget
        super().__init__(
            f"El presupuesto local de consultas al modelo está agotado: "
            f"{budget.used} de {budget.ceiling} en los últimos "
            f"{budget.window_days} días, y se detiene al llegar a {budget.stop_at}."
        )


def current(session: Session, settings: Settings) -> Budget:
    ceiling = settings.rag_call_budget
    since = datetime.now(UTC) - timedelta(days=settings.rag_budget_window_days)
    used = audit.count_since(session, AuditAction.EVIDENCE_QUESTION_ANSWERED, since=since)
    # `int()` truncates, so a 40-call ceiling at 0.9 stops on the 36th rather
    # than after it. Refusing one call early is the safe direction.
    stop_at = 0 if ceiling == 0 else max(int(ceiling * settings.rag_budget_stop_fraction), 1)
    return Budget(
        used=used, ceiling=ceiling, window_days=settings.rag_budget_window_days, stop_at=stop_at
    )


def guard(session: Session, settings: Settings) -> Budget:
    """Refuse before spending, not after."""
    budget = current(session, settings)
    if budget.exhausted:
        raise BudgetExhaustedError(budget)
    return budget
