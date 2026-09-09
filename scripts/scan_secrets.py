"""Fail when tracked Git content contains a high-confidence credential signature."""

from __future__ import annotations

import re
import subprocess
import sys

MAX_BLOB_BYTES = 2 * 1024 * 1024

SIGNATURES = {
    "private key": re.compile(b"-----BEGIN " + b"(?:RSA |EC |OPENSSH )?" + b"PRIVATE KEY-----"),
    "GitHub token": re.compile(b"gh" + b"[pousr]_[A-Za-z0-9_]{32,}"),
    "AWS access key": re.compile(b"AK" + b"IA[0-9A-Z]{16}"),
    "OpenAI-style key": re.compile(b"sk" + b"-[A-Za-z0-9_-]{32,}"),
    "Slack token": re.compile(b"xox" + b"[abprs]-[A-Za-z0-9-]{20,}"),
    "JWT": re.compile(b"eyJ[A-Za-z0-9_-]{12,}\\.eyJ[A-Za-z0-9_-]{12,}\\.[A-Za-z0-9_-]{12,}"),
}


def _run(*args: str) -> bytes:
    return subprocess.check_output(args, stderr=subprocess.DEVNULL)  # noqa: S603


def _revisions() -> list[str]:
    revisions = _run("git", "rev-list", "--all").decode().splitlines()
    return revisions or ["HEAD"]


def _paths(revision: str) -> list[str]:
    raw = _run("git", "ls-tree", "-r", "--name-only", "-z", revision)
    return [part.decode("utf-8", errors="replace") for part in raw.split(b"\0") if part]


def main() -> int:
    findings: list[str] = []
    for revision in _revisions():
        for path in _paths(revision):
            try:
                blob = _run("git", "show", f"{revision}:{path}")
            except subprocess.CalledProcessError:
                continue
            if len(blob) > MAX_BLOB_BYTES or b"\0" in blob:
                continue
            for label, pattern in SIGNATURES.items():
                if pattern.search(blob):
                    findings.append(f"{revision[:12]} {path}: {label}")

    if findings:
        print("High-confidence credential signatures found:", file=sys.stderr)
        print("\n".join(sorted(set(findings))), file=sys.stderr)
        return 1
    print(f"No high-confidence credential signatures found in {len(_revisions())} revision(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
