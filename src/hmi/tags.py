"""Tag definitions loaded from a JSON tag database."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

KINDS = ("DI", "DO", "AI", "AO", "SP")
ROLES = ("viewer", "operator", "engineer", "integrator")
MODBUS_TABLES = ("coil", "discrete", "input", "holding")
ALARM_LIMITS = ("hihi", "hi", "lo", "lolo")
TAG_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


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
    role: str = "operator"  # minimum role allowed to write this tag
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
            "role": self.role,
            "alarm": self.alarm,
        }


@dataclass
class TagDatabase:
    plant: dict[str, Any]
    tags: dict[str, Tag] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> "TagDatabase":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_dicts(raw.get("plant", {}), raw["tags"])

    @classmethod
    def from_dicts(cls, plant: dict[str, Any], entries: list[dict[str, Any]]) -> "TagDatabase":
        db = cls(plant=plant)
        for entry in entries:
            tag = validate_tag(entry)
            if tag.name in db.tags:
                raise TagError(f"duplicate tag {tag.name}")
            db.tags[tag.name] = tag
        for tag in db.tags.values():  # alarm limits may name another tag (a setpoint)
            for key, spec in (tag.alarm or {}).items():
                if isinstance(spec, str) and key in ALARM_LIMITS and spec not in db.tags:
                    raise TagError(f"{tag.name}: alarm limit {key} refers to unknown tag {spec!r}")
        return db

    def get(self, name: str) -> Tag:
        try:
            return self.tags[name]
        except KeyError:
            raise TagError(f"unknown tag {name!r}") from None

    def analog(self) -> list[Tag]:
        return [t for t in self.tags.values() if t.type == "float"]


def validate_tag(entry: dict[str, Any]) -> Tag:
    """Build a Tag from a definition, rejecting anything the runtime couldn't use."""
    known = set(Tag.__dataclass_fields__)
    unknown = set(entry) - known
    if unknown:
        raise TagError(f"{entry.get('name', '?')}: unknown field(s) {', '.join(sorted(unknown))}")
    try:
        tag = Tag(**entry)
    except TypeError as exc:
        raise TagError(f"{entry.get('name', '?')}: {exc}") from None
    if not isinstance(tag.name, str) or not TAG_NAME_RE.match(tag.name):
        raise TagError(f"{tag.name!r}: tag names are letters, digits and _ (max 64), starting with a letter")
    if tag.kind not in KINDS:
        raise TagError(f"{tag.name}: unknown kind {tag.kind!r}")
    if tag.type not in ("bool", "float"):
        raise TagError(f"{tag.name}: unknown type {tag.type!r}")
    if tag.role not in ROLES:
        raise TagError(f"{tag.name}: unknown role {tag.role!r}")
    if tag.min is not None and tag.max is not None and tag.min >= tag.max:
        raise TagError(f"{tag.name}: min must be less than max")
    if not isinstance(tag.decimals, int) or not 0 <= tag.decimals <= 6:
        raise TagError(f"{tag.name}: decimals must be 0–6")
    if tag.modbus:
        table, address = tag.modbus.get("table"), tag.modbus.get("address")
        if table not in MODBUS_TABLES:
            raise TagError(f"{tag.name}: Modbus table must be one of {', '.join(MODBUS_TABLES)}")
        if not isinstance(address, int) or not 0 <= address <= 65535:
            raise TagError(f"{tag.name}: Modbus address must be 0–65535")
        scale = tag.modbus.get("scale", 1)
        if not isinstance(scale, (int, float)) or scale <= 0:
            raise TagError(f"{tag.name}: Modbus scale must be a positive number")
        if tag.writable and table in ("discrete", "input"):
            raise TagError(f"{tag.name}: {table} registers are read-only; use coil or holding for writable tags")
    if tag.alarm:
        if tag.type == "bool":
            if "state" not in tag.alarm:
                raise TagError(f"{tag.name}: digital alarms need a 'state' (the value that raises the alarm)")
        else:
            for key in ALARM_LIMITS:
                spec = tag.alarm.get(key)
                if spec is not None and not isinstance(spec, (int, float, str)):
                    raise TagError(f"{tag.name}: alarm limit {key} must be a number or a tag name")
    return tag
