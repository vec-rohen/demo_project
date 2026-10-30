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


def driver_for_plc(plc: dict, db) -> Driver:
    """Build the driver described by an asset's ``plc`` connection block."""
    if plc["protocol"] == "modbus":
        return create_driver("modbus", db, host=plc["host"], port=plc.get("port", 502), unit=plc.get("unit", 1))
    return create_driver(plc["protocol"], db)


__all__ = ["Driver", "Value", "SimulatorDriver", "create_driver", "driver_for_plc"]
