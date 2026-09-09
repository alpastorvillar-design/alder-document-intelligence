"""The generated corpus is a stable measurement input, not a moving target."""

from __future__ import annotations

import hashlib
from pathlib import Path

from corpus.generate import write_corpus


def _tree_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_two_independent_corpus_builds_are_byte_identical(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    write_corpus(first)
    write_corpus(second)
    assert _tree_hashes(first) == _tree_hashes(second)
