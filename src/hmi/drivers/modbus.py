"""Modbus TCP driver for real PLCs (Schneider, Siemens via CP, AutomationDirect, Wago…).

Each tag's ``modbus`` block in ``tags.json`` maps it to a register:

* ``table``: ``coil`` (0x), ``discrete`` (1x), ``input`` (3x) or ``holding`` (4x)
* ``address``: zero-based register/bit address
* ``scale``: (analog only) engineering value = signed 16-bit raw / scale

Only coils and holding registers can be written.
"""

from __future__ import annotations

import logging

from pymodbus.client import AsyncModbusTcpClient

from ..tags import Tag, TagDatabase, TagError
from .base import Driver, Value

log = logging.getLogger(__name__)

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
        self.unit = unit
        self.client = AsyncModbusTcpClient(host, port=port, timeout=2)
        self.mapped: list[Tag] = [t for t in db.tags.values() if t.modbus]
        self._last: dict[str, Value] = {}

    async def start(self) -> None:
        self.connected = await self.client.connect()
        if not self.connected:
            log.warning("Modbus connection failed; will retry on each poll")

    async def stop(self) -> None:
        self.client.close()
        self.connected = False

    async def read_all(self) -> dict[str, Value]:
        if not self.client.connected:
            self.connected = await self.client.connect()
            if not self.connected:
                return dict(self._last)
        try:
            for table, tags in self._by_table().items():
                await self._read_table(table, tags)
            self.connected = True
        except Exception as exc:  # noqa: BLE001 - keep polling on any comms fault
            log.warning("Modbus read failed: %s", exc)
            self.connected = False
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
        result = await fn(start, count=end - start + 1, device_id=self.unit)
        if result.isError():
            raise IOError(f"{table} read {start}..{end}: {result}")
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
            result = await self.client.write_coil(address, bool(value), device_id=self.unit)
        elif table == "holding":
            raw = int(round(float(value) * float(tag.modbus.get("scale", 1))))
            if not -0x8000 <= raw <= 0x7FFF:
                raise TagError(f"{name}: scaled value {raw} does not fit a 16-bit register")
            result = await self.client.write_register(address, raw & 0xFFFF, device_id=self.unit)
        else:
            raise TagError(f"{name}: {table} registers are read-only")
        if result.isError():
            raise IOError(f"write {name} failed: {result}")
        self._last[name] = value
