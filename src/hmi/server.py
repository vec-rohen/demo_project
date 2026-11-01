"""FastAPI web server for an edge device: REST for metadata and actions,
WebSockets for live data.

Every page and API call except the sign-in page requires a session. Role
checks are enforced here; the browser only mirrors them to grey out controls.

Run with ``python -m src.hmi`` (see ``__main__.py`` for options).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .auth import ROLES, AuthError, SessionManager, User, UserStore, role_at_least
from .package import PackageError, apply_package, demo_package, export_package, validate_asset_io, validate_plc
from .runtime import Runtime, WriteRejected
from .site import SiteManager
from .store import Store
from .tags import TagError

HERE = Path(__file__).parent
STATIC = HERE / "static"
COOKIE = "hmi_session"
CHECK_STATUSES = ("pass", "fail", "na")
log = logging.getLogger("hmi")


class WriteRequest(BaseModel):
    value: Any


class AckRequest(BaseModel):
    id: Optional[str] = None


class LoginRequest(BaseModel):
    username: str
    password: str


class PasswordChange(BaseModel):
    current: str
    new: str


class NewUser(BaseModel):
    username: str
    password: str
    role: str = "operator"
    full_name: str = ""


class UserUpdate(BaseModel):
    role: Optional[str] = None
    full_name: Optional[str] = None
    disabled: Optional[bool] = None
    password: Optional[str] = None


class IOCheck(BaseModel):
    status: Optional[str]  # pass | fail | na, or null to clear
    note: str = ""


def open_store(path: str | None = None) -> Store:
    """Open the device database. When it is empty, load ``HMI_PACKAGE`` if given,
    the demo plant if ``HMI_DEMO=1``, or otherwise start unconfigured and wait
    for an integrator to load a package from the browser."""
    store = Store(path or os.environ.get("HMI_DB", "data/edge.db"))
    if store.is_empty():
        package_file = os.environ.get("HMI_PACKAGE")
        if package_file:
            pkg = json.loads(Path(package_file).read_text(encoding="utf-8"))
            apply_package(store, pkg, "first start", source="file")
        elif os.environ.get("HMI_DEMO") == "1":
            apply_package(store, demo_package(), "first start", source="demo")
    return store


def create_app(store: Store | None = None, *, site: SiteManager | None = None,
               idle_timeout: float | None = None, secure_cookies: bool | None = None) -> FastAPI:
    store = store or open_store()
    site = site or SiteManager(store)
    users = UserStore(store)
    if idle_timeout is None:
        minutes = float(os.environ.get("HMI_IDLE_MINUTES", "30"))
        idle_timeout = minutes * 60 if minutes > 0 else float("inf")
    if secure_cookies is None:
        secure_cookies = os.environ.get("HMI_SECURE_COOKIES", "0") == "1"
    sessions = SessionManager(users, idle_timeout=idle_timeout)

    # On a service install the console goes to the system journal, so the
    # one-time password is also written to a root-only file the installer shows.
    password_file = os.environ.get("HMI_INITIAL_PASSWORD_FILE")
    first_password = users.bootstrap()
    if first_password:
        banner = (f"\n{'=' * 64}\n  No user accounts found – created integrator account\n"
                  f"    username: admin\n    password: {first_password}\n"
                  f"  You will be asked to change it at first sign-in.\n{'=' * 64}\n")
        print(banner, flush=True)
        if password_file:
            Path(password_file).parent.mkdir(parents=True, exist_ok=True)
            Path(password_file).write_text(f"username: admin\npassword: {first_password}\n", encoding="utf-8")
            os.chmod(password_file, 0o600)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await site.start()
        try:
            yield
        finally:
            await site.stop()

    app = FastAPI(title="PLC Web HMI", lifespan=lifespan)
    app.state.store = store
    app.state.site = site
    app.state.users = users
    app.state.sessions = sessions

    @app.middleware("http")
    async def json_only_writes(request: Request, call_next):
        # A cross-site form can only POST urlencoded/multipart/text bodies, and a
        # JSON POST (or any PUT/PATCH/DELETE) from another site needs a CORS
        # preflight that we never allow, so this blocks CSRF on top of SameSite.
        if request.method == "POST" and request.url.path.startswith("/api/"):
            if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
                return JSONResponse({"detail": "Content-Type must be application/json"}, status_code=415)
        return await call_next(request)

    # -- auth dependencies ------------------------------------------------------
    def current_user(request: Request) -> User:
        user = sessions.resolve(request.cookies.get(COOKIE))
        if user is None:
            raise HTTPException(401, "Sign-in required")
        return user

    def require(role: str, allow_pending_password: bool = False) -> Callable[..., User]:
        def check(user: User = Depends(current_user)) -> User:
            if user.must_change_password and not allow_pending_password:
                raise HTTPException(403, "Change your password before making changes")
            if not role_at_least(user.role, role):
                raise HTTPException(403, f"{role.capitalize()} role required")
            return user
        return check

    viewer = require("viewer", allow_pending_password=True)
    operator = require("operator")
    engineer = require("engineer")
    integrator = require("integrator")

    def runtime_for(asset_id: str) -> Runtime:
        try:
            return site.runtime(asset_id)
        except KeyError:
            raise HTTPException(404, f"Unknown asset {asset_id!r}") from None

    def asset_or_404(asset_id: str) -> dict[str, Any]:
        asset = store.asset(asset_id)
        if asset is None:
            raise HTTPException(404, f"Unknown asset {asset_id!r}")
        return asset

    # -- sign-in --------------------------------------------------------------
    @app.post("/api/login")
    def login(body: LoginRequest, response: Response) -> dict[str, Any]:
        try:
            session, user = sessions.login(body.username, body.password)
        except AuthError as exc:
            raise HTTPException(401, str(exc)) from None
        response.set_cookie(COOKIE, session.token, httponly=True, samesite="strict",
                            secure=secure_cookies, path="/")
        site.events.add("auth", f"{user.username} signed in", user=user.username)
        return user.public()

    @app.post("/api/logout")
    def logout(request: Request, response: Response) -> dict[str, bool]:
        token = request.cookies.get(COOKIE)
        user = sessions.resolve(token, touch=False)
        sessions.logout(token)
        response.delete_cookie(COOKIE, path="/")
        if user:
            site.events.add("auth", f"{user.username} signed out", user=user.username)
        return {"ok": True}

    @app.get("/api/me")
    def me(user: User = Depends(viewer)) -> dict[str, Any]:
        return {**user.public(), "roles": ROLES,
                "idle_timeout": None if idle_timeout == float("inf") else idle_timeout}

    @app.post("/api/me/ping")
    def ping(user: User = Depends(viewer)) -> dict[str, bool]:
        """Called by the browser on operator activity to keep the session alive."""
        return {"ok": True}

    @app.post("/api/me/password")
    def change_password(body: PasswordChange, request: Request, user: User = Depends(viewer)) -> dict[str, bool]:
        try:
            users.change_own_password(user.username, body.current, body.new)
        except AuthError as exc:
            raise HTTPException(422, str(exc)) from None
        sessions.end_user_sessions(user.username, keep=request.cookies.get(COOKIE))
        store.log_change(user.username, None, "user.password", user.username, None,
                         {"password_hash": users.get(user.username).password_hash})
        if password_file and user.username == "admin" and Path(password_file).exists():
            Path(password_file).unlink()  # the one-time password is no longer valid
        site.events.add("auth", f"{user.username} changed their password", user=user.username)
        return {"ok": True}

    # -- user administration (engineers and integrators) -------------------------
    def _can_manage(admin: User, target_role: str) -> None:
        if not role_at_least(admin.role, target_role):
            raise HTTPException(403, f"Only an {target_role} or higher can manage {target_role} accounts")

    def _user_record(user: User) -> dict[str, Any]:
        return asdict(user)  # includes the hash so the cloud can sync it

    @app.get("/api/users")
    def list_users(_: User = Depends(engineer)) -> list[dict[str, Any]]:
        return [u.public() for u in sorted(users.users.values(), key=lambda u: u.username)]

    @app.post("/api/users", status_code=201)
    def add_user(body: NewUser, admin: User = Depends(engineer)) -> dict[str, Any]:
        if body.role in ROLES:
            _can_manage(admin, body.role)
        try:
            user = users.add(body.username, body.password, body.role, body.full_name)
        except AuthError as exc:
            raise HTTPException(422, str(exc)) from None
        store.log_change(admin.username, None, "user", user.username, None, _user_record(user))
        site.events.add("auth", f"User {user.username} created ({user.role})", user=admin.username)
        return user.public()

    @app.patch("/api/users/{username}")
    def update_user(username: str, body: UserUpdate, admin: User = Depends(engineer)) -> dict[str, Any]:
        if username == admin.username and (body.role not in (None, admin.role) or body.disabled):
            raise HTTPException(422, "You can't change your own role or disable yourself")
        target = users.users.get(username)
        if target is None:
            raise HTTPException(404, f"Unknown user {username!r}")
        _can_manage(admin, target.role)
        if body.role in ROLES:
            _can_manage(admin, body.role)
        before = _user_record(target)
        try:
            user = users.update(username, role=body.role, full_name=body.full_name,
                                disabled=body.disabled, password=body.password)
        except AuthError as exc:
            raise HTTPException(422, str(exc)) from None
        changes = []
        if body.role is not None:
            changes.append(f"role → {body.role}")
        if body.disabled is not None:
            changes.append("disabled" if body.disabled else "enabled")
        if body.password is not None:
            changes.append("password reset")
        if body.full_name is not None:
            changes.append("name updated")
        if body.role is not None or body.disabled or body.password is not None:
            sessions.end_user_sessions(username)  # take effect immediately
        store.log_change(admin.username, None, "user", username, before, _user_record(user))
        site.events.add("auth", f"User {username}: {', '.join(changes) or 'no change'}", user=admin.username)
        return user.public()

    @app.delete("/api/users/{username}")
    def delete_user(username: str, admin: User = Depends(engineer)) -> dict[str, bool]:
        if username == admin.username:
            raise HTTPException(422, "You can't delete your own account")
        target = users.users.get(username)
        if target is None:
            raise HTTPException(404, f"Unknown user {username!r}")
        _can_manage(admin, target.role)
        before = _user_record(target)
        try:
            users.delete(username)
        except AuthError as exc:
            raise HTTPException(422, str(exc)) from None
        sessions.end_user_sessions(username)
        store.log_change(admin.username, None, "user", username, before, None)
        site.events.add("auth", f"User {username} deleted", user=admin.username)
        return {"ok": True}

    # -- site, device and configuration package -------------------------------
    @app.get("/api/site")
    def site_info(_: User = Depends(viewer)) -> dict[str, Any]:
        return site.info()

    @app.get("/api/package")
    def download_package(_: User = Depends(integrator)) -> JSONResponse:
        pkg = export_package(store)
        name = f"{pkg['site']['id']}_{pkg['device']['id']}_package.json"
        return JSONResponse(pkg, headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.post("/api/package")
    async def upload_package(request: Request, user: User = Depends(integrator)) -> dict[str, Any]:
        try:
            pkg = await request.json()
            info = apply_package(store, pkg, user.username, source="upload")
        except (PackageError, ValueError) as exc:
            raise HTTPException(422, f"Package rejected: {exc}") from None
        users.reload()
        for name in list(users.users):  # accounts may have changed: start everyone afresh
            sessions.end_user_sessions(name)
        await site.reload_all()
        site.events.add("system", f"Configuration package v{info['version']} loaded", user=user.username)
        return {"ok": True, "package": info}

    @app.get("/api/changes")
    def change_log(_: User = Depends(engineer)) -> list[dict[str, Any]]:
        # Never send password hashes to the browser.
        out = []
        for change in store.changes():
            for side in ("before", "after"):
                if isinstance(change[side], dict):
                    change[side] = {k: v for k, v in change[side].items() if k != "password_hash"}
            out.append(change)
        return out

    # -- asset data -----------------------------------------------------------------
    @app.get("/api/assets/{asset_id}/config")
    def config(asset_id: str, _: User = Depends(viewer)) -> dict[str, Any]:
        runtime = runtime_for(asset_id)
        asset = asset_or_404(asset_id)
        return {
            "asset": {k: asset.get(k) for k in ("id", "name", "description", "screen", "mode_tag")},
            "plant": runtime.db.plant,
            "driver": type(runtime.driver).__name__,
            "simulated_inputs": bool(getattr(runtime.driver, "supports_simulated_inputs", False)),
            "tags": [t.public() for t in runtime.db.tags.values()],
        }

    @app.get("/api/assets/{asset_id}/state")
    def state(asset_id: str, _: User = Depends(viewer)) -> dict[str, Any]:
        return runtime_for(asset_id).snapshot()

    @app.get("/api/assets/{asset_id}/history")
    def history(asset_id: str, tags: Optional[str] = None, seconds: float = 900,
                _: User = Depends(viewer)) -> dict[str, list[tuple[float, float]]]:
        runtime = runtime_for(asset_id)
        names = tags.split(",") if tags else list(runtime.history)
        unknown = [n for n in names if n not in runtime.history]
        if unknown:
            raise HTTPException(404, f"no history for {', '.join(unknown)}")
        seconds = min(max(seconds, 10), 31 * 86400)
        return {n: runtime.trend(n, seconds) for n in names}

    @app.post("/api/assets/{asset_id}/tags/{name}")
    async def write_tag(asset_id: str, name: str, body: WriteRequest,
                        user: User = Depends(operator)) -> dict[str, Any]:
        runtime = runtime_for(asset_id)
        try:
            tag = runtime.db.get(name)
        except TagError as exc:
            raise HTTPException(404, str(exc)) from None
        if not role_at_least(user.role, tag.role):
            raise HTTPException(403, f"{tag.role.capitalize()} role required to change {tag.description}")
        try:
            value = await runtime.write(name, body.value, user.username)
        except TagError as exc:
            raise HTTPException(422, str(exc)) from None
        except WriteRejected as exc:
            raise HTTPException(409, str(exc)) from None
        except IOError as exc:
            raise HTTPException(502, f"PLC write failed: {exc}") from None
        return {"name": name, "value": value}

    @app.post("/api/assets/{asset_id}/simulate/{name}")
    async def simulate(asset_id: str, name: str, body: WriteRequest,
                       user: User = Depends(engineer)) -> dict[str, Any]:
        runtime = runtime_for(asset_id)
        try:
            value = await runtime.simulate_input(name, body.value, user.username)
        except TagError as exc:
            raise HTTPException(422, str(exc)) from None
        except WriteRejected as exc:
            raise HTTPException(409, str(exc)) from None
        return {"name": name, "value": value}

    @app.post("/api/assets/{asset_id}/alarms/ack")
    def ack(asset_id: str, body: AckRequest, user: User = Depends(operator)) -> dict[str, int]:
        runtime = runtime_for(asset_id)
        if body.id is not None and body.id not in runtime.alarms.alarms:
            raise HTTPException(404, f"unknown alarm {body.id!r}")
        return {"acknowledged": runtime.alarms.acknowledge(body.id, user.username)}

    # -- commissioning ------------------------------------------------------------
    @app.get("/api/assets/{asset_id}/plc")
    def get_plc(asset_id: str, _: User = Depends(engineer)) -> dict[str, Any]:
        return asset_or_404(asset_id)["plc"]

    @app.put("/api/assets/{asset_id}/plc")
    async def put_plc(asset_id: str, body: dict[str, Any], user: User = Depends(integrator)) -> dict[str, Any]:
        asset = asset_or_404(asset_id)
        try:
            plc = validate_plc(body)
        except PackageError as exc:
            raise HTTPException(422, str(exc)) from None
        before = asset["plc"]
        store.save_asset({**asset, "plc": plc})
        store.log_change(user.username, asset_id, "plc", asset_id, before, plc)
        await site.reload_asset(asset_id)
        target = "the simulator" if plc["protocol"] == "sim" else f"{plc['protocol']} {plc['host']}:{plc['port']}"
        site.events.add("config", f"{asset['name']}: PLC connection set to {target}",
                        asset=asset_id, user=user.username)
        return plc

    @app.post("/api/assets/{asset_id}/plc/test")
    async def test_plc(asset_id: str, body: dict[str, Any], user: User = Depends(engineer)) -> dict[str, Any]:
        asset = asset_or_404(asset_id)
        try:
            plc = validate_plc(body or asset["plc"])
        except PackageError as exc:
            raise HTTPException(422, str(exc)) from None
        result = await site.test_connection(asset_id, plc)
        outcome = "passed" if result["ok"] else "failed"
        site.events.add("commission", f"{asset['name']}: PLC connection test {outcome}",
                        asset=asset_id, user=user.username)
        return result

    @app.get("/api/assets/{asset_id}/io")
    def io_list(asset_id: str, _: User = Depends(engineer)) -> dict[str, Any]:
        asset_or_404(asset_id)
        return {"tags": store.tags(asset_id), "checks": store.io_checks(asset_id)}

    @app.put("/api/assets/{asset_id}/io/{name}/check")
    def io_check(asset_id: str, name: str, body: IOCheck, user: User = Depends(engineer)) -> dict[str, Any]:
        asset = asset_or_404(asset_id)
        if name not in {t["name"] for t in store.tags(asset_id)}:
            raise HTTPException(404, f"Unknown tag {name!r}")
        if body.status is not None and body.status not in CHECK_STATUSES:
            raise HTTPException(422, f"status must be one of {', '.join(CHECK_STATUSES)} or null")
        note = body.note.strip()[:500]
        store.set_io_check(asset_id, name, body.status, note, user.username)
        label = {"pass": "passed", "fail": "FAILED", "na": "marked N/A", None: "check cleared"}[body.status]
        site.events.add("commission", f"{asset['name']}: I/O {name} {label}" + (f" – {note}" if note else ""),
                        asset=asset_id, tag=name, user=user.username)
        return store.io_checks(asset_id).get(name, {"status": None})

    @app.put("/api/assets/{asset_id}/tags/{name}/definition")
    async def put_tag(asset_id: str, name: str, body: dict[str, Any],
                      user: User = Depends(integrator)) -> dict[str, Any]:
        asset = asset_or_404(asset_id)
        if body.get("name") != name:
            raise HTTPException(422, "The tag name in the body must match the URL")
        current = {t["name"]: t for t in store.tags(asset_id)}
        before = current.get(name)
        current[name] = body
        try:
            validate_asset_io(list(current.values()), asset.get("mode_tag"))  # the whole asset
        except TagError as exc:
            raise HTTPException(422, str(exc)) from None
        store.save_tag(asset_id, body)
        store.log_change(user.username, asset_id, "tag", name, before, body)
        store.set_io_check(asset_id, name, None, "", user.username)  # changed: needs checking again
        await site.reload_asset(asset_id)
        site.events.add("config", f"{asset['name']}: I/O {name} {'updated' if before else 'added'}",
                        asset=asset_id, tag=name, user=user.username)
        return body

    @app.delete("/api/assets/{asset_id}/tags/{name}")
    async def delete_tag(asset_id: str, name: str, user: User = Depends(integrator)) -> dict[str, bool]:
        asset = asset_or_404(asset_id)
        current = {t["name"]: t for t in store.tags(asset_id)}
        if name not in current:
            raise HTTPException(404, f"Unknown tag {name!r}")
        before = current.pop(name)
        try:
            validate_asset_io(list(current.values()), asset.get("mode_tag"))
        except TagError as exc:
            raise HTTPException(422, f"Can't delete {name}: {exc}") from None
        store.delete_tag(asset_id, name)
        store.log_change(user.username, asset_id, "tag", name, before, None)
        await site.reload_asset(asset_id)
        site.events.add("config", f"{asset['name']}: I/O {name} deleted", asset=asset_id, tag=name,
                        user=user.username)
        return {"ok": True}

    # -- live data ---------------------------------------------------------------------
    @app.websocket("/ws/{asset_id}")
    async def live(ws: WebSocket, asset_id: str) -> None:
        token = ws.cookies.get(COOKIE)
        if sessions.resolve(token, touch=False) is None:
            await ws.close(code=4401)
            return
        if asset_id not in site.runtimes:
            await ws.close(code=4404)
            return
        await ws.accept()
        runtime: Runtime | None = None
        queue: asyncio.Queue | None = None
        try:
            while True:
                if sessions.resolve(token, touch=False) is None:  # expired, signed out or disabled
                    await ws.close(code=4401)
                    break
                current = site.runtimes.get(asset_id)
                if current is not runtime:  # asset reloaded after a config change
                    if runtime and queue:
                        runtime.unsubscribe(queue)
                    runtime, queue = current, (current.subscribe() if current else None)
                    if runtime:
                        await ws.send_json(runtime.snapshot())  # full state incl. the journal
                if queue is None:
                    await asyncio.sleep(0.5)
                    continue
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                await ws.send_json(message)
        except (WebSocketDisconnect, asyncio.CancelledError, RuntimeError):
            pass
        finally:
            if runtime and queue:
                runtime.unsubscribe(queue)

    # -- pages --------------------------------------------------------------
    @app.get("/")
    def index(request: Request):
        if sessions.resolve(request.cookies.get(COOKIE), touch=False) is None:
            return RedirectResponse("/login", status_code=303)
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/login")
    def login_page(request: Request):
        if sessions.resolve(request.cookies.get(COOKIE), touch=False) is not None:
            return RedirectResponse("/", status_code=303)
        return FileResponse(STATIC / "login.html", headers={"Cache-Control": "no-store"})

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
