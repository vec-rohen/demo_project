"""Siemens S7 driver (S7-1200, S7-1500, S7-300/400, ET 200SP CPUs) using snap7.

Each tag's ``s7`` block gives a TIA-style address and data type::

    {"address": "%DB10.DBD4", "type": "real", "scale": 1}

PLC settings needed in TIA Portal (S7-1200/1500):
  * CPU properties → Protection & Security → Connection mechanisms:
    tick "Permit access with PUT/GET communication from remote partner".
  * Data blocks read by the HMI: untick "Optimized block access" (DB properties
    → Attributes) so variables have fixed byte offsets, then compile & download.
Rack/slot: S7-1200/1500 = 0/1 (or 0/0), S7-300 = 0/2.

snap7 calls block, so they run on a worker thread; one connection is shared and
calls are serialised. Bits are written with read-modify-write of their byte.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

from ..tags import Tag, TagDatabase, TagError
from .base import Driver, Value
from .codec import batches
from .s7addr import S7Address, decode, default_type, encode, parse

log = logging.getLogger(__name__)
MAX_BYTES = 200  # stays inside the smallest S7 PDU (240 bytes) after headers


def _snap7():
    import snap7  # imported lazily: only needed when an asset uses S7
    from snap7.type import Area

    return snap7, Area


class S7Driver(Driver):
    def __init__(self, db: TagDatabase, host: str, rack: int = 0, slot: int = 1, port: int = 102):
        super().__init__(db)
        self.host, self.rack, self.slot, self.port = host, rack, slot, port
        snap7, self._Area = _snap7()
        self.client = snap7.client.Client()
        self._lock = threading.Lock()
        self.mapped: list[tuple[Tag, S7Address, str]] = []
        for tag in db.tags.values():
            if tag.s7:
                addr = parse(tag.s7["address"])
                self.mapped.append((tag, addr, tag.s7.get("type") or default_type(addr)))
        self._last: dict[str, Value] = {}
        self.errors: list[str] = []

    # -- connection --------------------------------------------------------------
    def _connect(self) -> bool:
        try:
            if not self.client.get_connected():
                self.client.connect(self.host, self.rack, self.slot, self.port)
            return bool(self.client.get_connected())
        except Exception as exc:  # noqa: BLE001
            self.errors.append(f"Can't connect to S7 PLC {self.host} (rack {self.rack}, slot {self.slot}): {exc}")
            return False

    def _disconnect(self) -> None:
        try:
            self.client.disconnect()
        except Exception:  # noqa: BLE001
            pass

    async def start(self) -> None:
        self.connected = await asyncio.to_thread(self._locked, self._connect)

    async def stop(self) -> None:
        await asyncio.to_thread(self._locked, self._disconnect)
        self.connected = False

    def _locked(self, fn, *args: Any):
        with self._lock:
            return fn(*args)

    # -- reading -----------------------------------------------------------------
    async def read_all(self) -> dict[str, Value]:
        self.errors = []
        self.connected = await asyncio.to_thread(self._locked, self._read_blocking)
        return dict(self._last)

    def _read_blocking(self) -> bool:
        if not self._connect():
            return False
        groups: dict[tuple[str, int], list[tuple[Tag, S7Address, str]]] = {}
        for item in self.mapped:
            groups.setdefault((item[1].area, item[1].db), []).append(item)
        ok = True
        for (area, dbn), items in groups.items():
            spans = [(a.byte, a.nbytes) for _, a, _ in items]
            for start, size in batches(spans, MAX_BYTES):
                try:
                    buf = self.client.read_area(getattr(self._Area, area), dbn, start, size)
                except Exception as exc:  # noqa: BLE001 - report and carry on with other areas
                    where = f"DB{dbn}" if area == "DB" else {"PE": "inputs", "PA": "outputs", "MK": "merkers"}[area]
                    self.errors.append(f"Reading {where} bytes {start}–{start + size - 1}: {exc}")
                    ok = False
                    if not self.client.get_connected():
                        return False
                    continue
                for tag, addr, dtype in items:
                    if start <= addr.byte < start + size:
                        raw = decode(buf, addr.byte - start, addr, dtype)
                        self._last[tag.name] = raw if tag.type == "bool" and addr.size == "X" else (
                            bool(raw) if tag.type == "bool" else raw / float(tag.s7.get("scale", 1)))
        if not ok and not self.client.get_connected():
            self._disconnect()
        return ok

    # -- writing -----------------------------------------------------------------
    async def write(self, name: str, value: Value) -> None:
        tag = self.db.get(name)
        item = next((i for i in self.mapped if i[0].name == name), None)
        if item is None:
            raise TagError(f"{name} has no S7 address")
        _, addr, dtype = item
        if addr.area == "PE":
            raise TagError(f"{name}: inputs (%I) can't be written")
        await asyncio.to_thread(self._locked, self._write_blocking, tag, addr, dtype, value)
        self._last[name] = value

    def _write_blocking(self, tag: Tag, addr: S7Address, dtype: str, value: Value) -> None:
        if not self._connect():
            raise IOError("; ".join(self.errors) or "PLC not connected")
        area = getattr(self._Area, addr.area)
        try:
            if addr.size == "X":
                current = self.client.read_area(area, addr.db, addr.byte, 1)
                byte = current[0] | (1 << addr.bit) if value else current[0] & ~(1 << addr.bit)
                data = bytearray([byte & 0xFF])
            else:
                raw = (1 if value else 0) if tag.type == "bool" else float(value) * float(tag.s7.get("scale", 1))
                try:
                    data = bytearray(encode(raw, dtype))
                except ValueError as exc:
                    raise TagError(f"{tag.name}: {exc}") from None
            self.client.write_area(area, addr.db, addr.byte, data)
        except TagError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise IOError(f"write {tag.name} to {addr.text()} failed: {exc}") from None
