"""Injectable time - PRINCIPAL-OWNED.

All timestamps are UTC. Domain and service code must take a Clock, never call
``datetime.now`` directly: reproducible scenario snapshots and golden report tests
are impossible without frozen time.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FrozenClock:
    """Test/demo clock returning a fixed instant until advanced."""

    def __init__(self, instant: datetime) -> None:
        if instant.tzinfo is None:
            raise ValueError("FrozenClock requires an aware (UTC) datetime")
        self._instant = instant

    def now(self) -> datetime:
        return self._instant

    def advance_to(self, instant: datetime) -> None:
        if instant.tzinfo is None:
            raise ValueError("FrozenClock requires an aware (UTC) datetime")
        self._instant = instant
