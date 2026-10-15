"""Tag definitions loaded from a JSON tag database."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

KINDS = ("DI", "DO", "AI", "AO", "SP")


class TagError(ValueError):
    """Raised when a tag lookup or write is invalid."""


@dataclass
class Tag:
    name: str
    kind: str
    type: str
    description: str = ""
    unit: str = ""
    min: float | None = None
    max: float | None = None
    decimals: int = 1
    writable: bool = False
    manual_only: bool = False
    alarm: dict[str, Any] | None = None
    modbus: dict[str, Any] | None = None

    def coerce(self, value: Any) -> bool | float:
        """Validate and convert an incoming write value for this tag."""
        if self.type == "bool":
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)) and value in (0, 1):
                return bool(value)
            if isinstance(value, str) and value.lower() in ("true", "false", "1", "0", "on", "off"):
                return value.lower() in ("true", "1", "on")
            raise TagError(f"{self.name}: expected a boolean, got {value!r}")
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise TagError(f"{self.name}: expected a number, got {value!r}") from None
        if number != number:  # NaN
            raise TagError(f"{self.name}: value must be a number")
        if self.min is not None and number < self.min:
            raise TagError(f"{self.name}: {number} is below minimum {self.min}")
        if self.max is not None and number > self.max:
            raise TagError(f"{self.name}: {number} is above maximum {self.max}")
        return number

    def public(self) -> dict[str, Any]:
        """Tag metadata sent to the browser (driver addressing omitted)."""
        return {
            "name": self.name,
            "kind": self.kind,
            "type": self.type,
            "description": self.description,
            "unit": self.unit,
            "min": self.min,
            "max": self.max,
            "decimals": self.decimals,
            "writable": self.writable,
            "manual_only": self.manual_only,
            "alarm": self.alarm,
        }


@dataclass
class TagDatabase:
    plant: dict[str, Any]
    tags: dict[str, Tag] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> "TagDatabase":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        db = cls(plant=raw.get("plant", {}))
        for entry in raw["tags"]:
            tag = Tag(**entry)
            if tag.kind not in KINDS:
                raise TagError(f"{tag.name}: unknown kind {tag.kind!r}")
            if tag.type not in ("bool", "float"):
                raise TagError(f"{tag.name}: unknown type {tag.type!r}")
            if tag.name in db.tags:
                raise TagError(f"duplicate tag {tag.name}")
            db.tags[tag.name] = tag
        return db

    def get(self, name: str) -> Tag:
        try:
            return self.tags[name]
        except KeyError:
            raise TagError(f"unknown tag {name!r}") from None

    def analog(self) -> list[Tag]:
        return [t for t in self.tags.values() if t.type == "float"]
