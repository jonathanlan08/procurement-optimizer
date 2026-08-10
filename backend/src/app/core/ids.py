"""Injectable id generation — PRINCIPAL-OWNED.

UUIDv4 in production; a deterministic sequence in tests so snapshots and golden
files are stable.
"""

from __future__ import annotations

import uuid
from typing import Protocol


class IdGenerator(Protocol):
    def new_id(self) -> uuid.UUID: ...


class RandomIdGenerator:
    def new_id(self) -> uuid.UUID:
        return uuid.uuid4()


class SequentialIdGenerator:
    """Deterministic ids for tests: 00000000-0000-4000-8000-000000000001, ..."""

    def __init__(self, start: int = 1) -> None:
        self._counter = start

    def new_id(self) -> uuid.UUID:
        value = uuid.UUID(f"00000000-0000-4000-8000-{self._counter:012d}")
        self._counter += 1
        return value
