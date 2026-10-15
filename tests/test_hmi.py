import asyncio
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.hmi.alarms import AlarmManager, EventLog
from src.hmi.drivers import SimulatorDriver
from src.hmi.runtime import Runtime
from src.hmi.server import create_app
from src.hmi.tags import TagDatabase, TagError

TAGS = Path(__file__).resolve().parents[1] / "src" / "hmi" / "tags.json"


@pytest.fixture
def db():
    return TagDatabase.load(TAGS)


@pytest.fixture
def client(db):
    runtime = Runtime(db, SimulatorDriver(db, seed=1), poll_interval=0.05)
    with TestClient(create_app(runtime)) as c:
        yield c


def test_coerce_validates_range_and_type(db):
    sp = db.get("SP_LevelTarget")
    assert sp.coerce("55.5") == 55.5
    with pytest.raises(TagError):
        sp.coerce(5)  # below min 10
    with pytest.raises(TagError):
        sp.coerce("abc")
    assert db.get("SP_AutoMode").coerce("off") is False


def test_simulator_auto_mode_reaches_level_target(db):
    sim = SimulatorDriver(db, seed=0)
    for _ in range(int(240 / 0.1)):  # 4 simulated minutes
        sim.step(0.1)
    assert abs(sim.values["AI_TankLevel"] - sim.values["SP_LevelTarget"]) < 2.0
    assert sim.values["DI_PumpRunning"] is True


def test_estop_interlock_stops_outputs(db):
    sim = SimulatorDriver(db, seed=0)
    for _ in range(20):
        sim.step(0.1)
    sim.values["DI_EStopOK"] = False
    sim.step(0.1)
    assert sim.values["DO_PumpRun"] is False
    assert sim.values["DI_PumpRunning"] is False
    assert sim.values["DO_HeaterOn"] is False


def test_alarm_lifecycle_with_setpoint_limit_and_deadband(db):
    events = EventLog()
    alarms = AlarmManager(db, events)
    values = {"AI_TankLevel": 50.0, "SP_LevelHiAlarm": 80.0}
    alarms.evaluate(values)
    assert not alarms.alarms["AI_TankLevel.hi"].active

    values["AI_TankLevel"] = 81.0
    alarms.evaluate(values)
    alarm = alarms.alarms["AI_TankLevel.hi"]
    assert alarm.active and not alarm.acked and alarm.limit == 80.0

    values["AI_TankLevel"] = 79.5  # inside the 1 % deadband: stays active
    alarms.evaluate(values)
    assert alarm.active

    values["AI_TankLevel"] = 78.0
    alarms.evaluate(values)
    assert not alarm.active
    assert [a["id"] for a in alarms.visible()] == ["AI_TankLevel.hi"]  # cleared but unacked

    assert alarms.acknowledge(None, "op") == 1
    assert alarms.visible() == []
    assert [e["kind"] for e in events.all()] == ["alarm", "clear", "ack"]


def test_low_alarm_clears_above_limit_plus_deadband(db):
    alarms = AlarmManager(db, EventLog())
    values = {"AI_TankLevel": 9.0, "SP_LevelHiAlarm": 80.0}
    alarms.evaluate(values)
    lo = alarms.alarms["AI_TankLevel.lo"]
    assert lo.active
    values["AI_TankLevel"] = 10.5
    alarms.evaluate(values)
    assert lo.active
    values["AI_TankLevel"] = 11.5
    alarms.evaluate(values)
    assert not lo.active


def test_api_config_and_state(client):
    cfg = client.get("/api/config").json()
    names = {t["name"] for t in cfg["tags"]}
    assert {"AI_TankLevel", "SP_LevelTarget", "DO_PumpRun"} <= names
    assert "modbus" not in cfg["tags"][0]
    state = client.get("/api/state").json()
    assert state["connected"] is True
    assert "AI_TankLevel" in state["values"]


def test_write_setpoint_and_journal(client):
    r = client.post("/api/tags/SP_LevelTarget", json={"value": 70}, headers={"X-Operator": "alice"})
    assert r.status_code == 200 and r.json()["value"] == 70
    assert client.get("/api/state").json()["values"]["SP_LevelTarget"] == 70
    events = client.get("/api/state").json()["events"]
    assert any(e["kind"] == "write" and e["user"] == "alice" for e in events)


def test_write_rejections(client):
    assert client.post("/api/tags/SP_LevelTarget", json={"value": 500}).status_code == 422
    assert client.post("/api/tags/AI_TankLevel", json={"value": 5}).status_code == 409
    assert client.post("/api/tags/NOPE", json={"value": 5}).status_code == 404
    # Manual-only outputs are locked while in automatic mode.
    assert client.post("/api/tags/DO_PumpRun", json={"value": True}).status_code == 409
    assert client.post("/api/tags/SP_AutoMode", json={"value": False}).status_code == 200
    assert client.post("/api/tags/DO_PumpRun", json={"value": True}).status_code == 200


def test_simulated_input_and_ack(client):
    assert client.post("/api/simulate/DI_EStopOK", json={"value": False}).status_code == 200
    assert client.post("/api/simulate/AI_TankLevel", json={"value": 1}).status_code == 422
    time.sleep(0.2)
    alarms = client.get("/api/state").json()["alarms"]
    assert any(a["id"] == "DI_EStopOK.state" and a["active"] for a in alarms)
    assert client.post("/api/alarms/ack", json={"id": "DI_EStopOK.state"}).json()["acknowledged"] == 1
    assert client.post("/api/alarms/ack", json={"id": "bogus"}).status_code == 404


def test_websocket_streams_updates(client):
    with client.websocket_connect("/ws") as ws:
        first = ws.receive_json()
        assert first["type"] == "update" and "AI_TankLevel" in first["values"]
        assert ws.receive_json()["t"] >= first["t"]


def test_history_endpoint(client):
    hist = client.get("/api/history?tags=AI_TankLevel").json()
    assert len(hist["AI_TankLevel"]) >= 1
    assert client.get("/api/history?tags=DI_EStopOK").status_code == 404


def test_modbus_driver_against_local_server(db):
    """Round-trip reads/writes through a real Modbus TCP server."""
    pytest.importorskip("pymodbus")
    from pymodbus.datastore import ModbusDeviceContext, ModbusSequentialDataBlock, ModbusServerContext
    from pymodbus.server import ModbusTcpServer

    from src.hmi.drivers.modbus import ModbusDriver

    async def scenario():
        device = ModbusDeviceContext(
            di=ModbusSequentialDataBlock(1, [1, 0, 0, 0, 1] + [0] * 20),
            co=ModbusSequentialDataBlock(1, [0] * 32),
            ir=ModbusSequentialDataBlock(1, [553, 214, 0, 0, 0, 0xFFF6] + [0] * 10),  # 55.3 %, 21.4 °C, -1.0 A
            hr=ModbusSequentialDataBlock(1, [0] * 32),
        )
        # Data blocks start at 1 so that protocol address 0 maps to the first value.
        server = ModbusTcpServer(ModbusServerContext(devices=device, single=True), address=("127.0.0.1", 15020))
        task = asyncio.create_task(server.serve_forever())
        await asyncio.sleep(0.3)
        driver = ModbusDriver(db, host="127.0.0.1", port=15020)
        try:
            await driver.start()
            values = await driver.read_all()
            await driver.write("SP_LevelTarget", 62.5)
            await driver.write("DO_DrainValve", True)
            after = await driver.read_all()
        finally:
            await driver.stop()
            await server.shutdown()
            task.cancel()
        return values, after

    values, after = asyncio.run(scenario())
    assert after["SP_LevelTarget"] == 62.5
    assert after["DO_DrainValve"] is True
    assert values["DI_EStopOK"] is True and values["DI_DoorClosed"] is True
    assert values["AI_TankLevel"] == 55.3
    assert values["AI_TankTemp"] == 21.4
    assert values["AI_MotorCurrent"] == -1.0  # signed 16-bit decoding
