"""The dossier state machine.

Transitions are declared once here and enforced twice: this module rejects an
illegal move before it is attempted, and the persistence layer performs the
move as a conditional UPDATE so two concurrent writers cannot both win.
"""

from __future__ import annotations

from iep.domain.enums import DossierStatus as S

ALLOWED: dict[S, frozenset[S]] = {
    S.DRAFT: frozenset({S.INGESTED, S.FAILED}),
    # Documents can keep arriving after the first one, so INGESTED loops.
    S.INGESTED: frozenset({S.INGESTED, S.QUEUED, S.FAILED}),
    S.QUEUED: frozenset({S.PROCESSING, S.FAILED}),
    # Processing never approves. The only exit from a successful run is
    # review by a person.
    S.PROCESSING: frozenset({S.NEEDS_REVIEW, S.FAILED}),
    # Review can send a dossier back round: a correction re-runs validation.
    S.NEEDS_REVIEW: frozenset({S.QUEUED, S.NEEDS_REVIEW, S.APPROVED, S.REJECTED, S.FAILED}),
    # Terminal-but-reopenable: a rejected dossier can be resubmitted with new
    # documents. An approved one cannot be silently altered.
    S.REJECTED: frozenset({S.INGESTED}),
    S.APPROVED: frozenset(),
    S.FAILED: frozenset({S.QUEUED, S.INGESTED}),
}

TERMINAL: frozenset[S] = frozenset({S.APPROVED})


class InvalidTransitionError(Exception):
    def __init__(self, current: S, target: S) -> None:
        self.current = current
        self.target = target
        super().__init__(f"cannot move dossier from {current} to {target}")


def can_transition(current: S, target: S) -> bool:
    return target in ALLOWED[current]


def assert_transition(current: S, target: S) -> None:
    if not can_transition(current, target):
        raise InvalidTransitionError(current, target)


def sources_for(target: S) -> frozenset[S]:
    """Every state from which `target` is reachable in one step."""
    return frozenset(source for source, targets in ALLOWED.items() if target in targets)
