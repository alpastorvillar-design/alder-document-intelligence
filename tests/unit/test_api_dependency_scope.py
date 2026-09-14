"""Transaction dependencies finish before FastAPI sends a response."""

from __future__ import annotations

import ast
from pathlib import Path

ROUTES = Path(__file__).resolve().parents[2] / "src" / "iep" / "api" / "routes"


def test_every_database_dependency_commits_before_the_response_is_sent() -> None:
    """A yield dependency defaults to request scope and may commit too late.

    FastAPI runs a function-scoped dependency's cleanup before sending the
    response.  The guard covers every route because a TestClient waits for
    request-scoped cleanup and cannot reproduce the production race.
    """
    offenders: list[str] = []
    for path in sorted(ROUTES.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id != "Depends" or not node.args:
                continue
            dependency = node.args[0]
            if not isinstance(dependency, ast.Name) or dependency.id != "db_session":
                continue
            scope = next(
                (keyword.value for keyword in node.keywords if keyword.arg == "scope"), None
            )
            if not isinstance(scope, ast.Constant) or scope.value != "function":
                offenders.append(f"{path.name}:{node.lineno}")

    assert not offenders, "db_session lacks function scope: " + ", ".join(offenders)
