"""Alarm evaluation and an operator event journal (ISA-18.2 style lifecycle).

An alarm stays in the active list until it has both returned to normal *and*
been acknowledged, so a transient trip is never missed by the operator.
"""

from __future__ import annotations

import itertools
import time
from collections import deque
from dataclasses import asdict, dataclass
from typing import Any

from .tags import Tag, TagDatabase

ANALOG_LIMITS = {
    # condition: (comparison direction, default priority, label)
    "hihi": (1, "high", "High-high"),
    "hi": (1, "medium", "High"),
    "lo": (-1, "medium", "Low"),
    "lolo": (-1, "high", "Low-low"),
}


@dataclass
class Alarm:
    id: str
    tag: str
    condition: str
    priority: str
    message: str
    active: bool = False
    acked: bool = True
    raised_at: float | None = None
    cleared_at: float | None = None
    value: Any = None
    limit: float | None = None


class EventLog:
    def __init__(self, size: int = 500):
        self._events: deque[dict[str, Any]] = deque(maxlen=size)
        self._seq = itertools.count(1)

    def add(self, kind: str, message: str, **extra: Any) -> dict[str, Any]:
        event = {"seq": next(self._seq), "t": time.time(), "kind": kind, "message": message, **extra}
        self._events.append(event)
        return event

    def since(self, seq: int) -> list[dict[str, Any]]:
        return [e for e in self._events if e["seq"] > seq]

    def all(self) -> list[dict[str, Any]]:
        return list(self._events)


class AlarmManager:
    def __init__(self, db: TagDatabase, events: EventLog):
        self.db = db
        self.events = events
        self.alarms: dict[str, Alarm] = {}
        for tag in db.tags.values():
            if not tag.alarm:
                continue
            if tag.type == "bool":
                self._add(tag, "state", tag.alarm.get("priority", "medium"),
                          tag.alarm.get("message", f"{tag.description} alarm"))
            else:
                for cond, (_, priority, label) in ANALOG_LIMITS.items():
                    if cond in tag.alarm:
                        self._add(tag, cond, priority, f"{tag.description} {label.lower()}")

    def _add(self, tag: Tag, condition: str, priority: str, message: str) -> None:
        alarm_id = f"{tag.name}.{condition}"
        self.alarms[alarm_id] = Alarm(alarm_id, tag.name, condition, priority, message)

    def _limit(self, spec: Any, values: dict[str, Any]) -> float | None:
        """A limit is either a number or the name of a setpoint tag holding it."""
        if isinstance(spec, str):
            value = values.get(spec)
            return float(value) if value is not None else None
        return float(spec)

    def evaluate(self, values: dict[str, Any], now: float | None = None) -> None:
        now = time.time() if now is None else now
        for alarm in self.alarms.values():
            tag = self.db.tags[alarm.tag]
            value = values.get(alarm.tag)
            if value is None:
                continue
            if alarm.condition == "state":
                should_be_active = bool(value) == bool(tag.alarm["state"])
            else:
                limit = self._limit(tag.alarm[alarm.condition], values)
                if limit is None:
                    continue
                alarm.limit = limit
                direction = ANALOG_LIMITS[alarm.condition][0]
                deadband = float(tag.alarm.get("deadband", 0.0))
                if alarm.active:  # hold until clear of the deadband
                    should_be_active = (value - (limit - direction * deadband)) * direction > 0
                else:
                    should_be_active = (value - limit) * direction >= 0
            alarm.value = value
            if should_be_active and not alarm.active:
                alarm.active, alarm.acked = True, False
                alarm.raised_at, alarm.cleared_at = now, None
                self.events.add("alarm", alarm.message, alarm=alarm.id, priority=alarm.priority,
                                tag=alarm.tag, value=value)
            elif not should_be_active and alarm.active:
                alarm.active, alarm.cleared_at = False, now
                self.events.add("clear", f"{alarm.message} returned to normal", alarm=alarm.id,
                                tag=alarm.tag, value=value)

    def acknowledge(self, alarm_id: str | None, user: str) -> int:
        """Acknowledge one alarm, or every unacknowledged alarm when ``alarm_id`` is None."""
        targets = [self.alarms[alarm_id]] if alarm_id else list(self.alarms.values())
        count = 0
        for alarm in targets:
            if not alarm.acked:
                alarm.acked = True
                count += 1
                self.events.add("ack", f"{alarm.message} acknowledged", alarm=alarm.id, user=user)
        return count

    def visible(self) -> list[dict[str, Any]]:
        """Alarms the operator still needs to see: active or not yet acknowledged."""
        order = {"high": 0, "medium": 1, "low": 2}
        shown = [a for a in self.alarms.values() if a.active or not a.acked]
        shown.sort(key=lambda a: (order.get(a.priority, 3), -(a.raised_at or 0)))
        return [asdict(a) for a in shown]
