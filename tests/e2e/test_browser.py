"""Browser walkthroughs of the edge HMI and the cloud portal."""

from __future__ import annotations

import csv
import io
import json

from playwright.sync_api import expect

from .conftest import PW, sign_in

DEMO = "SKID-01"


# -- edge HMI -----------------------------------------------------------------------

def test_overview_is_live_and_setpoint_change_works(page, edge):
    sign_in(page, edge.url, "op")
    page.goto(f"{edge.url}/?asset={DEMO}")
    expect(page.locator("#conn-text")).to_have_text("PLC online")
    level = page.locator(".tank-level")
    first = level.text_content()
    expect(level).not_to_have_text(first, timeout=10_000)  # simulated tank level moves

    page.click(".tabs [data-tab=setpoints]")
    card = page.locator("[data-sp=SP_LevelTarget]")
    card.locator("input[type=number]").fill("72")
    card.locator(".btn.primary").click()
    page.click("#confirm-ok")
    expect(card.locator(".sp-live")).to_contain_text("72.0")
    # Alarm limits are engineer-only: the operator sees them locked with the reason.
    expect(page.locator("[data-sp=SP_LevelHiAlarm] .sp-lock")).to_have_text("Engineer role required")


def test_alarm_raise_and_acknowledge(page, edge):
    sign_in(page, edge.url, "eng")
    page.goto(f"{edge.url}/?asset={DEMO}#io")
    page.locator("[data-row=DI_EStopOK] .sim-btn").click()
    expect(page.locator("#alarm-banner")).to_contain_text("E-stop activated")
    page.click(".tabs [data-tab=alarms]")
    row = page.locator("#alarm-table tbody tr", has_text="E-stop activated")
    row.locator("button", has_text="Ack").click()
    expect(row.locator(".state")).to_have_text("Active · acknowledged")


def test_manual_only_outputs_follow_the_mode_tag(page, edge):
    sign_in(page, edge.url, "op")
    page.goto(f"{edge.url}/?asset={DEMO}#setpoints")
    pump = page.locator("[data-sp=DO_PumpRun]")
    expect(pump.locator(".sp-lock")).to_contain_text("Controlled by PLC in Auto")
    page.click("#mode-switch [data-mode=false]")
    page.click("#confirm-ok")
    expect(page.locator("#mode-chip")).to_have_text("MANUAL")
    expect(pump.locator(".sp-lock")).to_be_hidden()


def test_commissioning_test_check_and_edit(page, edge):
    sign_in(page, edge.url, "integ")
    page.goto(f"{edge.url}/?asset={DEMO}#commission")
    page.click("#plc-test")
    expect(page.locator("#plc-result")).to_contain_text("Connected · read 23 of 23")

    row = page.locator("#io-check-table tbody tr", has=page.locator(".io-name", has_text="AI_TankTemp"))
    row.locator(".chk-fail").click()
    page.fill("#ask-input", "reads 10 °C high")
    page.click("#ask-dialog .btn.primary")
    expect(row).to_contain_text("reads 10 °C high")
    expect(page.locator(".progress-text")).to_contain_text("1 failed")

    # Edit with the shared dialog: register settings appear for input registers.
    row.locator("button", has_text="Edit").click()
    expect(page.locator("#tag-dialog")).to_be_visible()
    expect(page.locator("#td-mtype")).to_be_visible()
    page.select_option("#td-mtype", "float32")
    expect(page.locator("#td-word")).to_be_visible()
    page.fill("#td-scale", "1")
    page.click("#tag-form .btn.primary")
    page.wait_for_load_state("load")
    row = page.locator("#io-check-table tbody tr", has=page.locator(".io-name", has_text="AI_TankTemp"))
    expect(row).to_contain_text("3x 1 FLOAT32")
    expect(row.locator(".chk-fail")).to_have_attribute("aria-pressed", "false")  # needs re-checking


def test_offline_plc_greys_out_values_and_alarms(page, edge):
    sign_in(page, edge.url, "op")
    page.goto(f"{edge.url}/?asset=OFFLINE")
    expect(page.locator("#conn-text")).to_have_text("PLC comms lost")
    expect(page.locator("#stale-strip")).to_contain_text("PLC communication lost")
    expect(page.locator("body")).to_have_class("stale")
    expect(page.locator("#alarm-banner")).to_contain_text("PLC communication lost", timeout=10_000)


def test_viewer_is_read_only(page, edge):
    sign_in(page, edge.url, "view")
    page.goto(f"{edge.url}/?asset={DEMO}#setpoints")
    expect(page.locator("#readonly-strip")).to_be_visible()
    expect(page.locator("#mode-switch button").first).to_be_disabled()
    expect(page.locator(".tabs [data-tab=commission]")).to_be_hidden()
    expect(page.locator(".tabs [data-tab=users]")).to_be_hidden()


def test_phone_layout_has_no_sideways_scroll(browser, edge):
    ctx = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
    page = ctx.new_page()
    sign_in(page, edge.url, "integ")
    for tab in ("overview", "io", "setpoints", "trends", "alarms", "commission"):
        page.goto(f"{edge.url}/?asset={DEMO}#{tab}")
        page.wait_for_timeout(600)
        overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
        assert overflow <= 0, f"{tab} scrolls sideways by {overflow}px"
    ctx.close()


# -- cloud portal → package → edge -------------------------------------------------------

def _dialog_fill(page, values: dict[str, str]) -> None:
    for name, value in values.items():
        field = page.locator(f"dialog[open] #fd-{name}")
        if field.evaluate("e => e.tagName") == "SELECT":
            field.select_option(value)
        else:
            field.fill(value)
    page.click("dialog[open] .btn.primary")


def test_portal_setup_publish_and_load_on_device(page, portal, edge, tmp_path):
    sign_in(page, portal.url, "tom")
    page.click("[data-nav=clients]")
    page.click("text=New client")
    _dialog_fill(page, {"name": "Acme Water", "email": "ops@acme.example"})
    expect(page.locator(".page-head h1")).to_contain_text("Acme Water")
    page.click("button:text('Add site')")
    _dialog_fill(page, {"name": "North Pump Station"})
    page.click("button:text('Add device')")
    _dialog_fill(page, {"name": "IoT2050 MCC"})
    expect(page.locator("button:text('Publish new version')")).to_be_disabled()
    page.locator(".crumbs a").last.click()
    page.click("button:text('Add asset')")
    _dialog_fill(page, {"name": "Pump 1"})
    expect(page.locator(".page-head h1")).to_contain_text("Pump 1")

    # PLC: Siemens S7 with rack/slot
    plc = page.locator(".panel", has=page.locator("h2", has_text="PLC connection"))
    plc.locator("select").select_option("s7")
    plc.locator("input.mono").fill("127.0.0.1")
    page.click("button:text('Save PLC settings')")
    expect(page.locator("#toasts")).to_contain_text("PLC settings saved")

    # I/O from CSV (S7 addresses), then choose the Auto/Manual point
    rows = [["name", "kind", "type", "description", "s7_address", "s7_type", "min", "max", "writable", "manual_only"],
            ["InAuto", "SP", "bool", "Auto mode", "%M0.0", "", "", "", "yes", ""],
            ["Run", "DO", "bool", "Run command", "%M0.1", "", "", "", "yes", "yes"],
            ["Level", "AI", "float", "Wet well level", "%DB1.DBD0", "real", "0", "100", "", ""]]
    out = io.StringIO()
    csv.writer(out).writerows(rows)
    path = tmp_path / "io.csv"
    path.write_text(out.getvalue())
    page.locator(".panel", has=page.locator("h2", has_text="I/O points")).locator("input[type=file]").first.set_input_files(str(path))
    _dialog_fill(page, {"mode": "replace"})
    expect(page.locator("#toasts")).to_contain_text("Import rejected")   # manual-only needs a mode tag first
    _import_rows = [r for r in rows if r[0] != "Run"]
    out = io.StringIO()
    csv.writer(out).writerows(_import_rows)
    path.write_text(out.getvalue())
    page.locator(".panel", has=page.locator("h2", has_text="I/O points")).locator("input[type=file]").first.set_input_files(str(path))
    _dialog_fill(page, {"mode": "replace"})
    expect(page.locator(".panel h2", has_text="I/O points")).to_contain_text("(2)")
    expect(page.locator("td.mono", has_text="%DB1.DBD0 REAL")).to_be_visible()
    details = page.locator(".panel", has=page.locator("h2", has_text="Asset details"))
    details.locator("select").nth(2).select_option("InAuto")
    page.click("button:text('Save details')")
    expect(page.locator("#toasts")).to_contain_text("Asset saved")

    # Client user with operator access
    page.locator(".crumbs a").nth(1).click()
    page.click("button:text('Add user')")
    _dialog_fill(page, {"username": "olly", "password": "olly-temp-pass"})
    expect(page.locator("dialog[open] h3")).to_have_text("Site access for olly")
    page.locator("dialog[open] select").select_option("operator")  # the client's only site
    page.click("dialog[open] .btn.primary")
    expect(page.locator("tr", has_text="olly")).to_contain_text("North Pump Station: Operator")

    # Publish and download
    page.locator(".panel", has=page.locator("h2", has_text="Devices")).locator("a").first.click()
    page.click("button:text('Publish new version')")
    _dialog_fill(page, {"notes": "e2e"})
    expect(page.locator(".pill", has_text="v1 · up to date")).to_be_visible()
    with page.expect_download() as dl:
        page.click("a:text('Download')")
    pkg_path = tmp_path / "pkg.json"
    dl.value.save_as(pkg_path)
    pkg = json.loads(pkg_path.read_text())
    assert pkg["assets"][0]["mode_tag"] == "InAuto" and pkg["assets"][0]["plc"]["protocol"] == "s7"
    assert {u["username"]: u["role"] for u in pkg["users"]} == {"tom": "integrator", "olly": "operator"}

    # Load it on the device from the Commissioning tab
    sign_in(page, edge.url, "integ")
    page.goto(f"{edge.url}/?asset={DEMO}#commission")
    page.locator("#package-file").set_input_files(str(pkg_path))
    page.click("#confirm-ok")
    page.wait_for_url("**/login**")
    sign_in(page, edge.url, "tom")  # portal staff are integrators on the device
    expect(page.locator("#plant-name")).to_have_text("Pump 1")
    expect(page.locator("#plant-desc")).to_contain_text("Acme Water · North Pump Station")


def test_tia_import_in_portal(page, portal, tmp_path):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["Name", "Path", "Data Type", "Logical Address", "Comment", "Hmi Visible", "Hmi Accessible", "Hmi Writeable"])
    ws.append(["Pump_Run", "Tags", "Bool", "%I0.0", "Pump running", True, True, False])
    ws.append(["Level", "Tags", "Int", "%IW64", "Level raw", True, True, False])
    ws.append(["Recipe", "Tags", "String", "%MB100", "", True, True, False])
    path = tmp_path / "tags.xlsx"
    wb.save(path)

    sign_in(page, portal.url, "tom")
    page.click("[data-nav=clients]")
    page.click("text=New client")
    _dialog_fill(page, {"name": "Beta"})
    page.click("button:text('Add site')")
    _dialog_fill(page, {"name": "Plant"})
    page.click("button:text('Add asset')")
    _dialog_fill(page, {"name": "Line 1"})
    page.locator("label:has-text('Import TIA tags') input").set_input_files(str(path))
    _dialog_fill(page, {"mode": "replace"})
    expect(page.locator("#confirm-title")).to_have_text("1 tag(s) not imported")
    expect(page.locator("#confirm-body")).to_contain_text("Recipe")
    page.click("#confirm-ok")
    expect(page.locator(".panel h2", has_text="I/O points")).to_contain_text("(2)")
    expect(page.locator("td.mono", has_text="%IW64 INT")).to_be_visible()


def test_fresh_device_setup_screen(browser, tmp_path, monkeypatch):
    """A new device shows the setup screen and accepts a package from the browser."""
    from src.hmi.package import demo_package
    from src.hmi.server import create_app, open_store

    from .conftest import ServerThread

    pw_file = tmp_path / "pw"
    monkeypatch.setenv("HMI_INITIAL_PASSWORD_FILE", str(pw_file))
    monkeypatch.delenv("HMI_DEMO", raising=False)
    monkeypatch.delenv("HMI_PACKAGE", raising=False)
    store = open_store(str(tmp_path / "fresh.db"))
    with ServerThread(create_app(store)) as srv:
        password = pw_file.read_text().split("password: ")[1].strip()
        page = browser.new_page()
        sign_in(page, srv.url, "admin", password)
        page.fill("#pw-current", password)
        page.fill("#pw-new", PW)
        page.fill("#pw-repeat", PW)
        page.click("#pw-form .btn.primary")
        expect(page.locator("#pw-dialog")).to_be_hidden()
        page.reload()
        expect(page.locator(".setup-panel h2")).to_have_text("This device isn't configured yet")
        pkg = tmp_path / "pkg.json"
        pkg.write_text(json.dumps(demo_package()))
        page.locator(".setup-panel input[type=file]").set_input_files(str(pkg))
        page.click("#confirm-ok")
        page.wait_for_url("**/login**")
        page.close()
