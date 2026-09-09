"""Security properties of operator-facing diagnostics."""

from __future__ import annotations

import json
import logging

from iep.db import session as db_session
from iep.logging import JsonFormatter


def test_secret_shaped_fields_are_redacted_recursively() -> None:
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "event", (), None)
    record.integration = {"authorization": "Bearer not-for-logs", "nested": {"token": "raw"}}

    payload = json.loads(JsonFormatter().format(record))

    assert payload["integration"] == {
        "authorization": "[redacted]",
        "nested": {"token": "[redacted]"},
    }
    assert "not-for-logs" not in json.dumps(payload)


def test_sql_parameter_values_are_hidden_from_engine_errors() -> None:
    db_session.reset_engine()
    try:
        assert db_session.get_engine().hide_parameters is True
    finally:
        db_session.reset_engine()
