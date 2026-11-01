"""Runs one asset: polls its PLC driver, evaluates alarms, records trend history
and fans live updates out to browsers.

A device runs one Runtime per asset (see ``site.py``). Trend data is kept in
memory for the last 15 minutes at 1 s resolution, and written to the device
database on change (with a deadband) plus a heartbeat, for longer trends.
"""

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


HISTORY_HEARTBEAT_S = 60.0   # store a point at least this often, even if unchanged
HISTORY_DEADBAND = 0.005     # ...or when it moves more than 0.5 % of its span
HISTORY_FLUSH_S = 5.0
COMMS_ALARM = "comms"          # alarm id for "PLC communication lost"
COMMS_ALARM_DELAY_S = 10.0     # ride through short blips (e.g. a PLC restart) before alarming


class Runtime:
    def __init__(
        self,
        db: TagDatabase,
        driver: Driver,
        poll_interval: float = 0.25,
        history_interval: float = 1.0,
        history_points: int = 900,
        asset_id: str = "default",
        events: EventLog | None = None,
        store=None,
        mode_tag: str | None = None,
    ):
        self.db = db
        self.driver = driver
        self.asset_id = asset_id
        self.store = store
        self.mode_tag = mode_tag  # digital tag that is ON while the PLC runs the asset in Auto
        self.poll_interval = poll_interval
        self.history_interval = history_interval
        self.events = events if events is not None else EventLog()
        self.alarms = AlarmManager(db, self.events, asset_id)
        self.values: dict[str, Value] = {}
        self.updated_at: float = 0.0
        self.history: dict[str, deque[tuple[float, float]]] = {
            t.name: deque(maxlen=history_points) for t in db.analog()
        }
        self._last_history = 0.0
        self.started_at = time.time()
        self.last_good: float | None = None   # last poll with fresh values from the PLC
        self._stored: dict[str, tuple[float, float]] = {}   # tag -> (t, v) last written to the store
        self._pending_rows: list[tuple[str, str, float, float]] = []
        self._last_flush = time.time()
        self._subscribers: set[asyncio.Queue] = set()
        self._task: asyncio.Task | None = None
        self._last_event_seq = 0

    async def start(self) -> None:
        self._seed_history()
        await self.driver.start()
        self._event("system", f"{self.db.plant.get('name', self.asset_id)} started ({type(self.driver).__name__})")
        await self.poll_once()
        self._task = asyncio.create_task(self._run(), name=f"poll-{self.asset_id}")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self.driver.stop()
        self._flush_history(force=True)

    def _event(self, kind: str, message: str, **extra: Any) -> dict[str, Any]:
        return self.events.add(kind, message, asset=self.asset_id, **extra)

    def _seed_history(self) -> None:
        """After a restart or reload, fill the live trend buffers from the database."""
        if self.store is None:
            return
        for name, buf in self.history.items():
            since = time.time() - self.history_interval * buf.maxlen
            buf.extend(self.store.history(self.asset_id, name, since, max_points=buf.maxlen))

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
        connected = self.driver.connected
        if was_connected != connected:
            self._event("system", "PLC connection restored" if connected else "PLC connection lost")
        if connected:
            self.last_good = now
            # Only judge alarms and record trends on fresh values; while the PLC is
            # unreachable the last values are frozen and would mislead.
            self.alarms.evaluate(self.values, now)
        offline_for = now - (self.last_good or self.started_at)
        self.alarms.set_system(COMMS_ALARM, not connected and offline_for >= COMMS_ALARM_DELAY_S,
                               f"{self.db.plant.get('name', self.asset_id)}: PLC communication lost", now=now)
        if connected and now - self._last_history >= self.history_interval:
            self._last_history = now
            for name, buf in self.history.items():
                if name in self.values:
                    value = float(self.values[name])
                    buf.append((round(now, 3), value))
                    self._maybe_store(name, now, value)
            self._flush_history()
        self._broadcast(self.snapshot(include_events_since=self._last_event_seq))

    def _maybe_store(self, name: str, now: float, value: float) -> None:
        if self.store is None:
            return
        last = self._stored.get(name)
        tag = self.db.tags[name]
        span = (tag.max - tag.min) if tag.min is not None and tag.max is not None else 0.0
        if last is None or now - last[0] >= HISTORY_HEARTBEAT_S or abs(value - last[1]) > span * HISTORY_DEADBAND:
            self._stored[name] = (now, value)
            self._pending_rows.append((self.asset_id, name, round(now, 3), value))

    def _flush_history(self, force: bool = False) -> None:
        if self.store is None or not self._pending_rows:
            return
        if force or time.time() - self._last_flush >= HISTORY_FLUSH_S:
            rows, self._pending_rows = self._pending_rows, []
            self._last_flush = time.time()
            self.store.add_history(rows)

    def trend(self, name: str, seconds: float) -> list[tuple[float, float]]:
        """Trend points for the last ``seconds``: memory for short windows, database for long ones."""
        buf = self.history[name]
        cutoff = time.time() - seconds
        if self.store is None or (buf and buf[0][0] <= cutoff):
            return [p for p in buf if p[0] >= cutoff]
        self._flush_history(force=True)
        return self.store.history(self.asset_id, name, cutoff)

    # -- browser-facing state -------------------------------------------------
    def snapshot(self, include_events_since: int | None = None) -> dict[str, Any]:
        events = self.events.all() if include_events_since is None else self.events.since(include_events_since)
        if events:
            self._last_event_seq = max(self._last_event_seq, events[-1]["seq"])
        return {
            "type": "update",
            "asset": self.asset_id,
            "t": self.updated_at,
            "connected": self.driver.connected,
            "last_good": self.last_good,
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
        if tag.manual_only and self.mode_tag and self.values.get(self.mode_tag):
            raise WriteRejected(f"{name} is controlled by the PLC in automatic mode; switch to manual first")
        value = tag.coerce(raw_value)
        old = self.values.get(name)
        await self.driver.write(name, value)
        self.values[name] = value
        self._event("write", f"{tag.description}: {self._fmt(tag, old)} → {self._fmt(tag, value)}",
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
        self._event("sim", f"Simulated {tag.description} = {self._fmt(tag, value)}", tag=name, user=user)
        return value

    @staticmethod
    def _fmt(tag, value: Any) -> str:
        if value is None:
            return "—"
        if tag.type == "bool":
            return "ON" if value else "OFF"
        return f"{float(value):.{tag.decimals}f}{(' ' + tag.unit) if tag.unit else ''}"
