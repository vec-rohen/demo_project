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
    """Journal of alarms, operator actions and system events.

    With a ``store`` the journal is persisted in the device database and
    survives restarts; the most recent entries are cached in memory.
    """

    def __init__(self, size: int = 500, store=None):
        self.store = store
        self._events: deque[dict[str, Any]] = deque(maxlen=size)
        self._seq = itertools.count(1)
        if store is not None:
            self._events.extend(store.recent_events(size))

    def add(self, kind: str, message: str, **extra: Any) -> dict[str, Any]:
        extra = {k: v for k, v in extra.items() if v is not None}
        t = time.time()
        seq = self.store.add_event(t, kind, message, extra) if self.store is not None else next(self._seq)
        event = {"seq": seq, "t": t, "kind": kind, "message": message, **extra}
        self._events.append(event)
        return event

    def since(self, seq: int) -> list[dict[str, Any]]:
        return [e for e in self._events if e["seq"] > seq]

    def all(self) -> list[dict[str, Any]]:
        return list(self._events)


class AlarmManager:
    def __init__(self, db: TagDatabase, events: EventLog, asset_id: str | None = None):
        self.db = db
        self.events = events
        self.asset_id = asset_id
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
            if alarm.condition == "system":
                continue  # raised by the runtime itself (e.g. PLC communication lost)
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
                self.events.add("alarm", alarm.message, alarm=alarm.id, asset=self.asset_id, priority=alarm.priority,
                                tag=alarm.tag, value=value)
            elif not should_be_active and alarm.active:
                alarm.active, alarm.cleared_at = False, now
                self.events.add("clear", f"{alarm.message} returned to normal", alarm=alarm.id, asset=self.asset_id,
                                tag=alarm.tag, value=value)

    def set_system(self, alarm_id: str, active: bool, message: str, priority: str = "high",
                   now: float | None = None) -> None:
        """Raise or clear an alarm that isn't tied to a tag value."""
        now = time.time() if now is None else now
        alarm = self.alarms.get(alarm_id)
        if alarm is None:
            if not active:
                return
            alarm = self.alarms[alarm_id] = Alarm(alarm_id, "", "system", priority, message)
        if active and not alarm.active:
            alarm.active, alarm.acked, alarm.raised_at, alarm.cleared_at = True, False, now, None
            self.events.add("alarm", message, alarm=alarm_id, priority=priority, asset=self.asset_id)
        elif not active and alarm.active:
            alarm.active, alarm.cleared_at = False, now
            self.events.add("clear", f"{message} – returned to normal", alarm=alarm_id, asset=self.asset_id)

    def acknowledge(self, alarm_id: str | None, user: str) -> int:
        """Acknowledge one alarm, or every unacknowledged alarm when ``alarm_id`` is None."""
        targets = [self.alarms[alarm_id]] if alarm_id else list(self.alarms.values())
        count = 0
        for alarm in targets:
            if not alarm.acked:
                alarm.acked = True
                count += 1
                self.events.add("ack", f"{alarm.message} acknowledged", alarm=alarm.id,
                                asset=self.asset_id, user=user)
        return count

    def visible(self) -> list[dict[str, Any]]:
        """Alarms the operator still needs to see: active or not yet acknowledged."""
        order = {"high": 0, "medium": 1, "low": 2}
        shown = [a for a in self.alarms.values() if a.active or not a.acked]
        shown.sort(key=lambda a: (order.get(a.priority, 3), -(a.raised_at or 0)))
        return [asdict(a) for a in shown]
