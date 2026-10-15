"""Polls the driver, runs alarms, keeps trend history and fans updates out to browsers."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any

from .alarms import AlarmManager, EventLog
from .drivers import Driver, Value
from .tags import TagDatabase, TagError

log = logging.getLogger(__name__)


class WriteRejected(Exception):
    """A write was well-formed but is not allowed in the current plant state."""


class Runtime:
    def __init__(
        self,
        db: TagDatabase,
        driver: Driver,
        poll_interval: float = 0.25,
        history_interval: float = 1.0,
        history_points: int = 900,
    ):
        self.db = db
        self.driver = driver
        self.poll_interval = poll_interval
        self.history_interval = history_interval
        self.events = EventLog()
        self.alarms = AlarmManager(db, self.events)
        self.values: dict[str, Value] = {}
        self.updated_at: float = 0.0
        self.history: dict[str, deque[tuple[float, float]]] = {
            t.name: deque(maxlen=history_points) for t in db.analog()
        }
        self._last_history = 0.0
        self._subscribers: set[asyncio.Queue] = set()
        self._task: asyncio.Task | None = None
        self._last_event_seq = 0

    async def start(self) -> None:
        await self.driver.start()
        self.events.add("system", f"HMI started ({type(self.driver).__name__})")
        await self.poll_once()
        self._task = asyncio.create_task(self._run(), name="hmi-poll")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self.driver.stop()

    async def _run(self) -> None:
        while True:
            try:
                await self.poll_once()
            except Exception:  # noqa: BLE001 - a bad scan must not kill the HMI
                log.exception("poll failed")
            await asyncio.sleep(self.poll_interval)

    async def poll_once(self) -> None:
        was_connected = self.driver.connected
        self.values = await self.driver.read_all()
        now = time.time()
        self.updated_at = now
        if was_connected != self.driver.connected:
            self.events.add("system", "PLC connection restored" if self.driver.connected
                            else "PLC connection lost")
        self.alarms.evaluate(self.values, now)
        if now - self._last_history >= self.history_interval:
            self._last_history = now
            for name, buf in self.history.items():
                if name in self.values:
                    buf.append((round(now, 3), float(self.values[name])))
        self._broadcast(self.snapshot(include_events_since=self._last_event_seq))

    # -- browser-facing state -------------------------------------------------
    def snapshot(self, include_events_since: int | None = None) -> dict[str, Any]:
        events = self.events.all() if include_events_since is None else self.events.since(include_events_since)
        if events:
            self._last_event_seq = max(self._last_event_seq, events[-1]["seq"])
        return {
            "type": "update",
            "t": self.updated_at,
            "connected": self.driver.connected,
            "values": self.values,
            "alarms": self.alarms.visible(),
            "events": events,
        }

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=8)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def _broadcast(self, message: dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            if queue.full():  # slow client: drop its oldest frame rather than block the scan
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(message)

    # -- operator actions -----------------------------------------------------
    async def write(self, name: str, raw_value: Any, user: str) -> Value:
        tag = self.db.get(name)
        if not tag.writable:
            raise WriteRejected(f"{name} is read-only")
        if tag.manual_only and self.values.get("SP_AutoMode"):
            raise WriteRejected(f"{name} is controlled by the PLC in automatic mode; switch to manual first")
        value = tag.coerce(raw_value)
        old = self.values.get(name)
        await self.driver.write(name, value)
        self.values[name] = value
        self.events.add("write", f"{tag.description}: {self._fmt(tag, old)} → {self._fmt(tag, value)}",
                        tag=name, old=old, new=value, user=user)
        return value

    async def simulate_input(self, name: str, raw_value: Any, user: str) -> Value:
        if not getattr(self.driver, "supports_simulated_inputs", False):
            raise WriteRejected("input simulation is only available with the simulator driver")
        tag = self.db.get(name)
        if tag.kind != "DI":
            raise TagError(f"{name} is not a digital input")
        value = tag.coerce(raw_value)
        await self.driver.simulate_input(name, value)
        self.values[name] = value
        self.events.add("sim", f"Simulated {tag.description} = {self._fmt(tag, value)}", tag=name, user=user)
        return value

    @staticmethod
    def _fmt(tag, value: Any) -> str:
        if value is None:
            return "—"
        if tag.type == "bool":
            return "ON" if value else "OFF"
        return f"{float(value):.{tag.decimals}f}{(' ' + tag.unit) if tag.unit else ''}"
