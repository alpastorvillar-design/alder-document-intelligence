"""What this application has spent on models, and what it cannot know.

Two different questions get confused here, so they are answered separately.

**What has this application spent?** Answerable exactly. Every grounded answer
writes an audit row carrying its provider, model and token counts, so calls,
tokens and - where a backend reports one - cost can be summed over a window
from the trail itself. Nothing to keep in step, nothing to reset by restarting.

**How much of the subscription is left?** Not answerable from here. Neither
assistant CLI publishes it: `claude auth status --json` returns the account,
the auth method and the plan, and no quota; `codex login status` returns a
line of text. A meter labelled "remaining" that actually showed the first
number would be worse than no meter, so this module reports what it measured
and says what it did not.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from iep.config import Settings
from iep.db.models import AuditEvent
from iep.domain.enums import AuditAction


@dataclass(frozen=True)
class ModelSpend:
    model: str
    calls: int
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class Usage:
    """Measured from the trail, over the same window as the call budget."""

    window_days: int
    calls: int
    input_tokens: int
    output_tokens: int
    by_model: list[ModelSpend] = field(default_factory=list)
    local_calls: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


def measured(session: Session, settings: Settings) -> Usage:
    since = datetime.now(UTC) - timedelta(days=settings.rag_budget_window_days)
    rows = list(
        session.execute(
            select(AuditEvent.payload).where(
                AuditEvent.action == str(AuditAction.EVIDENCE_QUESTION_ANSWERED),
                AuditEvent.created_at >= since,
            )
        ).scalars()
    )

    per_model: dict[str, list[int]] = {}
    got = wrote = local = 0
    for payload in rows:
        if not isinstance(payload, dict):
            continue
        model = str(payload.get("model") or "desconocido")
        # Older rows predate the token fields. Absent is not zero, but for a
        # running total it has to be treated as such - and saying so here is
        # cheaper than a nullable total nobody can add up.
        entry = per_model.setdefault(model, [0, 0, 0])
        entry[0] += 1
        entry[1] += _as_int(payload.get("input_tokens"))
        entry[2] += _as_int(payload.get("output_tokens"))
        got += _as_int(payload.get("input_tokens"))
        wrote += _as_int(payload.get("output_tokens"))
        if str(payload.get("provider") or "").startswith("ollama"):
            local += 1

    by_model = [
        ModelSpend(model=model, calls=counts[0], input_tokens=counts[1], output_tokens=counts[2])
        for model, counts in sorted(per_model.items(), key=lambda item: -item[1][0])
    ]
    return Usage(
        window_days=settings.rag_budget_window_days,
        calls=len(rows),
        input_tokens=got,
        output_tokens=wrote,
        by_model=by_model,
        local_calls=local,
    )


def _as_int(value: object) -> int:
    return value if isinstance(value, int) else 0


@dataclass(frozen=True)
class Account:
    """What the CLI will say about the account it is signed in as.

    Read by asking the CLI, not by reading its credential store: this process
    has no business opening somebody's token file, and the CLI already offers
    the answer through a command designed for it.
    """

    tool: str
    logged_in: bool
    method: str = ""
    plan: str = ""
    detail: str = ""


def account(tool: str = "claude") -> Account:
    binary = shutil.which(tool)
    if binary is None:
        return Account(tool=tool, logged_in=False, detail="no está en el PATH de este proceso")
    argv = [binary, "auth", "status", "--json"] if tool == "claude" else [binary, "login", "status"]
    try:
        done = subprocess.run(  # noqa: S603 - argv list, fixed command, no shell
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20.0,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return Account(tool=tool, logged_in=False, detail="no respondió")

    if done.returncode != 0:
        return Account(tool=tool, logged_in=False, detail="sesión no iniciada")
    if tool != "claude":
        line = (done.stdout or "").strip().splitlines()
        return Account(tool=tool, logged_in=bool(line), detail=line[0][:120] if line else "")

    try:
        body = json.loads(done.stdout)
    except ValueError:
        return Account(tool=tool, logged_in=False, detail="respuesta ilegible")
    if not isinstance(body, dict):
        return Account(tool=tool, logged_in=False, detail="respuesta ilegible")
    return Account(
        tool=tool,
        logged_in=bool(body.get("loggedIn")),
        # `claude.ai` here means OAuth against the person's own account, not
        # an API key held by this application - which is the whole reason a
        # demonstration can run without a key.
        method=str(body.get("authMethod") or ""),
        plan=str(body.get("subscriptionType") or ""),
    )
