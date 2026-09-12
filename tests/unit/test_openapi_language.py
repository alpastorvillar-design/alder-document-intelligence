"""Everything OpenAPI publishes is in Spanish.

The rest of this codebase keeps its comments and docstrings in English, and a
route handler's docstring is the one that is not a comment: FastAPI publishes
it as the endpoint's description, so it is read by whoever uses the API. The
domain, the documents and the people who would use this are Spanish, and
`/docs` was the last thing a person reads that was not.

Which makes it the easiest thing to let slip back: a new endpoint written in
the language of the code around it, and nobody notices until somebody opens
the page.
"""

from __future__ import annotations

import re

import pytest

from iep.api.app import create_app

# Words that are only English, common enough to appear in any sentence written
# by accident in it, and that are not Spanish words with other meanings. `no`,
# `final`, `total`, `error` and `detail` are deliberately absent: they are
# Spanish too, or they are field names.
ENGLISH = re.compile(
    r"\b(the|and|with|that|which|from|when|this|these|those|every|what|"
    r"would|should|returns|means|never|only|still|there|about|into|been|"
    r"were|does|doesn't|cannot|can't|its|it's|they|their|because|"
    r"instead|rather|while|until|before|after|each|both|other|another)\b",
    re.IGNORECASE,
)

# Code spans carry route paths, field paths, enum values and header names, all
# of which stay in English because they are keys rather than prose.
CODE_SPAN = re.compile(r"`[^`]*`")


def published() -> list[tuple[str, str]]:
    """Every piece of prose the OpenAPI document exposes, with where it is."""
    spec = create_app().openapi()
    prose: list[tuple[str, str]] = [("la portada", spec["info"].get("description") or "")]
    for tag in spec.get("tags", []):
        prose.append((f"la etiqueta «{tag['name']}»", tag.get("description") or ""))
    for path, methods in spec["paths"].items():
        for verb, operation in methods.items():
            if verb not in {"get", "post", "patch", "put", "delete"}:
                continue
            where = f"{verb.upper()} {path}"
            prose.append((f"{where} · summary", operation.get("summary") or ""))
            prose.append((f"{where} · description", operation.get("description") or ""))
    return prose


def offenders() -> list[str]:
    found = []
    for where, text in published():
        for line in text.splitlines():
            without_code = CODE_SPAN.sub("", line)
            match = ENGLISH.search(without_code)
            if match:
                found.append(f"{where}: «{match.group(0)}» en “{without_code.strip()[:70]}”")
    return found


def test_nothing_published_is_in_english() -> None:
    found = offenders()
    assert not found, "OpenAPI publica texto en inglés:\n  " + "\n  ".join(found)


def test_every_operation_says_what_it_is_for() -> None:
    """A summary is the line shown in the collapsed list, so a missing one
    leaves an endpoint identified only by its verb and path."""
    spec = create_app().openapi()
    naked = [
        f"{verb.upper()} {path}"
        for path, methods in spec["paths"].items()
        for verb, operation in methods.items()
        if verb in {"get", "post", "patch", "put", "delete"} and not operation.get("summary")
    ]
    assert not naked, f"operaciones sin summary: {naked}"


def test_the_guard_can_actually_fail() -> None:
    """The regex is the whole test, so it is worth proving it bites.

    A scanner that matches nothing passes for the wrong reason, and this one is
    deliberately narrow: it ignores code spans, and its word list avoids
    anything that is also a Spanish word.
    """
    assert ENGLISH.search("Queues the work and returns the job")
    assert ENGLISH.search("One file per call") is None  # no English stopword in it
    # A path or a field name in backticks is not prose.
    assert CODE_SPAN.sub("", "usa `POST /dossiers/{id}/process` y espera") == "usa  y espera"
    assert ENGLISH.search(CODE_SPAN.sub("", "`the` es una clave, no prosa")) is None


@pytest.mark.parametrize("tag", ["system", "dossiers", "jobs", "review", "artifacts", "ui"])
def test_every_tag_is_described(tag: str) -> None:
    """The tag description is the paragraph above a whole group of endpoints."""
    spec = create_app().openapi()
    described = {entry["name"]: entry.get("description", "") for entry in spec.get("tags", [])}
    assert described.get(tag), f"la etiqueta «{tag}» no tiene descripción"
