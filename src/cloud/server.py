"""Cloud portal web server.

Your staff sign in here to set up clients, sites, IoT2050 devices, assets,
I/O and users, then publish a configuration package to each device.

Run with ``python -m src.cloud`` (see ``__main__.py``).
"""

from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from src.hmi.auth import AuthError, SessionManager, User, hash_password, verify_password, _DUMMY_HASH

from .db import CloudDB
from .service import CloudError, CloudService

HERE = Path(__file__).parent
EDGE_STATIC = HERE.parent / "hmi" / "static"
COOKIE = "portal_session"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class PortalAccounts:
    """Staff accounts for portal sign-in, shaped like the edge's UserStore so the
    same SessionManager (idle timeout, throttling) can be reused."""

    def __init__(self, service: CloudService):
        self.service = service
        self.users: dict[str, User] = {}
        self.reload_if_changed()

    def reload_if_changed(self) -> None:
        rows = self.service.db.q("SELECT * FROM users WHERE kind = 'staff'")
        self.users = {r["username"]: User(r["username"], r["portal_role"], r["password_hash"], r["full_name"],
                                          bool(r["must_change_password"]), bool(r["disabled"]),
                                          r["created_at"], r["last_login"]) for r in rows}

    def authenticate(self, username: str, password: str) -> User | None:
        self.reload_if_changed()
        user = self.users.get(username.strip().lower())
        if user is None:
            verify_password(password, _DUMMY_HASH)
            return None
        if not verify_password(password, user.password_hash) or user.disabled:
            return None
        self.service.db.run("UPDATE users SET last_login = ? WHERE username = ?", (time.time(), user.username))
        return user

    def change_own_password(self, username: str, current: str, new: str) -> None:
        user = self.users[username]
        if not verify_password(current, user.password_hash):
            raise AuthError("Current password is incorrect")
        if current == new:
            raise AuthError("New password must be different from the current one")
        if len(new) < 8:
            raise AuthError("Password must be at least 8 characters")
        self.service.db.run("UPDATE users SET password_hash = ?, must_change_password = 0 WHERE username = ?",
                            (hash_password(new), username))
        self.reload_if_changed()

    def bootstrap(self) -> str | None:
        if self.users:
            return None
        password = secrets.token_urlsafe(9)
        self.service.create_user({"username": "admin", "password": password, "kind": "staff",
                                  "portal_role": "admin", "full_name": "Administrator"}, "first start")
        self.reload_if_changed()
        return password


class LoginRequest(BaseModel):
    username: str
    password: str


class PasswordChange(BaseModel):
    current: str
    new: str


def create_app(db: CloudDB | None = None, idle_timeout: float | None = None,
               secure_cookies: bool | None = None) -> FastAPI:
    db = db or CloudDB(os.environ.get("PORTAL_DB", "data/cloud.db"))
    service = CloudService(db)
    accounts = PortalAccounts(service)
    if idle_timeout is None:
        minutes = float(os.environ.get("PORTAL_IDLE_MINUTES", "60"))
        idle_timeout = minutes * 60 if minutes > 0 else float("inf")
    if secure_cookies is None:
        secure_cookies = os.environ.get("PORTAL_SECURE_COOKIES", "0") == "1"
    sessions = SessionManager(accounts, idle_timeout=idle_timeout)

    first = accounts.bootstrap()
    if first:
        print(f"\n{'=' * 64}\n  Cloud portal: created admin account\n    username: admin\n"
              f"    password: {first}\n  You will be asked to change it at first sign-in.\n{'=' * 64}\n", flush=True)

    app = FastAPI(title="Plant Cloud Portal")
    app.state.service = service

    @app.middleware("http")
    async def json_only_writes(request: Request, call_next):
        if request.method == "POST" and request.url.path.startswith("/api/"):
            ctype = request.headers.get("content-type", "").split(";")[0].strip()
            # None of these can be sent cross-site without a CORS preflight.
            if ctype not in ("application/json", "text/csv", XLSX):
                return JSONResponse({"detail": "Content-Type must be application/json"}, status_code=415)
        return await call_next(request)

    @app.exception_handler(CloudError)
    async def cloud_error(_: Request, exc: CloudError):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    def current_user(request: Request) -> User:
        user = sessions.resolve(request.cookies.get(COOKIE))
        if user is None:
            raise HTTPException(401, "Sign-in required")
        return user

    def staff(allow_pending: bool = False) -> Callable[..., User]:
        def check(user: User = Depends(current_user)) -> User:
            if user.must_change_password and not allow_pending:
                raise HTTPException(403, "Change your password before making changes")
            return user
        return check

    def admin_only(user: User = Depends(staff())) -> User:
        if user.role != "admin":
            raise HTTPException(403, "Admin role required")
        return user

    signed_in = staff(allow_pending=True)
    tech = staff()

    # -- sign-in ----------------------------------------------------------------
    @app.post("/api/login")
    def login(body: LoginRequest, response: Response) -> dict[str, Any]:
        try:
            session, user = sessions.login(body.username, body.password)
        except AuthError as exc:
            raise HTTPException(401, str(exc)) from None
        response.set_cookie(COOKIE, session.token, httponly=True, samesite="strict", secure=secure_cookies, path="/")
        service.audit(user.username, "auth.login")
        return user.public()

    @app.post("/api/logout")
    def logout(request: Request, response: Response) -> dict[str, bool]:
        sessions.logout(request.cookies.get(COOKIE))
        response.delete_cookie(COOKIE, path="/")
        return {"ok": True}

    @app.get("/api/me")
    def me(user: User = Depends(signed_in)) -> dict[str, Any]:
        return {**user.public(), "idle_timeout": None if idle_timeout == float("inf") else idle_timeout}

    @app.post("/api/me/ping")
    def ping(_: User = Depends(signed_in)) -> dict[str, bool]:
        return {"ok": True}

    @app.post("/api/me/password")
    def change_password(body: PasswordChange, request: Request, user: User = Depends(signed_in)) -> dict[str, bool]:
        try:
            accounts.change_own_password(user.username, body.current, body.new)
        except AuthError as exc:
            raise HTTPException(422, str(exc)) from None
        sessions.end_user_sessions(user.username, keep=request.cookies.get(COOKIE))
        service.audit(user.username, "auth.password", user.username)
        return {"ok": True}

    # -- dashboard & audit ------------------------------------------------------
    @app.get("/api/overview")
    def overview(_: User = Depends(signed_in)) -> dict[str, Any]:
        return service.overview()

    @app.get("/api/audit")
    def audit(target: Optional[str] = None, _: User = Depends(signed_in)) -> list[dict[str, Any]]:
        return service.audit_log(500, target)

    # -- clients ------------------------------------------------------------------
    @app.get("/api/clients")
    def clients(_: User = Depends(signed_in)):
        return service.list_clients()

    @app.post("/api/clients", status_code=201)
    def create_client(body: dict[str, Any] = Body(...), user: User = Depends(tech)):
        return service.create_client(body, user.username)

    @app.get("/api/clients/{client_id}")
    def client(client_id: str, _: User = Depends(signed_in)):
        return service.client_detail(client_id)

    @app.patch("/api/clients/{client_id}")
    def update_client(client_id: str, body: dict[str, Any] = Body(...), user: User = Depends(tech)):
        return service.update_client(client_id, body, user.username)

    @app.delete("/api/clients/{client_id}")
    def delete_client(client_id: str, user: User = Depends(tech)):
        service.delete_client(client_id, user.username)
        return {"ok": True}

    # -- sites ---------------------------------------------------------------------
    @app.post("/api/clients/{client_id}/sites", status_code=201)
    def create_site(client_id: str, body: dict[str, Any] = Body(...), user: User = Depends(tech)):
        return service.create_site(client_id, body, user.username)

    @app.get("/api/sites/{site_id}")
    def site(site_id: str, _: User = Depends(signed_in)):
        return service.site_detail(site_id)

    @app.patch("/api/sites/{site_id}")
    def update_site(site_id: str, body: dict[str, Any] = Body(...), user: User = Depends(tech)):
        return service.update_site(site_id, body, user.username)

    @app.delete("/api/sites/{site_id}")
    def delete_site(site_id: str, user: User = Depends(tech)):
        service.delete_site(site_id, user.username)
        return {"ok": True}

    # -- devices -------------------------------------------------------------------
    @app.post("/api/clients/{client_id}/devices", status_code=201)
    def create_device(client_id: str, body: dict[str, Any] = Body(...), user: User = Depends(tech)):
        return service.create_device(client_id, body, user.username)

    @app.get("/api/devices/{device_id}")
    def device(device_id: str, _: User = Depends(signed_in)):
        return service.device_detail(device_id)

    @app.patch("/api/devices/{device_id}")
    def update_device(device_id: str, body: dict[str, Any] = Body(...), user: User = Depends(tech)):
        return service.update_device(device_id, body, user.username)

    @app.delete("/api/devices/{device_id}")
    def delete_device(device_id: str, user: User = Depends(tech)):
        service.delete_device(device_id, user.username)
        return {"ok": True}

    @app.get("/api/devices/{device_id}/preview")
    def preview(device_id: str, _: User = Depends(tech)):
        pkg, problems = service.build_package(device_id)
        if pkg:  # never show password hashes in the browser
            pkg["users"] = [{k: v for k, v in u.items() if k != "password_hash"} for u in pkg["users"]]
        return {"package": pkg, "problems": problems}

    @app.post("/api/devices/{device_id}/publish")
    def publish(device_id: str, body: dict[str, Any] = Body(default={}), user: User = Depends(tech)):
        return service.publish(device_id, user.username, str(body.get("notes") or ""))

    @app.get("/api/devices/{device_id}/packages/{version}")
    def download(device_id: str, version: int, _: User = Depends(tech)):
        pkg = service.package(device_id, version)
        name = f"{pkg['site']['id']}_{device_id}_v{version}.json"
        return JSONResponse(pkg, headers={"Content-Disposition": f'attachment; filename="{name}"'})

    # -- assets & I/O -------------------------------------------------------------
    @app.post("/api/sites/{site_id}/assets", status_code=201)
    def create_asset(site_id: str, body: dict[str, Any] = Body(...), user: User = Depends(tech)):
        return service.create_asset(site_id, body, user.username)

    @app.get("/api/assets/{asset_id}")
    def asset(asset_id: str, _: User = Depends(signed_in)):
        return service.asset_detail(asset_id)

    @app.patch("/api/assets/{asset_id}")
    def update_asset(asset_id: str, body: dict[str, Any] = Body(...), user: User = Depends(tech)):
        return service.update_asset(asset_id, body, user.username)

    @app.delete("/api/assets/{asset_id}")
    def delete_asset(asset_id: str, user: User = Depends(tech)):
        service.delete_asset(asset_id, user.username)
        return {"ok": True}

    @app.post("/api/assets/{asset_id}/duplicate", status_code=201)
    def duplicate_asset(asset_id: str, body: dict[str, Any] = Body(default={}), user: User = Depends(tech)):
        return service.duplicate_asset(asset_id, body, user.username)

    @app.put("/api/assets/{asset_id}/tags/{name}")
    def put_tag(asset_id: str, name: str, body: dict[str, Any] = Body(...), user: User = Depends(tech)):
        return service.put_tag(asset_id, name, body, user.username)

    @app.delete("/api/assets/{asset_id}/tags/{name}")
    def delete_tag(asset_id: str, name: str, user: User = Depends(tech)):
        service.delete_tag(asset_id, name, user.username)
        return {"ok": True}

    @app.post("/api/assets/{asset_id}/import")
    async def import_csv(asset_id: str, request: Request, mode: str = "merge", user: User = Depends(tech)):
        raw = await request.body()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("cp1252")  # Excel on Windows
        return service.import_csv(asset_id, text, mode, user.username)

    @app.post("/api/assets/{asset_id}/import-tia")
    async def import_tia(asset_id: str, request: Request, mode: str = "merge", user: User = Depends(tech)):
        return service.import_tia(asset_id, await request.body(), mode, user.username)

    @app.get("/api/assets/{asset_id}/io.csv")
    def export_csv(asset_id: str, _: User = Depends(signed_in)):
        service.asset(asset_id)
        return PlainTextResponse(service.export_csv(asset_id), media_type="text/csv",
                                 headers={"Content-Disposition": f'attachment; filename="{asset_id}_io.csv"'})

    @app.get("/api/io-template.csv")
    def template(_: User = Depends(signed_in)):
        return PlainTextResponse(service.csv_template(), media_type="text/csv",
                                 headers={"Content-Disposition": 'attachment; filename="io-template.csv"'})

    # -- users -----------------------------------------------------------------------
    @app.get("/api/users")
    def users(client_id: Optional[str] = None, kind: Optional[str] = None, _: User = Depends(signed_in)):
        return service.list_users(client_id=client_id, staff=kind == "staff")

    @app.post("/api/users", status_code=201)
    def create_user(body: dict[str, Any] = Body(...), user: User = Depends(tech)):
        if body.get("kind") == "staff" and user.role != "admin":
            raise HTTPException(403, "Only admins can create staff accounts")
        return service.create_user(body, user.username)

    @app.patch("/api/users/{username}")
    def update_user(username: str, body: dict[str, Any] = Body(...), user: User = Depends(tech)):
        target = service.user_row(username)
        if target["kind"] == "staff" and user.role != "admin":
            raise HTTPException(403, "Only admins can change staff accounts")
        if username == user.username and (body.get("disabled") or body.get("portal_role") not in (None, user.role)):
            raise HTTPException(422, "You can't change your own role or disable yourself")
        result = service.update_user(username, body, user.username)
        if body.get("disabled") or body.get("password") or body.get("portal_role"):
            sessions.end_user_sessions(username)
        return result

    @app.delete("/api/users/{username}")
    def delete_user(username: str, user: User = Depends(tech)):
        target = service.user_row(username)
        if target["kind"] == "staff" and user.role != "admin":
            raise HTTPException(403, "Only admins can delete staff accounts")
        if username == user.username:
            raise HTTPException(422, "You can't delete your own account")
        service.delete_user(username, user.username)
        sessions.end_user_sessions(username)
        return {"ok": True}

    @app.put("/api/users/{username}/access")
    def set_access(username: str, body: list[dict[str, Any]] = Body(...), user: User = Depends(tech)):
        return service.set_access(username, body, user.username)

    # -- pages -------------------------------------------------------------------------
    login_html = (EDGE_STATIC / "login.html").read_text(encoding="utf-8") \
        .replace("Plant HMI</div>", "Plant Cloud Portal</div>").replace("Sign in · Plant HMI", "Sign in · Cloud Portal") \
        .replace("Ask an engineer to reset it.", "Ask a portal admin to reset it.")

    @app.get("/")
    def index(request: Request):
        if sessions.resolve(request.cookies.get(COOKIE), touch=False) is None:
            return RedirectResponse("/login", status_code=303)
        return FileResponse(HERE / "static" / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/login")
    def login_page(request: Request):
        if sessions.resolve(request.cookies.get(COOKIE), touch=False) is not None:
            return RedirectResponse("/", status_code=303)
        return HTMLResponse(login_html, headers={"Cache-Control": "no-store"})

    app.mount("/portal", StaticFiles(directory=HERE / "static"), name="portal")
    app.mount("/static", StaticFiles(directory=EDGE_STATIC), name="static")  # shared styles & scripts
    return app
