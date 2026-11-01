"""Browser end-to-end tests: real edge and portal servers, driven with Playwright.

Run:   python -m pytest tests/e2e
Needs: pip install playwright && python -m playwright install chromium
       (or set PW_CHROMIUM to an existing Chromium/Chrome executable)
"""

from __future__ import annotations

import os
import socket
import threading
import time

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")

import uvicorn  # noqa: E402

from src.cloud.db import CloudDB  # noqa: E402
from src.cloud.server import create_app as create_portal  # noqa: E402
from src.cloud.service import CloudService  # noqa: E402
from src.hmi import runtime as runtime_mod  # noqa: E402
from src.hmi.auth import UserStore  # noqa: E402
from src.hmi.drivers import SimulatorDriver, driver_for_plc  # noqa: E402
from src.hmi.package import apply_package, demo_package  # noqa: E402
from src.hmi.server import create_app as create_edge  # noqa: E402
from src.hmi.site import SiteManager  # noqa: E402
from src.hmi.store import Store  # noqa: E402

PW = "e2e-pass-123"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ServerThread:
    """Runs an ASGI app with uvicorn on a background thread."""

    def __init__(self, app):
        self.port = free_port()
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self):
        self.thread.start()
        deadline = time.time() + 15
        while not self.server.started:
            if time.time() > deadline:
                raise RuntimeError("server didn't start")
            time.sleep(0.05)
        return self

    def __exit__(self, *_):
        self.server.should_exit = True
        self.thread.join(timeout=10)


@pytest.fixture(scope="module")  # not session: Playwright's event loop must be gone before other tests run
def browser():
    with playwright_api.sync_playwright() as p:
        exe = os.environ.get("PW_CHROMIUM")
        try:
            b = p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()
        except Exception as exc:  # noqa: BLE001
            # Locally, no browser just means skip; CI sets E2E_REQUIRED so it can't silently pass.
            if os.environ.get("E2E_REQUIRED"):
                raise
            pytest.skip(f"no browser for end-to-end tests ({exc.__class__.__name__}); "
                        "run 'python -m playwright install chromium'")
        yield b
        b.close()


@pytest.fixture
def page(browser):
    ctx = browser.new_context(viewport={"width": 1366, "height": 900}, accept_downloads=True)
    pg = ctx.new_page()
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.js_errors = errors  # type: ignore[attr-defined]
    yield pg
    ctx.close()
    assert not errors, f"JavaScript errors in the page: {errors}"


@pytest.fixture
def edge(tmp_path, monkeypatch):
    """Edge device with the demo plant, an offline Modbus asset, and one user per role."""
    monkeypatch.setattr(runtime_mod, "COMMS_ALARM_DELAY_S", 1)
    store = Store(tmp_path / "edge.db")
    pkg = demo_package()
    pkg["assets"].append({
        "id": "OFFLINE", "name": "Offline pump", "plc": {"protocol": "modbus", "host": "127.0.0.1", "port": 1, "poll_ms": 500},
        "tags": [{"name": "Flow", "kind": "AI", "type": "float", "min": 0, "max": 50, "unit": "L/s",
                  "modbus": {"table": "input", "address": 0, "type": "float32"}}]})
    apply_package(store, pkg, "e2e")
    users = UserStore(store)
    for name, role in (("integ", "integrator"), ("eng", "engineer"), ("op", "operator"), ("view", "viewer")):
        users.add(name, PW, role, must_change_password=False)

    def factory(plc, db):
        return SimulatorDriver(db, seed=1) if plc["protocol"] == "sim" else driver_for_plc(plc, db)

    app = create_edge(store, site=SiteManager(store, driver_factory=factory))
    with ServerThread(app) as srv:
        yield srv


@pytest.fixture
def portal(tmp_path):
    db = CloudDB(tmp_path / "cloud.db")
    CloudService(db).create_user({"username": "tom", "password": PW, "kind": "staff", "portal_role": "admin",
                                  "full_name": "Tom Tech"}, "e2e")
    db.run("UPDATE users SET must_change_password = 0")
    with ServerThread(create_portal(db)) as srv:
        yield srv


def sign_in(page, base: str, user: str, password: str = PW) -> None:
    page.goto(base + "/login")
    page.fill("#username", user)
    page.fill("#password", password)
    page.click("#submit")
    page.wait_for_url(base + "/")
