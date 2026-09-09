"""Content-addressed local object store.

Uploaded filenames never reach the filesystem. The key is derived purely from
the SHA-256 of the content, which removes path traversal, unicode
normalisation and case-collision problems as a class rather than filtering for
them, and makes storage naturally deduplicating.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path

from iep.storage.base import ObjectNotFoundError

_KEY = re.compile(r"^[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{64}$")


def content_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def key_for(content_sha256: str) -> str:
    digest = content_sha256.lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("content_sha256 must be 64 lowercase hex characters")
    # Two levels of fan-out keep directory listings usable once an install has
    # accumulated a few hundred thousand objects.
    return f"{digest[:2]}/{digest[2:4]}/{digest}"


class LocalObjectStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        if not _KEY.match(key):
            raise ValueError(f"malformed storage key: {key!r}")
        path = (self.root / key).resolve()
        # Belt and braces: the key regex already forbids traversal, but the
        # containment check means a future key format cannot reintroduce it.
        if not path.is_relative_to(self.root.resolve()):
            raise ValueError("storage key escapes the store root")
        return path

    def put(self, content_sha256: str, data: bytes) -> str:
        key = key_for(content_sha256)
        path = self._path(key)
        if path.exists():
            return key
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temporary file in the same directory and rename, so a
        # crash mid-write cannot leave a truncated object under a valid digest.
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, suffix=".part")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            tmp.replace(path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return key

    def get(self, key: str) -> bytes:
        path = self._path(key)
        if not path.exists():
            raise ObjectNotFoundError(key)
        return path.read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def size(self, key: str) -> int:
        path = self._path(key)
        if not path.exists():
            raise ObjectNotFoundError(key)
        return path.stat().st_size
