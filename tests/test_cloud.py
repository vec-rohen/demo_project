import pytest
from fastapi.testclient import TestClient

from src.cloud.db import CloudDB
from src.cloud.server import create_app as create_portal
from src.cloud.service import CloudService
from src.hmi.drivers import SimulatorDriver
from src.hmi.package import apply_package
from src.hmi.server import create_app as create_edge
from src.hmi.site import SiteManager
from src.hmi.store import Store

PW = "staff-pass-1"


@pytest.fixture
def db():
    db = CloudDB()
    svc = CloudService(db)
    svc.create_user({"username": "boss", "password": PW, "kind": "staff", "portal_role": "admin"}, "t")
    svc.create_user({"username": "tom", "password": PW, "kind": "staff", "portal_role": "technician"}, "t")
    db.run("UPDATE users SET must_change_password = 0")
    return db


@pytest.fixture
def portal(db):
    with TestClient(create_portal(db)) as c:
        yield c


def login(c, user="tom", pw=PW):
    r = c.post("/api/login", json={"username": user, "password": pw})
    assert r.status_code == 200, r.text
    return r


def build_site(c):
    """Client → site → device → asset with I/O → client operator."""
    client = c.post("/api/clients", json={"name": "Acme Water", "email": "ops@acme.example"}).json()
    site = c.post(f"/api/clients/{client['id']}/sites", json={"name": "North Pump Station"}).json()
    device = c.post(f"/api/clients/{client['id']}/devices",
                    json={"name": "IoT2050 MCC", "serial": "SN1", "site_id": site["id"]}).json()
    asset = c.post(f"/api/sites/{site['id']}/assets",
                   json={"name": "Pump 1", "device_id": device["id"], "plc": {"protocol": "sim", "poll_ms": 500}}).json()
    template = c.get("/api/io-template.csv").text
    r = c.post(f"/api/assets/{asset['id']}/import?mode=replace", content=template.encode(),
               headers={"Content-Type": "text/csv"})
    assert r.status_code == 200 and r.json()["added"] == 7, r.text
    c.post("/api/users", json={"username": "olly", "password": "olly-temp-1", "kind": "client",
                               "client_id": client["id"], "full_name": "Olly Operator"})
    c.put("/api/users/olly/access", json=[{"site_id": site["id"], "role": "operator"}])
    return client, site, device, asset


def test_portal_requires_staff_sign_in(portal):
    assert portal.get("/api/clients").status_code == 401
    assert portal.get("/", follow_redirects=False).headers["location"] == "/login"
    assert "Cloud Portal" in portal.get("/login").text
    assert portal.post("/api/login", json={"username": "olly", "password": "x"}).status_code == 401
    login(portal)
    assert portal.get("/api/clients").json() == []


def test_staff_permissions(portal):
    login(portal, "tom")
    r = portal.post("/api/users", json={"username": "eve", "password": PW, "kind": "staff", "portal_role": "admin"})
    assert r.status_code == 403  # technicians can't create staff
    assert portal.patch("/api/users/boss", json={"disabled": True}).status_code == 403
    login(portal, "boss")
    r = portal.post("/api/users", json={"username": "eve", "password": PW, "kind": "staff", "portal_role": "technician"})
    assert r.status_code == 201
    assert portal.delete("/api/users/boss").status_code == 422  # not yourself
    assert portal.patch("/api/users/tom", json={"portal_role": "admin"}).status_code == 200


def test_setup_and_publish_flow(portal):
    login(portal)
    client, site, device, asset = build_site(portal)
    detail = portal.get(f"/api/devices/{device['id']}").json()
    assert detail["problems"] == [] and detail["unpublished_changes"]
    preview = portal.get(f"/api/devices/{device['id']}/preview").json()
    assert "password_hash" not in str(preview)
    assert {u["username"]: u["role"] for u in preview["package"]["users"]} == \
        {"boss": "integrator", "tom": "integrator", "olly": "operator"}
    r = portal.post(f"/api/devices/{device['id']}/publish", json={"notes": "Initial"})
    assert r.json()["version"] == 1
    assert not portal.get(f"/api/devices/{device['id']}").json()["unpublished_changes"]
    # Any change marks the device as needing a new publish.
    portal.patch(f"/api/assets/{asset['id']}", json={"description": "Duty pump"})
    assert portal.get(f"/api/devices/{device['id']}").json()["unpublished_changes"]
    assert portal.post(f"/api/devices/{device['id']}/publish", json={}).json()["version"] == 2
    versions = portal.get(f"/api/devices/{device['id']}").json()["versions"]
    assert [v["version"] for v in versions] == [2, 1] and versions[1]["notes"] == "Initial"
    pkg = portal.get(f"/api/devices/{device['id']}/packages/2").json()
    assert pkg["package_version"] == 2 and pkg["assets"][0]["description"] == "Duty pump"
    audit = [a["action"] for a in portal.get("/api/audit").json()]
    assert "device.publish" in audit and "tag.import" in audit


def test_publish_blocked_until_ready(portal):
    login(portal)
    client = portal.post("/api/clients", json={"name": "Beta"}).json()
    device = portal.post(f"/api/clients/{client['id']}/devices", json={"name": "GW"}).json()
    r = portal.post(f"/api/devices/{device['id']}/publish", json={})
    assert r.status_code == 422 and "site" in r.json()["detail"]
    site = portal.post(f"/api/clients/{client['id']}/sites", json={"name": "S"}).json()
    portal.patch(f"/api/devices/{device['id']}", json={"site_id": site["id"]})
    asset = portal.post(f"/api/sites/{site['id']}/assets", json={"name": "A", "device_id": device["id"]}).json()
    r = portal.post(f"/api/devices/{device['id']}/publish", json={})
    assert r.status_code == 422 and "no I/O" in r.json()["detail"]
    # Moving the device to another site releases its assets.
    site2 = portal.post(f"/api/clients/{client['id']}/sites", json={"name": "S2"}).json()
    portal.patch(f"/api/devices/{device['id']}", json={"site_id": site2["id"]})
    assert portal.get(f"/api/assets/{asset['id']}").json()["device_id"] is None
    # Assets can only go on a device at their own site.
    assert portal.patch(f"/api/assets/{asset['id']}", json={"device_id": device["id"]}).status_code == 422
    # A client with sites can't be deleted.
    assert portal.delete(f"/api/clients/{client['id']}").status_code == 409


def test_csv_import_validation(portal):
    login(portal)
    _, site, _, asset = build_site(portal)
    url = f"/api/assets/{asset['id']}/import"
    bad = "name,kind,type,min,max\nLevel,AI,float,100,0\n"
    r = portal.post(url + "?mode=merge", content=bad, headers={"Content-Type": "text/csv"})
    assert r.status_code == 422 and "nothing was changed" in r.json()["detail"]
    assert len(portal.get(f"/api/assets/{asset['id']}").json()["tags"]) == 7
    r = portal.post(url, content="name,kind\nA,DI\nA,DI\n", headers={"Content-Type": "text/csv"})
    assert r.status_code == 422 and "twice" in r.json()["detail"]
    # Semicolon-separated (European Excel) merge adds a point and updates one
    csv = "name;kind;type;description\nSpare1;DI;bool;Spare input\nPumpRunning;DI;bool;Running (renamed)\n"
    r = portal.post(url + "?mode=merge", content=csv.encode(), headers={"Content-Type": "text/csv"})
    assert r.json() == {"added": 1, "updated": 1, "removed": 0, "total": 8}
    exported = portal.get(f"/api/assets/{asset['id']}/io.csv").text
    assert "Running (renamed)" in exported and exported.startswith("name,description,kind")
    # Round trip: exporting and re-importing changes nothing
    r = portal.post(url + "?mode=replace", content=exported.encode(), headers={"Content-Type": "text/csv"})
    assert r.json()["total"] == 8 and r.json()["removed"] == 0
    tags = {t["name"]: t for t in portal.get(f"/api/assets/{asset['id']}").json()["tags"]}
    assert tags["FlowTotal"]["modbus"]["type"] == "float32" and tags["VSDReady"]["modbus"]["bit"] == 0


def test_duplicate_asset_and_tag_edit(portal):
    login(portal)
    _, site, _, asset = build_site(portal)
    copy = portal.post(f"/api/assets/{asset['id']}/duplicate", json={"name": "Pump 2"}).json()
    assert copy["name"] == "Pump 2" and copy["device_id"] is None
    assert len(portal.get(f"/api/assets/{copy['id']}").json()["tags"]) == 7
    tag = {"name": "Flow", "kind": "AI", "type": "float", "min": 0, "max": 50, "unit": "L/s"}
    assert portal.put(f"/api/assets/{copy['id']}/tags/Flow", json=tag).status_code == 200
    assert portal.put(f"/api/assets/{copy['id']}/tags/Flow", json={**tag, "max": -1}).status_code == 422
    assert portal.delete(f"/api/assets/{copy['id']}/tags/Flow").status_code == 200


def test_access_rules(portal):
    login(portal)
    client, site, _, _ = build_site(portal)
    other = portal.post("/api/clients", json={"name": "Other"}).json()
    other_site = portal.post(f"/api/clients/{other['id']}/sites", json={"name": "X"}).json()
    r = portal.put("/api/users/olly/access", json=[{"site_id": other_site["id"], "role": "viewer"}])
    assert r.status_code == 422  # another client's site
    r = portal.put("/api/users/olly/access", json=[{"site_id": site["id"], "role": "integrator"}])
    assert r.status_code == 422  # integrator is for staff only
    assert portal.put("/api/users/tom/access", json=[]).status_code == 422


def test_published_package_runs_on_edge(portal, tmp_path):
    """End to end: publish in the portal, load on the device, sign in there as the client's operator."""
    login(portal)
    _, _, device, asset = build_site(portal)
    portal.post(f"/api/devices/{device['id']}/publish", json={})
    pkg = portal.get(f"/api/devices/{device['id']}/packages/1").json()

    edge_store = Store(tmp_path / "edge.db")
    apply_package(edge_store, pkg, "test", source="file")
    site = SiteManager(edge_store, driver_factory=lambda plc, db: SimulatorDriver(db, seed=1), poll_interval=0.05)
    with TestClient(create_edge(edge_store, site=site)) as edge:
        r = edge.post("/api/login", json={"username": "olly", "password": "olly-temp-1"})
        assert r.status_code == 200 and r.json()["role"] == "operator" and r.json()["must_change_password"]
        assert edge.post("/api/me/password", json={"current": "olly-temp-1", "new": "olly-own-pass"}).status_code == 200
        info = edge.get("/api/site").json()
        assert info["client"]["name"] == "Acme Water" and info["assets"][0]["id"] == asset["id"]
        state = edge.get(f"/api/assets/{asset['id']}/state").json()
        assert set(state["values"]) == {"PumpRunning", "PumpFault", "PumpStart", "Level", "LevelSP", "FlowTotal", "VSDReady"}
        # Staff accounts are integrators on every device (commissioning / recovery access).
        assert edge.post("/api/login", json={"username": "tom", "password": PW}).json()["role"] == "integrator"


def test_mode_tag_in_portal(portal):
    login(portal)
    _, _, device, asset = build_site(portal)
    url = f"/api/assets/{asset['id']}"
    assert portal.patch(url, json={"mode_tag": "Level"}).status_code == 422      # not digital
    assert portal.patch(url, json={"mode_tag": "Nope"}).status_code == 422
    assert portal.patch(url, json={"mode_tag": "PumpStart"}).status_code == 200
    preview = portal.get(f"/api/devices/{device['id']}/preview").json()["package"]
    assert preview["assets"][0]["mode_tag"] == "PumpStart"
    tag = {"name": "Spare", "kind": "DO", "type": "bool", "writable": True, "manual_only": True}
    assert portal.put(f"{url}/tags/Spare", json=tag).status_code == 200
    assert portal.patch(url, json={"mode_tag": None}).status_code == 422          # manual-only needs it


def tia_workbook(rows):
    """An .xlsx shaped like TIA Portal's PLC tag table export."""
    import io

    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "PLC Tags"
    ws.append(["Name", "Path", "Data Type", "Logical Address", "Comment", "Hmi Visible", "Hmi Accessible", "Hmi Writeable"])
    for r in rows:
        ws.append(r)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def test_tia_tag_table_import(portal):
    pytest.importorskip("openpyxl")
    login(portal)
    _, _, device, asset = build_site(portal)
    url = f"/api/assets/{asset['id']}"
    portal.patch(url, json={"plc": {"protocol": "s7", "host": "192.168.0.10", "rack": 0, "slot": 1}})
    data = tia_workbook([
        ["Pump_Run_FB", "Default tag table", "Bool", "%I0.0", "Pump running feedback", True, True, False],
        ["Level LT-101", "Default tag table", "Int", "%IW64", "Wet well level raw", True, True, False],
        ["Pump_Start", "Default tag table", "Bool", "%Q0.1", "", True, True, False],
        ["LevelSP", "Default tag table", "Real", "%MD100", "Level setpoint", True, True, True],
        ["Running_Hours", "Default tag table", "DInt", "%MD104", "", True, True, False],
        ["Batch_Name", "Default tag table", "String", "%MB200", "", True, True, False],
        ["Bad", "Default tag table", "Real", "%MW300", "", True, True, False],
    ])
    r = portal.post(f"{url}/import-tia?mode=replace", content=data, headers={"Content-Type": XLSX})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["added"] == 4 and body["updated"] == 1   # LevelSP already existed
    assert {s["name"] for s in body["skipped"]} == {"Batch_Name", "Bad"}
    tags = {t["name"]: t for t in portal.get(url).json()["tags"]}
    assert tags["Pump_Run_FB"]["kind"] == "DI" and tags["Pump_Run_FB"]["s7"] == {"address": "%I0.0"}
    assert tags["Level_LT_101"]["kind"] == "AI" and tags["Level_LT_101"]["s7"]["type"] == "int"
    assert tags["Pump_Start"]["kind"] == "DO"
    assert tags["LevelSP"]["kind"] == "SP" and tags["LevelSP"]["writable"] is True
    assert tags["Running_Hours"]["s7"]["type"] == "dint"
    # Merge keeps ranges/units added in the editor
    lvl = {**tags["Level_LT_101"], "min": 0, "max": 100, "unit": "%"}
    portal.put(f"{url}/tags/Level_LT_101", json=lvl)
    portal.post(f"{url}/import-tia?mode=merge", content=data, headers={"Content-Type": XLSX})
    assert portal.get(url).json()["tags"][1]["unit"] == "%"
    # Publishes as a valid S7 package
    assert portal.post(f"/api/devices/{device['id']}/publish", json={}).status_code == 200
    r = portal.post(f"{url}/import-tia", content=b"not excel", headers={"Content-Type": XLSX})
    assert r.status_code == 422 and "Excel" in r.json()["detail"]
