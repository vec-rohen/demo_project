"""Driver interface between the HMI server and a PLC (or a simulation of one)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Union

from ..tags import TagDatabase

Value = Union[bool, float]


class Driver(ABC):
    """A source of live tag values that also accepts writes."""

    def __init__(self, db: TagDatabase):
        self.db = db
        self.connected = False

    async def start(self) -> None:  # pragma: no cover - default no-op
        """Open connections / start background work."""

    async def stop(self) -> None:  # pragma: no cover - default no-op
        """Release resources."""

    @abstractmethod
    async def read_all(self) -> dict[str, Value]:
        """Return the latest value of every tag the driver can read."""

    @abstractmethod
    async def write(self, name: str, value: Value) -> None:
        """Write an already-validated value to a tag."""
