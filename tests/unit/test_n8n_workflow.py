"""Static contract checks for the optional n8n orchestrations."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# Resolved from this file rather than from the working directory: the same
# tests are run from the repository root in CI and from /app inside the image,
# where the optional automation directory is deliberately not shipped.
WORKFLOW_DIR = Path(__file__).resolve().parents[2] / "automation" / "n8n"
WORKFLOW_PATHS = sorted(WORKFLOW_DIR.glob("*.json"))

pytestmark = pytest.mark.skipif(
    not WORKFLOW_PATHS,
    reason="the optional n8n workflows are not part of the runtime image",
)


def load_workflow(filename: str) -> dict[str, object]:
    return json.loads((WORKFLOW_DIR / filename).read_text(encoding="utf-8"))


@pytest.mark.parametrize("workflow_path", WORKFLOW_PATHS, ids=lambda path: path.stem)
def test_workflow_has_stable_unique_identifiers_and_no_credentials(
    workflow_path: Path,
) -> None:
    raw = workflow_path.read_text(encoding="utf-8")
    workflow = json.loads(raw)
    node_ids = [node["id"] for node in workflow["nodes"]]
    node_names = [node["name"] for node in workflow["nodes"]]
    assert len(node_ids) == len(set(node_ids))
    assert len(node_names) == len(set(node_names))
    assert all("credentials" not in node for node in workflow["nodes"])
    assert "IEP_API_BASE_URL" in raw
    assert "C:\\Users\\" not in raw
    assert "api.openai.com" not in raw
    assert "api.anthropic.com" not in raw


def test_workflow_catalogue_is_deliberate() -> None:
    workflows = [json.loads(path.read_text(encoding="utf-8")) for path in WORKFLOW_PATHS]
    assert {workflow["id"] for workflow in workflows} == {
        "iep-approved-dossier-handoff",
        "iep-dossier-review",
        "iep-review-queue-digest",
    }
    assert all(workflow["active"] is False for workflow in workflows)


def test_failed_jobs_leave_the_polling_loop() -> None:
    workflow = load_workflow("dossier-review.json")
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


def test_queue_digest_is_read_only_and_has_scheduled_and_manual_entry_points() -> None:
    workflow = load_workflow("review-queue-digest.json")
    node_types = {node["type"] for node in workflow["nodes"]}
    assert "n8n-nodes-base.scheduleTrigger" in node_types
    assert "n8n-nodes-base.manualTrigger" in node_types

    requests = [node for node in workflow["nodes"] if node["type"] == "n8n-nodes-base.httpRequest"]
    assert requests
    assert all(node["parameters"].get("method", "GET") == "GET" for node in requests)
    query_parameters = requests[0]["parameters"]["queryParameters"]["parameters"]
    assert {parameter["name"]: parameter["value"] for parameter in query_parameters}[
        "dossier_status"
    ] == "NEEDS_REVIEW"
    assert "simulated:" in json.dumps(workflow)


def test_handoff_reads_an_export_only_after_the_approval_guard() -> None:
    workflow = load_workflow("approved-dossier-handoff.json")
    connections = workflow["connections"]
    approved_branches = connections["Approved?"]["main"]
    assert approved_branches[0][0]["node"] == "Fetch approved export"
    assert approved_branches[1][0]["node"] == "Block unapproved handoff"

    requests = [node for node in workflow["nodes"] if node["type"] == "n8n-nodes-base.httpRequest"]
    assert all(node["parameters"].get("method", "GET") == "GET" for node in requests)
    export_request = next(node for node in requests if node["name"] == "Fetch approved export")
    assert export_request["parameters"]["url"].endswith("/export.json")
    assert "simulated:" in json.dumps(workflow)
