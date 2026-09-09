"""Static contract checks for the optional n8n orchestration."""

from __future__ import annotations

import json
from pathlib import Path

WORKFLOW = Path("automation/n8n/dossier-review.json")


def test_workflow_has_stable_unique_identifiers_and_no_credentials() -> None:
    workflow = json.loads(WORKFLOW.read_text(encoding="utf-8"))
    node_ids = [node["id"] for node in workflow["nodes"]]
    node_names = [node["name"] for node in workflow["nodes"]]
    assert len(node_ids) == len(set(node_ids))
    assert len(node_names) == len(set(node_names))
    assert all("credentials" not in node for node in workflow["nodes"])
    assert "IEP_API_BASE_URL" in WORKFLOW.read_text(encoding="utf-8")


def test_failed_jobs_leave_the_polling_loop() -> None:
    workflow = json.loads(WORKFLOW.read_text(encoding="utf-8"))
    connections = workflow["connections"]
    failed = connections["Job failed?"]["main"]
    assert failed[0][0]["node"] == "Report the failure"
    assert failed[1][0]["node"] == "Give the worker a moment"
    failure_node = next(node for node in workflow["nodes"] if node["name"] == "Job failed?")
    terminal_states = {
        condition["rightValue"]
        for condition in failure_node["parameters"]["conditions"]["conditions"]
    }
    assert terminal_states == {"FAILED", "DEAD_LETTER"}
