"""The site manager: runs every asset assigned to this device.

Each asset gets its own Runtime (PLC driver, alarms, trends). All assets share
one event journal. When an asset's configuration changes on site, or a new
package is loaded, only the affected runtimes are restarted; the web server
keeps running and browsers reconnect to the new runtime automatically.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from typing import Any, Callable

from .alarms import EventLog
from .drivers import Driver, driver_for_plc
from .runtime import Runtime
from .store import Store
from .tags import TagDatabase

log = logging.getLogger(__name__)

DriverFactory = Callable[[dict[str, Any], TagDatabase], Driver]


class SiteManager:
    def __init__(self, store: Store, driver_factory: DriverFactory = driver_for_plc,
                 poll_interval: float | None = None):
        self.store = store
        self.driver_factory = driver_factory
        self.poll_interval = poll_interval  # overrides each asset's poll_ms (tests)
        self.events = EventLog(store=store)
        self.runtimes: dict[str, Runtime] = {}
        self._lock_obj: asyncio.Lock | None = None
        self._maintenance: asyncio.Task | None = None

    @property
    def _lock(self) -> asyncio.Lock:
        # Created on first use, inside the running event loop (required on Python 3.9).
        if self._lock_obj is None:
            self._lock_obj = asyncio.Lock()
        return self._lock_obj

    # -- lifecycle --------------------------------------------------------------
    async def start(self) -> None:
        async with self._lock:
            # In parallel: an unreachable PLC must not hold up the other assets.
            await asyncio.gather(*(self._start_asset(a) for a in self.store.assets()))
        self._maintenance = asyncio.create_task(self._housekeeping(), name="housekeeping")

    async def stop(self) -> None:
        if self._maintenance:
            self._maintenance.cancel()
        async with self._lock:
            for asset_id in list(self.runtimes):
                await self._stop_asset(asset_id)

    async def reload_asset(self, asset_id: str) -> None:
        async with self._lock:
            await self._stop_asset(asset_id)
            asset = self.store.asset(asset_id)
            if asset:
                await self._start_asset(asset)

    async def reload_all(self) -> None:
        async with self._lock:
            for asset_id in list(self.runtimes):
                await self._stop_asset(asset_id)
            await asyncio.gather(*(self._start_asset(a) for a in self.store.assets()))

    def build_db(self, asset: dict[str, Any]) -> TagDatabase:
        plant = {"name": asset["name"], "description": asset.get("description", "")}
        return TagDatabase.from_dicts(plant, self.store.tags(asset["id"]))

    async def _start_asset(self, asset: dict[str, Any]) -> None:
        try:
            db = self.build_db(asset)
            driver = self.driver_factory(asset["plc"], db)
        except Exception as exc:  # noqa: BLE001 - one bad asset must not stop the others
            log.exception("asset %s failed to load", asset["id"])
            self.events.add("system", f"{asset['name']} could not start: {exc}", asset=asset["id"])
            return
        poll = self.poll_interval or max(0.1, asset["plc"].get("poll_ms", 500) / 1000)
        runtime = Runtime(db, driver, poll_interval=poll, asset_id=asset["id"], events=self.events, store=self.store)
        await runtime.start()
        self.runtimes[asset["id"]] = runtime

    async def _stop_asset(self, asset_id: str) -> None:
        runtime = self.runtimes.pop(asset_id, None)
        if runtime:
            await runtime.stop()

    async def _housekeeping(self) -> None:
        """Hourly: drop trend data older than the retention period (the cloud keeps the rest)."""
        while True:
            days = float(self.store.get_meta("settings", {}).get("history_days", 7))
            removed = self.store.prune_history(time.time() - days * 86400)
            self.store.prune_events()
            if removed:
                log.info("pruned %d old trend points", removed)
            await asyncio.sleep(3600)

    # -- queries ----------------------------------------------------------------
    def runtime(self, asset_id: str) -> Runtime:
        try:
            return self.runtimes[asset_id]
        except KeyError:
            raise KeyError(f"unknown or stopped asset {asset_id!r}") from None

    def info(self) -> dict[str, Any]:
        assets = []
        for asset in self.store.assets():
            rt = self.runtimes.get(asset["id"])
            alarms = rt.alarms.visible() if rt else []
            assets.append({
                "id": asset["id"], "name": asset["name"], "description": asset.get("description", ""),
                "protocol": asset["plc"]["protocol"], "running": rt is not None,
                "connected": bool(rt and rt.driver.connected),
                "alarms_active": sum(1 for a in alarms if a["active"]),
                "alarms_unacked": sum(1 for a in alarms if not a["acked"]),
            })
        return {
            "client": self.store.get_meta("client"), "site": self.store.get_meta("site"),
            "device": self.store.get_meta("device"), "package": self.store.get_meta("package"),
            "settings": self.store.get_meta("settings", {}),
            "assets": assets,
            "pending_changes": len(self.store.changes(pending_only=True)),
            "configured": bool(assets),
            "hostname": socket.gethostname(),
        }

    async def test_connection(self, asset_id: str, plc: dict[str, Any], timeout: float = 10.0) -> dict[str, Any]:
        """Open a separate connection with the given settings and read every tag once."""
        asset = self.store.asset(asset_id)
        if asset is None:
            raise KeyError(asset_id)
        db = self.build_db(asset)
        result: dict[str, Any] = {"ok": False, "errors": [], "tags_total": len(db.tags), "tags_read": 0,
                                  "latency_ms": None}
        driver = None
        started = time.perf_counter()
        try:
            driver = self.driver_factory(plc, db)
            await asyncio.wait_for(driver.start(), timeout)
            t0 = time.perf_counter()
            values = await asyncio.wait_for(driver.read_all(), timeout)
            result["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            result["tags_read"] = sum(1 for name in db.tags if name in values)
            result["errors"] = list(getattr(driver, "errors", []))
            result["ok"] = driver.connected and not result["errors"]
        except asyncio.TimeoutError:
            result["errors"].append(f"No answer within {timeout:.0f} s")
        except Exception as exc:  # noqa: BLE001 - report anything to the technician
            result["errors"].append(str(exc) or type(exc).__name__)
        finally:
            if driver is not None:
                try:
                    await driver.stop()
                except Exception:  # noqa: BLE001
                    pass
        result["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
        return result
