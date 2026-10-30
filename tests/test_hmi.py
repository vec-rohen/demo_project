import asyncio
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.hmi.alarms import AlarmManager, EventLog
from src.hmi.auth import AuthError, SessionManager, UserStore, hash_password, verify_password
from src.hmi.drivers import SimulatorDriver
from src.hmi.runtime import Runtime
from src.hmi.server import create_app
from src.hmi.tags import TagDatabase, TagError

TAGS = Path(__file__).resolve().parents[1] / "src" / "hmi" / "tags.json"


@pytest.fixture
def db():
    return TagDatabase.load(TAGS)


PASSWORD = "correct-horse"


@pytest.fixture
def store(tmp_path):
    store = UserStore(tmp_path / "users.json")
    for name, role in (("eng", "engineer"), ("op", "operator"), ("view", "viewer")):
        store.add(name, PASSWORD, role, must_change_password=False)
    return store


def login(client, username, password=PASSWORD):
    return client.post("/api/login", json={"username": username, "password": password})


@pytest.fixture
def anon(db, store):
    """A client that has not signed in."""
    runtime = Runtime(db, SimulatorDriver(db, seed=1), poll_interval=0.05)
    with TestClient(create_app(runtime, users=store)) as c:
        yield c


@pytest.fixture
def client(anon):
    """A client signed in as an engineer."""
    assert login(anon, "eng").status_code == 200
    return anon


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
    r = client.post("/api/tags/SP_LevelTarget", json={"value": 70}, headers={"X-Operator": "spoofed"})
    assert r.status_code == 200 and r.json()["value"] == 70
    assert client.get("/api/state").json()["values"]["SP_LevelTarget"] == 70
    events = client.get("/api/state").json()["events"]
    assert any(e["kind"] == "write" and e["user"] == "eng" for e in events)  # header is ignored


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


# -- authentication & roles ---------------------------------------------------

def test_password_hashing():
    stored = hash_password("s3cret-pass")
    assert stored.startswith("scrypt$") and "s3cret-pass" not in stored
    assert verify_password("s3cret-pass", stored)
    assert not verify_password("wrong", stored)
    assert not verify_password("s3cret-pass", "garbage")


def test_bootstrap_creates_admin_once(tmp_path):
    store = UserStore(tmp_path / "u.json")
    password = store.bootstrap()
    assert password and store.users["admin"].role == "engineer"
    assert store.users["admin"].must_change_password
    assert UserStore(tmp_path / "u.json").bootstrap() is None  # persisted


def test_last_engineer_is_protected(tmp_path):
    store = UserStore(tmp_path / "u.json")
    store.add("boss", PASSWORD, "engineer")
    with pytest.raises(AuthError):
        store.update("boss", role="operator")
    with pytest.raises(AuthError):
        store.delete("boss")
    with pytest.raises(AuthError):
        store.add("x", "short", "operator")


def test_login_throttling(store):
    sessions = SessionManager(store, max_failures=3, lockout_seconds=60)
    for _ in range(3):
        with pytest.raises(AuthError, match="Incorrect"):
            sessions.login("op", "nope")
    with pytest.raises(AuthError, match="Too many"):
        sessions.login("op", PASSWORD)  # locked even with the right password


def test_idle_timeout_expires_session(store):
    sessions = SessionManager(store, idle_timeout=60)
    session, _ = sessions.login("op", PASSWORD)
    assert sessions.resolve(session.token).username == "op"
    session.last_seen -= 61
    assert sessions.resolve(session.token) is None


def test_requires_sign_in(anon):
    assert anon.get("/api/state").status_code == 401
    assert anon.post("/api/tags/SP_LevelTarget", json={"value": 50}).status_code == 401
    r = anon.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert anon.get("/login").status_code == 200
    with pytest.raises(Exception):
        with anon.websocket_connect("/ws") as ws:
            ws.receive_json()


def test_login_logout_flow(anon):
    assert login(anon, "op", "wrong").status_code == 401
    r = login(anon, "op")
    assert r.status_code == 200 and r.json()["role"] == "operator"
    assert "password_hash" not in r.json()
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert anon.get("/api/me").json()["username"] == "op"
    assert anon.get("/", follow_redirects=False).status_code == 200
    anon.post("/api/logout", json={})
    assert anon.get("/api/me").status_code == 401


def test_non_json_posts_rejected(client):
    r = client.post("/api/tags/SP_LevelTarget", content="value=50",
                    headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 415


def test_viewer_is_read_only(anon):
    login(anon, "view")
    assert anon.get("/api/state").status_code == 200
    assert anon.post("/api/tags/SP_LevelTarget", json={"value": 50}).status_code == 403
    assert anon.post("/api/alarms/ack", json={}).status_code == 403
    assert anon.get("/api/users").status_code == 403


def test_operator_limits(anon):
    login(anon, "op")
    assert anon.post("/api/tags/SP_LevelTarget", json={"value": 50}).status_code == 200
    assert anon.post("/api/alarms/ack", json={}).status_code == 200
    # Alarm limits, tuning and simulator forcing are engineer-only.
    assert anon.post("/api/tags/SP_LevelHiAlarm", json={"value": 80}).status_code == 403
    assert anon.post("/api/tags/SP_LevelKp", json={"value": 3}).status_code == 403
    assert anon.post("/api/simulate/DI_DoorClosed", json={"value": False}).status_code == 403
    assert anon.get("/api/users").status_code == 403


def test_forced_password_change(anon, store):
    store.update("op", password="temporary-1")  # admin reset -> must change
    login(anon, "op", "temporary-1")
    assert anon.get("/api/me").json()["must_change_password"] is True
    assert anon.post("/api/tags/SP_LevelTarget", json={"value": 50}).status_code == 403
    r = anon.post("/api/me/password", json={"current": "wrong", "new": "brand-new-pass"})
    assert r.status_code == 422
    assert anon.post("/api/me/password", json={"current": "temporary-1", "new": "brand-new-pass"}).status_code == 200
    assert anon.post("/api/tags/SP_LevelTarget", json={"value": 50}).status_code == 200


def test_user_admin(client):
    r = client.post("/api/users", json={"username": "Bob", "password": "temp-pass-1", "role": "viewer"})
    assert r.status_code == 201 and r.json()["username"] == "bob"
    assert r.json()["must_change_password"] is True
    assert client.post("/api/users", json={"username": "bob", "password": "temp-pass-1"}).status_code == 422
    assert client.patch("/api/users/bob", json={"role": "operator"}).json()["role"] == "operator"
    assert client.patch("/api/users/eng", json={"role": "viewer"}).status_code == 422  # not yourself
    assert client.delete("/api/users/eng").status_code == 422
    assert client.delete("/api/users/nobody").status_code == 404
    assert [u["username"] for u in client.get("/api/users").json()] == ["bob", "eng", "op", "view"]
    assert client.delete("/api/users/bob").status_code == 200
    kinds = [e["message"] for e in client.get("/api/state").json()["events"] if e["kind"] == "auth"]
    assert "User bob created (viewer)" in kinds and "User bob deleted" in kinds


def test_disabling_user_ends_their_session(db, store):
    runtime = Runtime(db, SimulatorDriver(db, seed=1), poll_interval=0.05)
    app = create_app(runtime, users=store)
    with TestClient(app) as admin:
        login(admin, "eng")
        other = TestClient(app)  # second browser sharing the running app
        login(other, "op")
        assert other.get("/api/me").status_code == 200
        assert admin.patch("/api/users/op", json={"disabled": True}).status_code == 200
        assert other.get("/api/me").status_code == 401
        assert login(other, "op").status_code == 401
