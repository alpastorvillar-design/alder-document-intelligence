"""Object store interface.

Only two operations are needed — put bytes, get bytes back — and keys are
derived from content, never from anything a caller supplied. Keeping the
surface this small is what makes swapping the local backend for S3-compatible
storage a contained change rather than an audit of every call site.
"""

from __future__ import annotations

from typing import Protocol


class ObjectStore(Protocol):
    def put(self, content_sha256: str, data: bytes) -> str:
        """Store `data` and return its storage key. Idempotent by digest."""
        ...

    def get(self, key: str) -> bytes: ...

    def exists(self, key: str) -> bool: ...

    def size(self, key: str) -> int: ...


class ObjectNotFoundError(Exception):
    pass
