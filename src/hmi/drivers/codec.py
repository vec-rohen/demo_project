"""Convert between Modbus 16-bit registers and engineering values.

Data types (the tag's ``modbus.type``):

* ``int16``   (default) signed 16-bit, 1 register
* ``uint16``  unsigned 16-bit, 1 register
* ``int32`` / ``uint32``   32-bit integer, 2 registers (Siemens DINT/UDINT, Schneider INT32…)
* ``float32`` IEEE 754 float, 2 registers (Siemens REAL, most "floating point" registers)

``word_order`` says which register of a 32-bit value comes first:
``big`` (default, high word first – Siemens, Schneider, Modicon "ABCD") or
``little`` (low word first – many Allen-Bradley, Wago and Unitronics setups, "CDAB").
Bytes within each register are always big-endian, as Modbus specifies.
"""

from __future__ import annotations

import struct

TYPES = ("int16", "uint16", "int32", "uint32", "float32")
WORD_ORDERS = ("big", "little")
_FORMAT = {"int16": ">h", "uint16": ">H", "int32": ">i", "uint32": ">I", "float32": ">f"}
_RANGE = {"int16": (-0x8000, 0x7FFF), "uint16": (0, 0xFFFF), "int32": (-0x80000000, 0x7FFFFFFF),
          "uint32": (0, 0xFFFFFFFF)}


def register_count(dtype: str) -> int:
    return 2 if dtype in ("int32", "uint32", "float32") else 1


def decode(registers: list[int], dtype: str = "int16", word_order: str = "big") -> float:
    words = list(registers[:register_count(dtype)])
    if word_order == "little":
        words.reverse()
    raw = b"".join(struct.pack(">H", w & 0xFFFF) for w in words)
    return struct.unpack(_FORMAT[dtype], raw)[0]


def encode(value: float, dtype: str = "int16", word_order: str = "big") -> list[int]:
    if dtype == "float32":
        raw = struct.pack(">f", float(value))
    else:
        number = int(round(value))
        low, high = _RANGE[dtype]
        if not low <= number <= high:
            raise ValueError(f"{number} doesn't fit a {dtype} register ({low} to {high})")
        raw = struct.pack(_FORMAT[dtype], number)
    words = [struct.unpack(">H", raw[i:i + 2])[0] for i in range(0, len(raw), 2)]
    if word_order == "little":
        words.reverse()
    return words


def batches(spans: list[tuple[int, int]], max_count: int, max_gap: int = 16) -> list[tuple[int, int]]:
    """Group (start, count) spans into as few reads as possible, each at most
    ``max_count`` long, without reading across large unused gaps."""
    out: list[list[int]] = []
    for start, count in sorted(spans):
        end = start + count
        if out and start - out[-1][1] <= max_gap and end - out[-1][0] <= max_count:
            out[-1][1] = max(out[-1][1], end)
        else:
            out.append([start, end])
    return [(s, e - s) for s, e in out]
