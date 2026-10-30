import asyncio
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.hmi.alarms import AlarmManager, EventLog
from src.hmi.auth import AuthError, SessionManager, UserStore, hash_password, verify_password
from src.hmi.drivers import SimulatorDriver, driver_for_plc
from src.hmi.package import PackageError, apply_package, demo_package, export_package
from src.hmi.server import create_app
from src.hmi.site import SiteManager
from src.hmi.store import Store
from src.hmi.tags import TagDatabase, TagError

TAGS = Path(__file__).resolve().parents[1] / "src" / "hmi" / "tags.json"


@pytest.fixture
def db():
    return TagDatabase.load(TAGS)


PASSWORD = "correct-horse"


ASSET = "SKID-01"
A = f"/api/assets/{ASSET}"


def sim_factory(plc, db):
    """Seeded simulator for 'sim' assets so tests are repeatable."""
    return SimulatorDriver(db, seed=1) if plc["protocol"] == "sim" else driver_for_plc(plc, db)


@pytest.fixture
def edge(tmp_path):
    """A device database loaded with the demo package."""
    store = Store(tmp_path / "edge.db")
    apply_package(store, demo_package(), "test")
    return store


@pytest.fixture
def store(edge):
    """User accounts on the device."""
    users = UserStore(edge)
    for name, role in (("eng", "engineer"), ("op", "operator"), ("view", "viewer"), ("integ", "integrator")):
        users.add(name, PASSWORD, role, must_change_password=False)
    return users


def make_app(edge):
    return create_app(edge, site=SiteManager(edge, driver_factory=sim_factory, poll_interval=0.05))


def login(client, username, password=PASSWORD):
    return client.post("/api/login", json={"username": username, "password": password})


@pytest.fixture
def anon(edge, store):
    """A client that has not signed in."""
    with TestClient(make_app(edge)) as c:
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
    cfg = client.get(f"{A}/config").json()
    names = {t["name"] for t in cfg["tags"]}
    assert {"AI_TankLevel", "SP_LevelTarget", "DO_PumpRun"} <= names
    assert "modbus" not in cfg["tags"][0]
    state = client.get(f"{A}/state").json()
    assert state["connected"] is True
    assert "AI_TankLevel" in state["values"]


def test_write_setpoint_and_journal(client):
    r = client.post(f"{A}/tags/SP_LevelTarget", json={"value": 70}, headers={"X-Operator": "spoofed"})
    assert r.status_code == 200 and r.json()["value"] == 70
    assert client.get(f"{A}/state").json()["values"]["SP_LevelTarget"] == 70
    events = client.get(f"{A}/state").json()["events"]
    assert any(e["kind"] == "write" and e["user"] == "eng" for e in events)  # header is ignored


def test_write_rejections(client):
    assert client.post(f"{A}/tags/SP_LevelTarget", json={"value": 500}).status_code == 422
    assert client.post(f"{A}/tags/AI_TankLevel", json={"value": 5}).status_code == 409
    assert client.post(f"{A}/tags/NOPE", json={"value": 5}).status_code == 404
    # Manual-only outputs are locked while in automatic mode.
    assert client.post(f"{A}/tags/DO_PumpRun", json={"value": True}).status_code == 409
    assert client.post(f"{A}/tags/SP_AutoMode", json={"value": False}).status_code == 200
    assert client.post(f"{A}/tags/DO_PumpRun", json={"value": True}).status_code == 200


def test_simulated_input_and_ack(client):
    assert client.post(f"{A}/simulate/DI_EStopOK", json={"value": False}).status_code == 200
    assert client.post(f"{A}/simulate/AI_TankLevel", json={"value": 1}).status_code == 422
    time.sleep(0.2)
    alarms = client.get(f"{A}/state").json()["alarms"]
    assert any(a["id"] == "DI_EStopOK.state" and a["active"] for a in alarms)
    assert client.post(f"{A}/alarms/ack", json={"id": "DI_EStopOK.state"}).json()["acknowledged"] == 1
    assert client.post(f"{A}/alarms/ack", json={"id": "bogus"}).status_code == 404


def test_websocket_streams_updates(client):
    with client.websocket_connect(f"/ws/{ASSET}") as ws:
        first = ws.receive_json()
        assert first["type"] == "update" and "AI_TankLevel" in first["values"]
        assert ws.receive_json()["t"] >= first["t"]


def test_history_endpoint(client):
    hist = client.get(f"{A}/history?tags=AI_TankLevel").json()
    assert len(hist["AI_TankLevel"]) >= 1
    assert client.get(f"{A}/history?tags=DI_EStopOK").status_code == 404


def test_modbus_driver_against_local_server(db):
    """Round-trip reads/writes through a real Modbus TCP server."""
    pytest.importorskip("pymodbus")
    from pymodbus import datastore
    from pymodbus.datastore import ModbusSequentialDataBlock, ModbusServerContext
    from pymodbus.server import ModbusTcpServer

    # pymodbus >= 3.10 says "device", older releases (Python 3.9) say "slave".
    ModbusDeviceContext = getattr(datastore, "ModbusDeviceContext", None) or datastore.ModbusSlaveContext
    devices_kw = "devices" if hasattr(datastore, "ModbusDeviceContext") else "slaves"

    from src.hmi.drivers.modbus import ModbusDriver

    async def scenario():
        device = ModbusDeviceContext(
            di=ModbusSequentialDataBlock(1, [1, 0, 0, 0, 1] + [0] * 20),
            co=ModbusSequentialDataBlock(1, [0] * 32),
            ir=ModbusSequentialDataBlock(1, [553, 214, 0, 0, 0, 0xFFF6] + [0] * 10),  # 55.3 %, 21.4 °C, -1.0 A
            hr=ModbusSequentialDataBlock(1, [0] * 32),
        )
        # Data blocks start at 1 so that protocol address 0 maps to the first value.
        server = ModbusTcpServer(ModbusServerContext(**{devices_kw: device}, single=True), address=("127.0.0.1", 15020))
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
    store = UserStore(Store(tmp_path / "u.db"))
    password = store.bootstrap()
    assert password and store.users["admin"].role == "integrator"
    assert store.users["admin"].must_change_password
    assert UserStore(Store(tmp_path / "u.db")).bootstrap() is None  # persisted


def test_last_engineer_is_protected(tmp_path):
    store = UserStore(Store(tmp_path / "u.db"))
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
    assert anon.get(f"{A}/state").status_code == 401
    assert anon.post(f"{A}/tags/SP_LevelTarget", json={"value": 50}).status_code == 401
    r = anon.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert anon.get("/login").status_code == 200
    with pytest.raises(Exception):
        with anon.websocket_connect(f"/ws/{ASSET}") as ws:
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
    r = client.post(f"{A}/tags/SP_LevelTarget", content="value=50",
                    headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 415


def test_viewer_is_read_only(anon):
    login(anon, "view")
    assert anon.get(f"{A}/state").status_code == 200
    assert anon.post(f"{A}/tags/SP_LevelTarget", json={"value": 50}).status_code == 403
    assert anon.post(f"{A}/alarms/ack", json={}).status_code == 403
    assert anon.get("/api/users").status_code == 403


def test_operator_limits(anon):
    login(anon, "op")
    assert anon.post(f"{A}/tags/SP_LevelTarget", json={"value": 50}).status_code == 200
    assert anon.post(f"{A}/alarms/ack", json={}).status_code == 200
    # Alarm limits, tuning and simulator forcing are engineer-only.
    assert anon.post(f"{A}/tags/SP_LevelHiAlarm", json={"value": 80}).status_code == 403
    assert anon.post(f"{A}/tags/SP_LevelKp", json={"value": 3}).status_code == 403
    assert anon.post(f"{A}/simulate/DI_DoorClosed", json={"value": False}).status_code == 403
    assert anon.get("/api/users").status_code == 403


def test_forced_password_change(anon, edge):
    # Reset from the command-line tool (its own database connection) while the server runs.
    UserStore(Store(edge.path)).update("op", password="temporary-1")
    login(anon, "op", "temporary-1")
    assert anon.get("/api/me").json()["must_change_password"] is True
    assert anon.post(f"{A}/tags/SP_LevelTarget", json={"value": 50}).status_code == 403
    r = anon.post("/api/me/password", json={"current": "wrong", "new": "brand-new-pass"})
    assert r.status_code == 422
    assert anon.post("/api/me/password", json={"current": "temporary-1", "new": "brand-new-pass"}).status_code == 200
    assert anon.post(f"{A}/tags/SP_LevelTarget", json={"value": 50}).status_code == 200


def test_user_admin(client):
    r = client.post("/api/users", json={"username": "Bob", "password": "temp-pass-1", "role": "viewer"})
    assert r.status_code == 201 and r.json()["username"] == "bob"
    assert r.json()["must_change_password"] is True
    assert client.post("/api/users", json={"username": "bob", "password": "temp-pass-1"}).status_code == 422
    assert client.patch("/api/users/bob", json={"role": "operator"}).json()["role"] == "operator"
    assert client.patch("/api/users/eng", json={"role": "viewer"}).status_code == 422  # not yourself
    assert client.delete("/api/users/eng").status_code == 422
    assert client.delete("/api/users/nobody").status_code == 404
    assert [u["username"] for u in client.get("/api/users").json()] == ["bob", "eng", "integ", "op", "view"]
    assert client.delete("/api/users/bob").status_code == 200
    kinds = [e["message"] for e in client.get(f"{A}/state").json()["events"] if e["kind"] == "auth"]
    assert "User bob created (viewer)" in kinds and "User bob deleted" in kinds


def test_disabling_user_ends_their_session(edge, store):
    app = make_app(edge)
    with TestClient(app) as admin:
        login(admin, "eng")
        other = TestClient(app)  # second browser sharing the running app
        login(other, "op")
        assert other.get("/api/me").status_code == 200
        assert admin.patch("/api/users/op", json={"disabled": True}).status_code == 200
        assert other.get("/api/me").status_code == 401
        assert login(other, "op").status_code == 401


# -- edge device: packages, multiple assets, commissioning -------------------------

def two_asset_package():
    pkg = demo_package()
    pkg["package_version"] = 7
    pkg["assets"].append({
        "id": "PS-02", "name": "Pump Station 2", "screen": "auto",
        "plc": {"protocol": "sim", "poll_ms": 500},
        "tags": [
            {"name": "Run", "kind": "DI", "type": "bool", "description": "Pump running"},
            {"name": "Flow", "kind": "AI", "type": "float", "description": "Flow", "unit": "L/s",
             "min": 0, "max": 50, "alarm": {"hi": 45}},
        ],
    })
    return pkg


def test_package_validation():
    pkg = demo_package()
    pkg["assets"][0]["plc"] = {"protocol": "modbus"}  # no host
    with pytest.raises(PackageError, match="IP address"):
        apply_package(Store(), pkg, "t")
    pkg = demo_package()
    pkg["assets"][0]["tags"][0]["kind"] = "XX"
    with pytest.raises(PackageError, match="unknown kind"):
        apply_package(Store(), pkg, "t")
    pkg = demo_package()
    pkg["assets"][0]["tags"][9]["alarm"]["hi"] = "NoSuchTag"
    with pytest.raises(PackageError, match="unknown tag"):
        apply_package(Store(), pkg, "t")
    with pytest.raises(PackageError, match="format"):
        apply_package(Store(), {"assets": []}, "t")
    pkg = demo_package()
    pkg["users"] = [{"username": "op", "role": "operator", "password_hash": hash_password("x" * 8)}]
    with pytest.raises(PackageError, match="engineer or integrator"):
        apply_package(Store(), pkg, "t")


def test_package_round_trip(edge):
    exported = export_package(edge)
    assert exported["client"]["id"] == "DEMO" and len(exported["assets"][0]["tags"]) == 23
    fresh = Store()
    apply_package(fresh, exported, "t")
    assert fresh.tags(ASSET) == edge.tags(ASSET)


def test_site_info_and_package_upload(anon):
    login(anon, "eng")
    assert anon.post("/api/package", json=two_asset_package()).status_code == 403  # integrators only
    login(anon, "integ")
    r = anon.post("/api/package", json=two_asset_package())
    assert r.status_code == 200 and r.json()["package"]["version"] == 7
    login(anon, "integ")  # loading a package ends all sessions
    info = anon.get("/api/site").json()
    assert [a["id"] for a in info["assets"]] == [ASSET, "PS-02"]
    assert all(a["running"] for a in info["assets"])
    cfg = anon.get("/api/assets/PS-02/config").json()
    assert cfg["asset"]["screen"] == "auto" and {t["name"] for t in cfg["tags"]} == {"Run", "Flow"}
    with anon.websocket_connect("/ws/PS-02") as ws:
        assert ws.receive_json()["asset"] == "PS-02"
    assert anon.get("/api/assets/NOPE/state").status_code == 404
    bad = two_asset_package()
    bad["schema"] = 99
    assert anon.post("/api/package", json=bad).status_code == 422


def test_plc_settings_and_test(anon):
    login(anon, "eng")
    assert anon.get(f"{A}/plc").json()["protocol"] == "sim"
    r = anon.post(f"{A}/plc/test", json={})
    assert r.status_code == 200 and r.json()["ok"] and r.json()["tags_read"] == 23
    assert anon.put(f"{A}/plc", json={"protocol": "sim"}).status_code == 403  # engineer can't change it
    login(anon, "integ")
    assert anon.put(f"{A}/plc", json={"protocol": "modbus", "host": ""}).status_code == 422
    # Nothing listens on this port: the test reports a clear failure instead of hanging.
    r = anon.post(f"{A}/plc/test", json={"protocol": "modbus", "host": "127.0.0.1", "port": 1, "poll_ms": 500})
    assert r.status_code == 200 and not r.json()["ok"] and r.json()["errors"]
    r = anon.put(f"{A}/plc", json={"protocol": "sim", "poll_ms": 300})
    assert r.status_code == 200 and r.json()["poll_ms"] == 300
    changes = anon.get("/api/changes").json()
    assert changes[-1]["kind"] == "plc" and changes[-1]["user"] == "integ" and not changes[-1]["synced"]


def test_io_checks(anon):
    login(anon, "op")
    assert anon.put(f"{A}/io/AI_TankLevel/check", json={"status": "pass"}).status_code == 403
    login(anon, "eng")
    r = anon.put(f"{A}/io/AI_TankLevel/check", json={"status": "pass", "note": "  4-20 mA verified "})
    assert r.json()["status"] == "pass" and r.json()["note"] == "4-20 mA verified" and r.json()["user"] == "eng"
    assert anon.put(f"{A}/io/AI_TankLevel/check", json={"status": "maybe"}).status_code == 422
    assert anon.put(f"{A}/io/Nope/check", json={"status": "pass"}).status_code == 404
    io = anon.get(f"{A}/io").json()
    assert io["checks"]["AI_TankLevel"]["status"] == "pass" and len(io["tags"]) == 23
    anon.put(f"{A}/io/AI_TankLevel/check", json={"status": None})
    assert "AI_TankLevel" not in anon.get(f"{A}/io").json()["checks"]


def test_onsite_tag_edit(anon, edge):
    login(anon, "eng")
    anon.put(f"{A}/io/AI_InletFlow/check", json={"status": "fail", "note": "reads double"})
    tag = next(t for t in anon.get(f"{A}/io").json()["tags"] if t["name"] == "AI_InletFlow")
    fixed = {**tag, "description": "Inlet flow (FT-101)", "modbus": {**tag["modbus"], "scale": 20}}
    assert anon.put(f"{A}/tags/AI_InletFlow/definition", json=fixed).status_code == 403  # integrators only
    login(anon, "integ")
    assert anon.put(f"{A}/tags/AI_InletFlow/definition", json=fixed).status_code == 200
    cfg = anon.get(f"{A}/config").json()
    assert next(t for t in cfg["tags"] if t["name"] == "AI_InletFlow")["description"] == "Inlet flow (FT-101)"
    assert "AI_InletFlow" not in anon.get(f"{A}/io").json()["checks"]  # needs re-checking after a change
    change = anon.get("/api/changes").json()[-1]
    assert change["kind"] == "tag" and change["before"]["modbus"]["scale"] == 10 and change["after"]["modbus"]["scale"] == 20
    # Validation
    assert anon.put(f"{A}/tags/AI_InletFlow/definition", json={**fixed, "min": 200}).status_code == 422
    assert anon.put(f"{A}/tags/AI_InletFlow/definition", json={**fixed, "name": "Other"}).status_code == 422
    # New tag, then delete; a tag another alarm depends on can't be deleted
    new = {"name": "AI_Spare", "kind": "AI", "type": "float", "description": "Spare", "min": 0, "max": 10}
    assert anon.put(f"{A}/tags/AI_Spare/definition", json=new).status_code == 200
    assert anon.delete(f"{A}/tags/AI_Spare").status_code == 200
    assert anon.delete(f"{A}/tags/SP_LevelHiAlarm").status_code == 422
    # The live runtime still works after the reloads
    assert anon.get(f"{A}/state").json()["connected"] is True


def test_role_escalation_blocked(client):
    # Engineers can't create or promote anyone to integrator.
    r = client.post("/api/users", json={"username": "sneaky", "password": "temp-pass-1", "role": "integrator"})
    assert r.status_code == 403
    assert client.patch("/api/users/op", json={"role": "integrator"}).status_code == 403
    assert client.patch("/api/users/integ", json={"disabled": True}).status_code == 403
    assert all("password_hash" not in str(c) for c in client.get("/api/changes").json())


def test_journal_and_history_survive_restart(edge, store):
    with TestClient(make_app(edge)) as c:
        login(c, "op")
        c.post(f"{A}/tags/SP_LevelTarget", json={"value": 66})
        time.sleep(1.2)
    edge.add_history([(ASSET, "AI_TankLevel", time.time() - 3600, 42.0)])
    with TestClient(make_app(edge)) as c:
        login(c, "op")
        events = c.get(f"{A}/state").json()["events"]
        assert any(e["kind"] == "write" and e.get("new") == 66 for e in events)
        hist = c.get(f"{A}/history?tags=AI_TankLevel&seconds=7200").json()["AI_TankLevel"]
        assert hist and hist[0][1] == 42.0  # older than the in-memory window: read from the database


def test_fresh_device_waits_for_configuration(tmp_path, monkeypatch):
    """A new install has no plant until an integrator loads a package from the browser."""
    pw_file = tmp_path / "initial-admin-password"
    monkeypatch.setenv("HMI_INITIAL_PASSWORD_FILE", str(pw_file))
    monkeypatch.delenv("HMI_DEMO", raising=False)
    monkeypatch.delenv("HMI_PACKAGE", raising=False)
    from src.hmi.server import open_store

    store = open_store(str(tmp_path / "edge.db"))
    assert store.is_empty()
    app = create_app(store, site=SiteManager(store, driver_factory=sim_factory, poll_interval=0.05))
    password = pw_file.read_text().split("password: ")[1].strip()
    assert oct(pw_file.stat().st_mode)[-3:] == "600"
    with TestClient(app) as c:
        assert login(c, "admin", password).status_code == 200
        info = c.get("/api/site").json()
        assert info["configured"] is False and info["assets"] == [] and info["hostname"]
        c.post("/api/me/password", json={"current": password, "new": "my-own-pass-1"})
        assert not pw_file.exists()  # one-time password removed once it has been replaced
        assert c.post("/api/package", json=demo_package()).status_code == 200
        login(c, "admin", "my-own-pass-1")
        assert c.get("/api/site").json()["configured"] is True
        assert c.get(f"{A}/state").json()["connected"] is True
