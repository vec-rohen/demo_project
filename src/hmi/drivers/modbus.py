"""Modbus TCP driver for real PLCs (Schneider, Siemens via CP, AutomationDirect, Wago…).

Each tag's ``modbus`` block in ``tags.json`` maps it to a register:

* ``table``: ``coil`` (0x), ``discrete`` (1x), ``input`` (3x) or ``holding`` (4x)
* ``address``: zero-based register/bit address
* ``type``: register data type – int16 (default), uint16, int32, uint32, float32
* ``word_order``: big (default, high word first) or little, for 32-bit types
* ``bit``: 0–15, for a digital tag that is one bit of an input/holding register
* ``scale``: (analog only) engineering value = raw value / scale

Only coils and holding registers can be written. Reads are batched to stay
within the protocol's 125-register / 2000-bit request limits.
"""

from __future__ import annotations

import inspect
import logging

from pymodbus.client import AsyncModbusTcpClient

from ..tags import Tag, TagDatabase, TagError
from .base import Driver, Value
from .codec import batches, decode, encode, register_count

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


MAX_REGISTERS = 120   # protocol limit is 125 per request; keep a margin
MAX_BITS = 1900       # protocol limit is 2000


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

    @staticmethod
    def _width(tag: Tag) -> int:
        """How many registers (or bits) the tag occupies."""
        if tag.modbus["table"] in ("coil", "discrete") or tag.type == "bool":
            return 1
        return register_count(tag.modbus.get("type", "int16"))

    async def _read_table(self, table: str, tags: list[Tag]) -> None:
        """Read a table in as few requests as the protocol allows (125 registers / 2000 bits each)."""
        limit = MAX_BITS if table in ("coil", "discrete") else MAX_REGISTERS
        spans = [(t.modbus["address"], self._width(t)) for t in tags]
        fn = getattr(self.client, READ_FN[table])
        for start, count in batches(spans, limit):
            result = await fn(start, count=count, **{_UNIT_KW: self.unit})
            inside = [t for t in tags if start <= t.modbus["address"] < start + count]
            if result.isError():
                names = ", ".join(t.name for t in inside)
                raise IOError(f"{table} {start}–{start + count - 1} ({names}): PLC returned {result}")
            for tag in inside:
                self._last[tag.name] = self._decode(tag, result, tag.modbus["address"] - start)

    @staticmethod
    def _decode(tag: Tag, result, offset: int) -> Value:
        m = tag.modbus
        if m["table"] in ("coil", "discrete"):
            return bool(result.bits[offset])
        if tag.type == "bool":
            word = result.registers[offset]
            return bool(word >> m["bit"] & 1) if "bit" in m else word != 0
        width = register_count(m.get("type", "int16"))
        raw = decode(result.registers[offset:offset + width], m.get("type", "int16"), m.get("word_order", "big"))
        return raw / float(m.get("scale", 1))

    async def write(self, name: str, value: Value) -> None:
        tag = self.db.get(name)
        if not tag.modbus:
            raise TagError(f"{name} has no Modbus address")
        m = tag.modbus
        table, address = m["table"], m["address"]
        unit = {_UNIT_KW: self.unit}
        if table == "coil":
            result = await self.client.write_coil(address, bool(value), **unit)
        elif table == "holding" and tag.type == "bool" and "bit" in m:
            # One bit of a status/command word: read, change that bit, write back.
            current = await self.client.read_holding_registers(address, count=1, **unit)
            if current.isError():
                raise IOError(f"write {name} failed reading the register first: {current}")
            word = current.registers[0]
            word = word | (1 << m["bit"]) if value else word & ~(1 << m["bit"])
            result = await self.client.write_register(address, word & 0xFFFF, **unit)
        elif table == "holding":
            dtype = m.get("type", "int16")
            raw = (1 if value else 0) if tag.type == "bool" else float(value) * float(m.get("scale", 1))
            try:
                words = encode(raw, dtype, m.get("word_order", "big"))
            except ValueError as exc:
                raise TagError(f"{name}: {exc}") from None
            if len(words) == 1:
                result = await self.client.write_register(address, words[0], **unit)
            else:
                result = await self.client.write_registers(address, words, **unit)
        else:
            raise TagError(f"{name}: {table} registers are read-only")
        if result.isError():
            raise IOError(f"write {name} failed: {result}")
        self._last[name] = value
