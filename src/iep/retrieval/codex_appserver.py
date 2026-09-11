"""Ask the Codex CLI which models it offers, over its own app-server protocol.

The picker used to carry a single entry reading "whatever Codex is configured
for", justified by the list not being enumerable from here. It is: the CLI
ships an app-server that speaks JSON-RPC over stdio, and `model/list` returns
the same catalogue the interactive `/model` picker draws - ids, display names,
the vendor's own one-line descriptions, and the reasoning effort each model
defaults to. Measured cold on this machine, the answer arrives in 1.8 s.

Two things this deliberately does not do.

It does not read `~/.codex/`. The configuration file would give one model and
the session history would give the ones used before, but session rollouts are
a record of someone's work and an evidence pipeline has no business reading
them to populate a dropdown.

It does not ask for the account. `account/read` answers with the signed-in
email address, and no screen in this system needs it - the plan type is the
only part that would be interesting, and not interesting enough to handle a
personal address for. Remaining subscription quota is not queryable at all:
it arrives as an `account/rateLimitsUpdated` notification after a turn runs,
which is why the interactive `/status` shows what the last call happened to
learn rather than a live figure.

The protocol is marked experimental by its authors, so every failure here is
one backend being unavailable rather than an error: the caller falls back to
the configured default, which is what shipped before this existed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass

# The client half of the handshake. `experimentalApi` is required: without it
# `model/list` is not answered at all - it returns nothing and the request
# simply never completes, which is a worse failure than an error.
CLIENT_NAME = "innovation-evidence-pipeline"
CLIENT_VERSION = "0.1.0"

# Long enough for a cold start (1.8 s measured, plus room for a loaded
# machine), short enough that a screen waiting on it is not left hanging.
TIMEOUT_SECONDS = 12.0


@dataclass(frozen=True)
class CodexModel:
    """One model the Codex CLI says it can run."""

    id: str
    display_name: str
    description: str
    default_effort: str


def _drain(stream: object, sink: list[str]) -> None:
    for line in stream:  # type: ignore[attr-defined]
        sink.append(line.rstrip("\n"))


def list_models(*, binary: str = "codex", timeout: float = TIMEOUT_SECONDS) -> list[CodexModel]:
    """The models `codex exec --model` will accept, or `[]` if it cannot say.

    Hidden models are dropped: the flag exists because the vendor does not
    want them in a picker, and second-guessing that would put names in front
    of a reviewer that the tool itself is hiding.
    """
    if shutil.which(binary) is None:
        return []

    try:
        process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell, no interpolation
            [binary, "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
    except OSError:
        return []

    lines: list[str] = []
    reader = threading.Thread(target=_drain, args=(process.stdout, lines), daemon=True)
    reader.start()

    def send(message: dict[str, object]) -> None:
        assert process.stdin is not None
        process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()

    try:
        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
                    "capabilities": {"experimentalApi": True},
                },
            }
        )
        deadline = time.monotonic() + timeout
        # `is None` rather than falsiness: an empty result object is a
        # successful handshake, and treating `{}` as failure would abandon a
        # server that had just agreed to talk.
        if _wait_for(lines, 1, deadline) is None:
            return []
        send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
        send({"jsonrpc": "2.0", "id": 2, "method": "model/list", "params": {}})
        payload = _wait_for(lines, 2, deadline)
    except (OSError, ValueError):
        return []
    finally:
        process.kill()

    if payload is None:
        return []
    return _models(payload)


def _wait_for(lines: list[str], request_id: int, deadline: float) -> dict[str, object] | None:
    """The result for `request_id`, or `None` on timeout or error.

    Notifications arrive interleaved with responses on the same stream, so
    matching on the id is the only way to read this: the next line is very
    often `remoteControl/status/changed` rather than the answer.
    """
    while time.monotonic() < deadline:
        for raw in list(lines):
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(message, dict) or message.get("id") != request_id:
                continue
            if "error" in message:
                return None
            result = message.get("result")
            return result if isinstance(result, dict) else {}
        time.sleep(0.05)
    return None


def _models(payload: dict[str, object]) -> list[CodexModel]:
    entries = payload.get("data")
    if not isinstance(entries, list):
        return []
    models: list[CodexModel] = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("hidden"):
            continue
        identifier = entry.get("id")
        if not isinstance(identifier, str) or not identifier:
            continue
        display = entry.get("displayName")
        effort = entry.get("defaultReasoningEffort")
        description = entry.get("description")
        models.append(
            CodexModel(
                id=identifier,
                display_name=display if isinstance(display, str) and display else identifier,
                description=description if isinstance(description, str) else "",
                default_effort=effort if isinstance(effort, str) else "",
            )
        )
    return models
