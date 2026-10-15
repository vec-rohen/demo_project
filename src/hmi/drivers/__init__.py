from .base import Driver, Value
from .simulator import SimulatorDriver


def create_driver(kind: str, db, **options) -> Driver:
    """Build a driver by name: ``sim`` (default) or ``modbus``."""
    if kind == "sim":
        return SimulatorDriver(db)
    if kind == "modbus":
        from .modbus import ModbusDriver  # pymodbus is only needed for real PLCs

        return ModbusDriver(db, **options)
    raise ValueError(f"unknown driver {kind!r} (expected 'sim' or 'modbus')")


__all__ = ["Driver", "Value", "SimulatorDriver", "create_driver"]
