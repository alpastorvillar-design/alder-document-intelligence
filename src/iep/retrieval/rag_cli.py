"""Grounded generation through an assistant CLI on the same host.

**Development only.** This provider exists so the integration point can be
demonstrated - a language model answering from a dossier's own retrieved
evidence, with its citations checked - without a metered API key. It is off by
default, it is not what a deployment would use, and `docs/rag.md` says why.

A deployment would use the hosted provider in `rag.py`: one HTTP call, a
schema-constrained response, a measurable latency budget and a bill. Shelling
out to an interactive assistant instead means a process launch per question,
no request-level rate limits, and a dependency on whatever version happens to
be installed on the machine. Those are the reasons it stays labelled.

What it does keep, because these are the properties that matter rather than
transport details:

- the same answer contract, validated the same way
- the same citation check: an id the generator did not receive is a refusal,
  not a footnote
- the same exclusion of documents flagged as carrying instructions aimed at an
  automated reader, applied before this is called

And what it adds, because a subprocess is a wider door than an HTTP call:

- `argv` as a list, never a shell string, so no document text can become a
  command
- the prompt on **stdin**, so nothing from a document lands in `argv` either
- an empty temporary working directory per call, deleted afterwards: a tool
  call that reads the filesystem finds nothing of this repository
- the CLI's own tools disabled by flag, and its sandbox set to read-only where
  it has one
- a wall-clock timeout, and the process killed when it expires
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass

from pydantic import ValidationError

from iep.retrieval.rag import (
    RAG_PROMPT_VERSION,
    ProviderAnswer,
    RagConfigurationError,
    RagGeneration,
    RagProviderError,
    bounded_evidence,
    system_prompt,
)
from iep.retrieval.search import EvidenceHit

# The hosted provider constrains the reply with a JSON schema, so the shared
# prompt only has to state the rules. A CLI has no such mechanism: asked for
# "the required structured output" it answered correctly - in Markdown, with
# the fields written out in prose - because nothing had told it what the shape
# was. So the shape is spelled out here, and only here: the *rules* stay in
# the one prompt both providers share, where they cannot drift apart.
_JSON_ONLY = """

FORMATO DE SALIDA - obligatorio y sin excepciones.

Responde con UN solo objeto JSON y absolutamente nada mas: sin texto antes ni
despues, sin explicacion, sin Markdown y sin vallas de codigo.

{"answer": "<tu respuesta en espanol>", "citations": ["E1"], "sufficient_evidence": true}

- answer: texto plano, sin Markdown.
- citations: solo valores evidence_id del array recibido. Ninguno inventado.
- sufficient_evidence: false si la evidencia recibida no sostiene la respuesta,
  y en ese caso dilo en answer.
"""

# Tools an assistant CLI offers that have no business being available while it
# answers a question about a document. Named rather than relying on a default:
# a future version that adds a tool should not silently gain it here.
_DENIED_TOOLS = (
    "Bash",
    "Read",
    "Write",
    "Edit",
    "NotebookEdit",
    "Glob",
    "Grep",
    "WebFetch",
    "WebSearch",
    "Task",
    "TodoWrite",
)


@dataclass(frozen=True)
class CliTool:
    """How to run one assistant CLI non-interactively, bounded.

    The flags are not guesses. Each preset was run against both CLIs and the
    combination recorded here is the one that answers from the supplied
    evidence, uses no tools, persists no session, and prints something this
    module can parse.
    """

    name: str
    binary: str
    # The instruction text goes in a flag for one CLI and in the prompt for the
    # other, because only one of them can replace its system prompt.
    instructions_in_prompt: bool

    def argv(self, *, model: str, instructions: str) -> list[str]:
        if self.name == "claude":
            # Claude Code refuses work it reads as outside software
            # engineering, so the default system prompt has to be replaced
            # rather than appended to - `--append-system-prompt` leaves the
            # refusal in place.
            args = [
                self.binary,
                "--print",
                "--output-format",
                "json",
                "--strict-mcp-config",
                "--no-session-persistence",
                "--system-prompt",
                instructions,
                "--disallowed-tools",
                *_DENIED_TOOLS,
            ]
        elif self.name == "codex":
            args = [
                self.binary,
                "exec",
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
                "--ephemeral",
            ]
        else:  # pragma: no cover - the settings validator refuses others
            raise RagConfigurationError(f"Unknown CLI tool {self.name!r}.")
        if model:
            args += ["--model", model]
        if self.name == "codex":
            # `-` is codex's "read the prompt from stdin", and must come last.
            args.append("-")
        return args


TOOLS = {
    "claude": CliTool(name="claude", binary="claude", instructions_in_prompt=False),
    "codex": CliTool(name="codex", binary="codex", instructions_in_prompt=True),
}

# A balanced-brace scan is not worth writing: the answer object is flat, so a
# non-greedy match from `{` to the closing `}` of the last key is enough. Every
# candidate is validated against the contract anyway, and the *last* one that
# validates wins - codex echoes the prompt back into its own stdout, so the
# first JSON object on screen is the question, not the answer.
_OBJECT = re.compile(r"\{[^{}]*\}", re.DOTALL)


def _json_object(text: str) -> dict[str, object] | None:
    try:
        loaded = json.loads(text)
    except ValueError:
        return None
    return loaded if isinstance(loaded, dict) else None


# `input_tokens` alone counts only what was not served from cache, which for
# a CLI carrying its own system prompt is a handful of tokens - reporting 10
# for a call that actually read eighteen thousand is worse than reporting
# nothing. The cache fields are part of the input and are added in.
_INPUT_TOKEN_KEYS = (
    "input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def _envelope_usage(envelope: dict[str, object]) -> tuple[int | None, int | None]:
    usage = envelope.get("usage")
    if not isinstance(usage, dict):
        return (None, None)
    counted = [usage.get(key) for key in _INPUT_TOKEN_KEYS]
    got = sum(value for value in counted if isinstance(value, int))
    wrote = usage.get("output_tokens")
    return (
        got if any(isinstance(value, int) for value in counted) else None,
        wrote if isinstance(wrote, int) else None,
    )


def _last_valid(text: str) -> ProviderAnswer | None:
    """The last JSON object in `text` that satisfies the answer contract.

    A span that does not parse, or parses but is not an answer, is simply not
    the answer - both raise `ValidationError`, and both mean "keep looking".
    """
    found: ProviderAnswer | None = None
    for candidate in _OBJECT.finditer(text):
        try:
            found = ProviderAnswer.model_validate_json(candidate.group(0))
        except ValidationError:
            continue
    return found


class CliRagGenerator:
    name = "cli"

    def __init__(
        self,
        *,
        tool: CliTool,
        model: str,
        timeout_seconds: float,
        max_context_chars: int,
    ) -> None:
        if shutil.which(tool.binary) is None:
            raise RagConfigurationError(
                f"The {tool.name!r} CLI is not on this process's PATH. The API runs in a "
                f"container by default and the CLI is installed on the host, so this "
                f"provider only works when the API runs on the host too - see docs/rag.md."
            )
        self.tool = tool
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_context_chars = max_context_chars

    def generate(self, question: str, hits: list[EvidenceHit]) -> RagGeneration:
        evidence = bounded_evidence(hits, self.max_context_chars)
        allowed_ids = {item["evidence_id"] for item in evidence}
        rules = system_prompt()
        instructions = rules + _JSON_ONLY
        request = json.dumps(
            {"question": question, "EVIDENCE_JSON": evidence},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        stdin = f"{instructions}\n\n{request}" if self.tool.instructions_in_prompt else request

        stdout = self._run(stdin, instructions)
        parsed, usage = self._parse(stdout)
        citation_ids = tuple(dict.fromkeys(parsed.citations))
        if not set(citation_ids).issubset(allowed_ids):
            raise RagProviderError("The RAG provider invented an evidence citation.")

        return RagGeneration(
            answer=parsed.answer,
            citation_ids=citation_ids,
            sufficient_evidence=parsed.sufficient_evidence,
            provider=f"{self.name}:{self.tool.name}",
            model=self.model or f"{self.tool.name} default",
            input_tokens=usage[0],
            output_tokens=usage[1],
            # The rules are shared and versioned; the hash covers what this
            # provider actually sent, which includes the shape appendix.
            prompt_version=RAG_PROMPT_VERSION,
            prompt_sha256=hashlib.sha256(instructions.encode("utf-8")).hexdigest(),
        )

    def _run(self, stdin: str, instructions: str) -> str:
        argv = self.tool.argv(model=self.model, instructions=instructions)
        with tempfile.TemporaryDirectory(prefix="iep-rag-cli-") as workdir:
            try:
                completed = subprocess.run(  # noqa: S603 - argv list, never a shell string
                    argv,
                    input=stdin,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=self.timeout_seconds,
                    cwd=workdir,
                    check=False,
                    # An assistant CLI started from a repository picks that
                    # repository up as context. An empty directory it owns for
                    # the length of one call has nothing to pick up.
                    shell=False,
                )
            except FileNotFoundError as exc:
                raise RagConfigurationError(
                    f"The {self.tool.binary!r} CLI disappeared between the check and the call."
                ) from exc
            except subprocess.TimeoutExpired as exc:
                raise RagProviderError(
                    f"The {self.tool.name} CLI did not answer within "
                    f"{self.timeout_seconds:.0f} seconds."
                ) from exc

        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip().splitlines()
            tail = detail[-1][:200] if detail else f"exit code {completed.returncode}"
            raise RagProviderError(f"The {self.tool.name} CLI failed: {tail}")
        return completed.stdout

    def _parse(self, stdout: str) -> tuple[ProviderAnswer, tuple[int | None, int | None]]:
        """The answer, and the token counts if the CLI volunteered them.

        Both CLIs surround their answer with text - a banner, a fenced block,
        a token count - and one of them echoes the prompt back into its own
        output. Rather than model each layout, every JSON object in the stream
        is tried against the contract and the last valid one wins.
        """
        envelope = _json_object(stdout)
        if envelope is not None:
            # Claude's `--output-format json` wraps the reply in an envelope,
            # so the contract object is a *string* inside it - and the usage
            # counts are real numbers beside it, worth keeping.
            result = envelope.get("result")
            found = _last_valid(result) if isinstance(result, str) else None
            if found is not None:
                return found, _envelope_usage(envelope)

        found = _last_valid(stdout)
        if found is None:
            raise RagProviderError(
                f"The {self.tool.name} CLI printed no answer matching the expected contract."
            )
        return found, (None, None)


def build_cli_generator(
    *, tool_name: str, model: str, timeout_seconds: float, max_context_chars: int
) -> CliRagGenerator:
    tool = TOOLS.get(tool_name)
    if tool is None:
        raise RagConfigurationError(
            f"rag_cli_tool must be one of {sorted(TOOLS)}, not {tool_name!r}."
        )
    return CliRagGenerator(
        tool=tool,
        model=model,
        timeout_seconds=timeout_seconds,
        max_context_chars=max_context_chars,
    )


def available_tools() -> dict[str, bool]:
    """Which CLIs this process could actually launch, for the screen to say."""
    return {name: shutil.which(tool.binary) is not None for name, tool in TOOLS.items()}
