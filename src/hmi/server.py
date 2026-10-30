"""FastAPI web server: REST for metadata and operator actions, WebSocket for live data.

Every page and API call except the sign-in page requires a session. Role
checks are enforced here; the browser only mirrors them to grey out controls.

Run with ``python -m src.hmi`` (see ``__main__.py`` for options).
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable

from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .auth import ROLES, AuthError, SessionManager, User, UserStore, role_at_least
from .drivers import create_driver
from .runtime import Runtime, WriteRejected
from .tags import TagDatabase, TagError

HERE = Path(__file__).parent
STATIC = HERE / "static"
COOKIE = "hmi_session"
log = logging.getLogger("hmi")


class WriteRequest(BaseModel):
    value: Any


class AckRequest(BaseModel):
    id: str | None = None


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
    role: str | None = None
    full_name: str | None = None
    disabled: bool | None = None
    password: str | None = None


def _default_runtime() -> Runtime:
    db = TagDatabase.load(os.environ.get("HMI_TAGS", HERE / "tags.json"))
    options = {}
    if os.environ.get("HMI_DRIVER") == "modbus":
        options = {
            "host": os.environ.get("HMI_MODBUS_HOST", "127.0.0.1"),
            "port": int(os.environ.get("HMI_MODBUS_PORT", "502")),
            "unit": int(os.environ.get("HMI_MODBUS_UNIT", "1")),
        }
    driver = create_driver(os.environ.get("HMI_DRIVER", "sim"), db, **options)
    return Runtime(db, driver, poll_interval=float(os.environ.get("HMI_POLL_S", "0.25")))


def create_app(runtime: Runtime | None = None, users: UserStore | None = None,
               idle_timeout: float | None = None, secure_cookies: bool | None = None) -> FastAPI:
    runtime = runtime or _default_runtime()
    users = users or UserStore(os.environ.get("HMI_USERS", "data/hmi_users.json"))
    if idle_timeout is None:
        minutes = float(os.environ.get("HMI_IDLE_MINUTES", "30"))
        idle_timeout = minutes * 60 if minutes > 0 else float("inf")
    if secure_cookies is None:
        secure_cookies = os.environ.get("HMI_SECURE_COOKIES", "0") == "1"
    sessions = SessionManager(users, idle_timeout=idle_timeout)

    first_password = users.bootstrap()
    if first_password:
        banner = (f"\n{'=' * 64}\n  No user accounts found – created engineer account\n"
                  f"    username: admin\n    password: {first_password}\n"
                  f"  You will be asked to change it at first sign-in.\n{'=' * 64}\n")
        print(banner, flush=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(title="PLC Web HMI", lifespan=lifespan)
    app.state.runtime = runtime
    app.state.users = users
    app.state.sessions = sessions

    @app.middleware("http")
    async def json_only_writes(request: Request, call_next):
        # A cross-site form can only POST urlencoded/multipart/text bodies, and a
        # JSON POST (or any PATCH/DELETE) from another site needs a CORS preflight
        # that we never allow, so this blocks CSRF on top of the SameSite cookie.
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

    # -- sign-in --------------------------------------------------------------
    @app.post("/api/login")
    def login(body: LoginRequest, response: Response) -> dict[str, Any]:
        try:
            session, user = sessions.login(body.username, body.password)
        except AuthError as exc:
            raise HTTPException(401, str(exc)) from None
        response.set_cookie(COOKIE, session.token, httponly=True, samesite="strict",
                            secure=secure_cookies, path="/")
        runtime.events.add("auth", f"{user.username} signed in", user=user.username)
        return user.public()

    @app.post("/api/logout")
    def logout(request: Request, response: Response) -> dict[str, bool]:
        token = request.cookies.get(COOKIE)
        user = sessions.resolve(token, touch=False)
        sessions.logout(token)
        response.delete_cookie(COOKIE, path="/")
        if user:
            runtime.events.add("auth", f"{user.username} signed out", user=user.username)
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
        runtime.events.add("auth", f"{user.username} changed their password", user=user.username)
        return {"ok": True}

    # -- user administration (engineers) -----------------------------------
    @app.get("/api/users")
    def list_users(_: User = Depends(engineer)) -> list[dict[str, Any]]:
        return [u.public() for u in sorted(users.users.values(), key=lambda u: u.username)]

    @app.post("/api/users", status_code=201)
    def add_user(body: NewUser, admin: User = Depends(engineer)) -> dict[str, Any]:
        try:
            user = users.add(body.username, body.password, body.role, body.full_name)
        except AuthError as exc:
            raise HTTPException(422, str(exc)) from None
        runtime.events.add("auth", f"User {user.username} created ({user.role})", user=admin.username)
        return user.public()

    @app.patch("/api/users/{username}")
    def update_user(username: str, body: UserUpdate, admin: User = Depends(engineer)) -> dict[str, Any]:
        if username == admin.username and (body.role not in (None, admin.role) or body.disabled):
            raise HTTPException(422, "You can't change your own role or disable yourself")
        try:
            user = users.update(username, role=body.role, full_name=body.full_name,
                                disabled=body.disabled, password=body.password)
        except AuthError as exc:
            raise HTTPException(404 if "Unknown user" in str(exc) else 422, str(exc)) from None
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
        runtime.events.add("auth", f"User {username}: {', '.join(changes) or 'no change'}", user=admin.username)
        return user.public()

    @app.delete("/api/users/{username}")
    def delete_user(username: str, admin: User = Depends(engineer)) -> dict[str, bool]:
        if username == admin.username:
            raise HTTPException(422, "You can't delete your own account")
        try:
            users.delete(username)
        except AuthError as exc:
            raise HTTPException(404 if "Unknown user" in str(exc) else 422, str(exc)) from None
        sessions.end_user_sessions(username)
        runtime.events.add("auth", f"User {username} deleted", user=admin.username)
        return {"ok": True}

    # -- process data -------------------------------------------------------
    @app.get("/api/config")
    def config(_: User = Depends(viewer)) -> dict[str, Any]:
        return {
            "plant": runtime.db.plant,
            "driver": type(runtime.driver).__name__,
            "simulated_inputs": bool(getattr(runtime.driver, "supports_simulated_inputs", False)),
            "tags": [t.public() for t in runtime.db.tags.values()],
        }

    @app.get("/api/state")
    def state(_: User = Depends(viewer)) -> dict[str, Any]:
        return runtime.snapshot()

    @app.get("/api/history")
    def history(tags: str | None = None, _: User = Depends(viewer)) -> dict[str, list[tuple[float, float]]]:
        names = tags.split(",") if tags else list(runtime.history)
        unknown = [n for n in names if n not in runtime.history]
        if unknown:
            raise HTTPException(404, f"no history for {', '.join(unknown)}")
        return {n: list(runtime.history[n]) for n in names}

    @app.post("/api/tags/{name}")
    async def write_tag(name: str, body: WriteRequest, user: User = Depends(operator)) -> dict[str, Any]:
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

    @app.post("/api/simulate/{name}")
    async def simulate(name: str, body: WriteRequest, user: User = Depends(engineer)) -> dict[str, Any]:
        try:
            value = await runtime.simulate_input(name, body.value, user.username)
        except TagError as exc:
            raise HTTPException(422, str(exc)) from None
        except WriteRejected as exc:
            raise HTTPException(409, str(exc)) from None
        return {"name": name, "value": value}

    @app.post("/api/alarms/ack")
    def ack(body: AckRequest, user: User = Depends(operator)) -> dict[str, int]:
        if body.id is not None and body.id not in runtime.alarms.alarms:
            raise HTTPException(404, f"unknown alarm {body.id!r}")
        return {"acknowledged": runtime.alarms.acknowledge(body.id, user.username)}

    @app.websocket("/ws")
    async def live(ws: WebSocket) -> None:
        token = ws.cookies.get(COOKIE)
        if sessions.resolve(token, touch=False) is None:
            await ws.close(code=4401)
            return
        await ws.accept()
        queue = runtime.subscribe()
        try:
            await ws.send_json(runtime.snapshot())  # full state incl. every journal event
            while True:
                message = await queue.get()
                if sessions.resolve(token, touch=False) is None:  # expired, signed out or disabled
                    await ws.close(code=4401)
                    break
                await ws.send_json(message)
        except (WebSocketDisconnect, asyncio.CancelledError, RuntimeError):
            pass
        finally:
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
