"""Modbus TCP driver for real PLCs (Schneider, Siemens via CP, AutomationDirect, Wago…).

Each tag's ``modbus`` block in ``tags.json`` maps it to a register:

* ``table``: ``coil`` (0x), ``discrete`` (1x), ``input`` (3x) or ``holding`` (4x)
* ``address``: zero-based register/bit address
* ``scale``: (analog only) engineering value = signed 16-bit raw / scale

Only coils and holding registers can be written.
"""

from __future__ import annotations

import inspect
import logging

from pymodbus.client import AsyncModbusTcpClient

from ..tags import Tag, TagDatabase, TagError
from .base import Driver, Value

log = logging.getLogger(__name__)

# pymodbus renamed the unit/slave id argument in 3.10. Older releases are what
# Python 3.9 (Debian 11 based IoT2050 images) can install, so support both.
_UNIT_KW = "device_id" if "device_id" in inspect.signature(AsyncModbusTcpClient.read_coils).parameters else "slave"

READ_FN = {
    "coil": "read_coils",
    "discrete": "read_discrete_inputs",
    "input": "read_input_registers",
    "holding": "read_holding_registers",
}


def _to_signed(raw: int) -> int:
    return raw - 0x10000 if raw & 0x8000 else raw


class ModbusDriver(Driver):
    def __init__(self, db: TagDatabase, host: str = "127.0.0.1", port: int = 502, unit: int = 1):
        super().__init__(db)
        self.host, self.port, self.unit = host, port, unit
        self.client = AsyncModbusTcpClient(host, port=port, timeout=2)
        self.mapped: list[Tag] = [t for t in db.tags.values() if t.modbus]
        self._last: dict[str, Value] = {}
        self.errors: list[str] = []   # problems from the most recent poll, for the connection test

    async def start(self) -> None:
        self.connected = await self.client.connect()
        if not self.connected:
            log.warning("Modbus connection failed; will retry on each poll")

    async def stop(self) -> None:
        self.client.close()
        self.connected = False

    async def read_all(self) -> dict[str, Value]:
        self.errors = []
        if not self.client.connected:
            self.connected = await self.client.connect()
            if not self.connected:
                self.errors.append(f"Can't connect to {self.host}:{self.port} (no response or connection refused)")
                return dict(self._last)
        ok = True
        for table, tags in self._by_table().items():
            try:
                await self._read_table(table, tags)
            except Exception as exc:  # noqa: BLE001 - keep polling on any comms fault
                log.warning("Modbus read failed: %s", exc)
                self.errors.append(str(exc))
                ok = False
        self.connected = ok and self.client.connected
        return dict(self._last)

    def _by_table(self) -> dict[str, list[Tag]]:
        grouped: dict[str, list[Tag]] = {}
        for tag in self.mapped:
            grouped.setdefault(tag.modbus["table"], []).append(tag)
        return grouped

    async def _read_table(self, table: str, tags: list[Tag]) -> None:
        """Read one contiguous block covering every tag in a table."""
        start = min(t.modbus["address"] for t in tags)
        end = max(t.modbus["address"] for t in tags)
        fn = getattr(self.client, READ_FN[table])
        result = await fn(start, count=end - start + 1, **{_UNIT_KW: self.unit})
        if result.isError():
            names = ", ".join(t.name for t in tags)
            raise IOError(f"{table} registers {start}–{end} ({names}): PLC returned {result}")
        for tag in tags:
            offset = tag.modbus["address"] - start
            if table in ("coil", "discrete"):
                self._last[tag.name] = bool(result.bits[offset])
            else:
                raw = _to_signed(result.registers[offset])
                if tag.type == "bool":
                    self._last[tag.name] = raw != 0
                else:
                    self._last[tag.name] = raw / float(tag.modbus.get("scale", 1))

    async def write(self, name: str, value: Value) -> None:
        tag = self.db.get(name)
        if not tag.modbus:
            raise TagError(f"{name} has no Modbus address")
        table, address = tag.modbus["table"], tag.modbus["address"]
        if table == "coil":
            result = await self.client.write_coil(address, bool(value), **{_UNIT_KW: self.unit})
        elif table == "holding":
            raw = int(round(float(value) * float(tag.modbus.get("scale", 1))))
            if not -0x8000 <= raw <= 0x7FFF:
                raise TagError(f"{name}: scaled value {raw} does not fit a 16-bit register")
            result = await self.client.write_register(address, raw & 0xFFFF, **{_UNIT_KW: self.unit})
        else:
            raise TagError(f"{name}: {table} registers are read-only")
        if result.isError():
            raise IOError(f"write {name} failed: {result}")
        self._last[name] = value
