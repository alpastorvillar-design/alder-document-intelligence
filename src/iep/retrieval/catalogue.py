"""Which models this process can actually reach, and under what name.

The screen lets a reviewer pick a model, so a model name arrives from a
client - and for the CLI backends it ends up in `argv`. That makes the
catalogue a security boundary, not a convenience: a name is only usable if it
appears here, and an unknown one is refused rather than passed through. A
validated identifier from a fixed set cannot become an argument.

Three backends, one flat namespace:

- `claude:<model>` - the Claude Code CLI on this host
- `codex:<model>`  - the Codex CLI on this host
- `ollama:<model>` - a local Ollama server, discovered at runtime

The first two are development-only, for the reasons in `rag_cli.py`. The third
is genuinely local: nothing leaves the machine, and it costs nothing, which is
why it is listed first and preselected.

Both CLI lists used to be hard-coded. Codex's no longer is - it is discovered
from the CLI itself, see `codex_appserver.py`.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from iep.config import Settings
from iep.retrieval.codex_appserver import CodexModel, list_models

# Backends whose availability is a question about this host, not about a
# network. Kept separate from the model list because "the CLI is installed" and
# "this model exists" fail for different reasons and need different messages.
CLI_BACKENDS = ("claude", "codex")
OLLAMA_BACKEND = "ollama"


@dataclass(frozen=True)
class ModelChoice:
    """One selectable model, named the way the screen and the API both use."""

    id: str
    backend: str
    model: str
    label: str
    # What it costs the person running this, in the only terms that matter to
    # them: whether the call leaves the machine.
    local: bool
    note: str = ""


# The Anthropic models worth offering here. Deliberately short: a reviewer
# choosing between eleven names is not choosing, and the interesting axis is
# fast-and-cheap against careful-and-slower.
CLAUDE_MODELS = (
    ("claude-haiku-4-5-20251001", "Haiku 4.5", "El más rápido y el más barato"),
    ("claude-sonnet-5", "Sonnet 5", "Más capaz, algo más lento"),
    ("claude-opus-5", "Opus 5", "El más capaz, el más lento"),
)

# What to offer for codex when its own catalogue cannot be read: one entry
# meaning "whatever the CLI is configured for", which is what shipped before
# the app-server made the real list reachable. An empty model id sends no
# `--model` at all, so this can never name a model that does not exist.
CODEX_FALLBACK = (("", "El configurado en Codex", "Lo que use `codex exec` por defecto"),)

# Asking codex costs a process and about two seconds, and the status endpoint
# is polled by the screen, so the answer is remembered. Short enough that a
# `codex update` shows up within the same sitting.
CODEX_CACHE_SECONDS = 600.0
_codex_cache: tuple[float, tuple[CodexModel, ...]] | None = None


def forget_codex_models() -> None:
    """Drop the cached list. For tests, and for a deliberate refresh."""
    global _codex_cache
    _codex_cache = None


def codex_models(*, discover: Callable[[], list[CodexModel]] | None = None) -> list[ModelChoice]:
    """The models codex says it can run, or the configured-default entry.

    Falling back rather than returning nothing is deliberate: a failed
    discovery must not remove the backend from the screen, because the CLI
    still works perfectly well without us knowing its inventory.
    """
    global _codex_cache
    # Resolved here rather than as a default argument. `discover=list_models`
    # in the signature binds the function object once, at import, so replacing
    # `catalogue.list_models` afterwards has no effect - which meant a test
    # that thought it had substituted the discovery was spawning the real CLI
    # and passing because this machine happened to have the model it named.
    # It failed on a runner that does not.
    discover = discover or list_models
    now = time.monotonic()
    if _codex_cache is not None and now - _codex_cache[0] < CODEX_CACHE_SECONDS:
        found = _codex_cache[1]
    else:
        try:
            found = tuple(discover())
        except Exception:
            found = ()
        _codex_cache = (now, found)

    if not found:
        return [
            ModelChoice(
                id="codex",
                backend="codex",
                model="",
                label=f"{label} · codex",
                local=False,
                note=note,
            )
            for _, label, note in CODEX_FALLBACK
        ]
    return [
        ModelChoice(
            id=f"codex:{model.id}",
            backend="codex",
            model=model.id,
            label=f"{model.display_name} · codex",
            local=False,
            # The vendor's own description, verbatim. Writing our own would be
            # inventing a claim about somebody else's model.
            note=_codex_note(model),
        )
        for model in found
    ]


def _codex_note(model: CodexModel) -> str:
    parts = [part for part in (model.description.strip(), _effort(model.default_effort)) if part]
    return " · ".join(parts)


def _effort(effort: str) -> str:
    """Reasoning effort in the terms the screen uses elsewhere: time and care."""
    return {
        "low": "esfuerzo bajo",
        "medium": "esfuerzo medio",
        "high": "esfuerzo alto",
        "xhigh": "esfuerzo muy alto",
    }.get(effort, "")


def ollama_models(settings: Settings) -> list[ModelChoice]:
    """Ask the local Ollama server what it has.

    Discovered rather than configured: the list is whatever the person pulled,
    and a hard-coded list would be wrong on any machine but one. A server that
    is not running is not an error here - it is one backend being unavailable,
    and the screen says so.
    """
    try:
        response = httpx.get(f"{settings.ollama_base_url.rstrip('/')}/api/tags", timeout=3.0)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        return []
    if not isinstance(payload, dict):
        return []

    choices: list[ModelChoice] = []
    for entry in payload.get("models") or ():
        if not isinstance(entry, dict):
            continue
        name = entry.get("model") or entry.get("name")
        if not isinstance(name, str) or not name:
            continue
        capabilities = entry.get("capabilities")
        skills = tuple(capabilities) if isinstance(capabilities, list) else ()
        # An embedding model cannot answer a question. Offering it would make
        # the picker look richer and the first attempt fail.
        if "embedding" in skills:
            continue
        if skills and "completion" not in skills:
            continue
        size = entry.get("size")
        note = _size_note(size) if isinstance(size, int) else ""
        choices.append(
            ModelChoice(
                id=f"{OLLAMA_BACKEND}:{name}",
                backend=OLLAMA_BACKEND,
                model=name,
                label=name,
                local=True,
                note=note,
            )
        )
    return sorted(choices, key=lambda choice: choice.model)


def _size_note(size_bytes: int) -> str:
    """How big the weights are, shown next to the name rather than hidden.

    It is the only number on that screen that predicts what choosing the model
    will feel like: one that does not fit in VRAM alongside its KV cache runs
    at host-memory speed, and switching models evicts the resident one, which
    costs about a minute of loading before the first token.
    """
    gigabytes = size_bytes / 1_000_000_000
    if gigabytes >= 1:
        return f"{gigabytes:.1f} GB".replace(".", ",")
    return f"{size_bytes / 1_000_000:.0f} MB"


def cli_models(available: dict[str, bool]) -> list[ModelChoice]:
    """The CLI-backed choices: a fixed short list for claude, a discovered one
    for codex.

    The asymmetry is not an oversight. `claude` takes any model name the
    account can reach and publishes no inventory over the CLI, so the three
    offered here are a deliberate editorial choice - fast, capable, most
    capable. Codex does publish one, so it is asked.
    """
    choices: list[ModelChoice] = []
    if available.get("claude"):
        choices += [
            ModelChoice(
                id=f"claude:{model}",
                backend="claude",
                model=model,
                label=f"{label} · claude",
                local=False,
                note=note,
            )
            for model, label, note in CLAUDE_MODELS
        ]
    if available.get("codex"):
        choices += codex_models()
    return choices


def catalogue(settings: Settings) -> list[ModelChoice]:
    """Everything selectable right now, in the order the screen shows it.

    Local first: a call that never leaves the machine is the one to reach for
    by default when demonstrating this, and putting it first says so without
    an explanation.
    """
    from iep.retrieval.rag_cli import available_tools

    local = ollama_models(settings)
    hosted = cli_models(available_tools())
    return [*local, *hosted]


def resolve(settings: Settings, model_id: str | None) -> ModelChoice | None:
    """The choice `model_id` names, or `None` if nothing offers it.

    `None` for an empty request means "use the configured default", which is
    resolved by the caller. An unknown id is not silently defaulted: a
    reviewer who picked a model and got a different one is being lied to, and
    a name that reaches `argv` unvalidated is worse than that.
    """
    if not model_id:
        return None
    for choice in catalogue(settings):
        if choice.id == model_id:
            return choice
    return None
