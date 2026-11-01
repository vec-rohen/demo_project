"""Siemens S7 addresses in TIA Portal / STEP 7 notation.

    %I0.0  %IB2  %IW64  %ID100        inputs (German E: %E0.0, %EW64)
    %Q0.1  %QB2  %QW80  %QD10         outputs (German A: %A0.1)
    %M10.3 %MB5  %MW20  %MD30         bit memory (merkers)
    %DB1.DBX0.0  %DB1.DBB2  %DB1.DBW4  %DB1.DBD8   data blocks

The leading % is optional. Data types per size:
    bit  (X) – bool
    byte (B) – byte, sint, usint
    word (W) – int (default), uint, word
    dword(D) – real (default), dint, udint, dword
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass

_RE = re.compile(
    r"^%?(?:(?P<area>[IQEAM])(?P<size>[XBWD]?)(?P<byte>\d+)(?:\.(?P<bit>[0-7]))?"
    r"|DB(?P<db>\d+)\.DB(?P<dsize>[XBWD])(?P<dbyte>\d+)(?:\.(?P<dbit>[0-7]))?)$",
    re.IGNORECASE,
)
AREAS = {"I": "PE", "E": "PE", "Q": "PA", "A": "PA", "M": "MK", "DB": "DB"}
TYPES_BY_SIZE = {
    "X": ("bool",),
    "B": ("byte", "sint", "usint"),
    "W": ("int", "uint", "word"),
    "D": ("real", "dint", "udint", "dword"),
}
SIZE_BYTES = {"X": 1, "B": 1, "W": 2, "D": 4}
_FORMAT = {"byte": ">B", "usint": ">B", "sint": ">b", "int": ">h", "uint": ">H", "word": ">H",
           "dint": ">i", "udint": ">I", "dword": ">I", "real": ">f"}


class S7AddressError(ValueError):
    pass


@dataclass(frozen=True)
class S7Address:
    area: str        # PE (inputs), PA (outputs), MK (merkers), DB
    db: int          # data block number, 0 for other areas
    byte: int
    bit: int | None  # for bool
    size: str        # X, B, W, D

    @property
    def nbytes(self) -> int:
        return SIZE_BYTES[self.size]

    def text(self) -> str:
        if self.area == "DB":
            return f"%DB{self.db}.DB{self.size}{self.byte}" + (f".{self.bit}" if self.size == "X" else "")
        letter = {"PE": "I", "PA": "Q", "MK": "M"}[self.area]
        if self.size == "X":
            return f"%{letter}{self.byte}.{self.bit}"
        return f"%{letter}{self.size}{self.byte}"


def parse(address: str) -> S7Address:
    m = _RE.match(str(address).strip().replace(" ", ""))
    if not m:
        raise S7AddressError(f"{address!r} isn't an S7 address (e.g. %I0.0, %IW64, %MD10, %DB1.DBD4)")
    if m.group("db"):
        size, byte, bit = m.group("dsize").upper(), int(m.group("dbyte")), m.group("dbit")
        area, db = "DB", int(m.group("db"))
        if int(m.group("db")) < 1:
            raise S7AddressError(f"{address!r}: data block numbers start at 1")
    else:
        area, db = AREAS[m.group("area").upper()], 0
        size, byte, bit = (m.group("size") or "").upper(), int(m.group("byte")), m.group("bit")
        if not size:
            if bit is None:
                raise S7AddressError(f"{address!r}: a bit address needs a bit number, e.g. %I0.0")
            size = "X"
    if size == "X" and bit is None:
        raise S7AddressError(f"{address!r}: a bit address needs a bit number (.0–.7)")
    if size != "X" and bit is not None:
        raise S7AddressError(f"{address!r}: only bit addresses have a .bit part")
    return S7Address(area, db, byte, int(bit) if bit is not None else None, size)


def default_type(addr: S7Address) -> str:
    return TYPES_BY_SIZE[addr.size][0]


def check_type(addr: S7Address, dtype: str) -> None:
    if dtype not in TYPES_BY_SIZE[addr.size]:
        raise S7AddressError(f"{addr.text()} holds {', '.join(TYPES_BY_SIZE[addr.size])}, not {dtype}")


def decode(buf: bytes, offset: int, addr: S7Address, dtype: str) -> float | bool:
    if addr.size == "X":
        return bool(buf[offset] >> addr.bit & 1)
    return struct.unpack(_FORMAT[dtype], bytes(buf[offset:offset + addr.nbytes]))[0]


def encode(value: float, dtype: str) -> bytes:
    if dtype == "real":
        return struct.pack(">f", float(value))
    number = int(round(value))
    try:
        return struct.pack(_FORMAT[dtype], number)
    except struct.error:
        raise ValueError(f"{number} doesn't fit an S7 {dtype.upper()}") from None
