"""FastAPI web server: REST for metadata and operator actions, WebSocket for live data.

Run with ``python -m src.hmi`` (see ``__main__.py`` for options).
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .drivers import create_driver
from .runtime import Runtime, WriteRejected
from .tags import TagDatabase, TagError

HERE = Path(__file__).parent
STATIC = HERE / "static"


class WriteRequest(BaseModel):
    value: Any


class AckRequest(BaseModel):
    id: str | None = None


def _user(request: Request) -> str:
    """Identify who made a change for the event journal."""
    return request.headers.get("x-operator") or (request.client.host if request.client else "unknown")


def create_app(runtime: Runtime | None = None) -> FastAPI:
    if runtime is None:
        db = TagDatabase.load(os.environ.get("HMI_TAGS", HERE / "tags.json"))
        driver = create_driver(
            os.environ.get("HMI_DRIVER", "sim"),
            db,
            **({
                "host": os.environ.get("HMI_MODBUS_HOST", "127.0.0.1"),
                "port": int(os.environ.get("HMI_MODBUS_PORT", "502")),
                "unit": int(os.environ.get("HMI_MODBUS_UNIT", "1")),
            } if os.environ.get("HMI_DRIVER") == "modbus" else {}),
        )
        runtime = Runtime(db, driver, poll_interval=float(os.environ.get("HMI_POLL_S", "0.25")))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(title="PLC Web HMI", lifespan=lifespan)
    app.state.runtime = runtime

    @app.get("/api/config")
    def config() -> dict[str, Any]:
        return {
            "plant": runtime.db.plant,
            "driver": type(runtime.driver).__name__,
            "simulated_inputs": bool(getattr(runtime.driver, "supports_simulated_inputs", False)),
            "tags": [t.public() for t in runtime.db.tags.values()],
        }

    @app.get("/api/state")
    def state() -> dict[str, Any]:
        return runtime.snapshot()

    @app.get("/api/history")
    def history(tags: str | None = None) -> dict[str, list[tuple[float, float]]]:
        names = tags.split(",") if tags else list(runtime.history)
        unknown = [n for n in names if n not in runtime.history]
        if unknown:
            raise HTTPException(404, f"no history for {', '.join(unknown)}")
        return {n: list(runtime.history[n]) for n in names}

    @app.post("/api/tags/{name}")
    async def write_tag(name: str, body: WriteRequest, request: Request) -> dict[str, Any]:
        try:
            value = await runtime.write(name, body.value, _user(request))
        except TagError as exc:
            raise HTTPException(404 if "unknown tag" in str(exc) else 422, str(exc)) from None
        except WriteRejected as exc:
            raise HTTPException(409, str(exc)) from None
        except IOError as exc:
            raise HTTPException(502, f"PLC write failed: {exc}") from None
        return {"name": name, "value": value}

    @app.post("/api/simulate/{name}")
    async def simulate(name: str, body: WriteRequest, request: Request) -> dict[str, Any]:
        try:
            value = await runtime.simulate_input(name, body.value, _user(request))
        except TagError as exc:
            raise HTTPException(422, str(exc)) from None
        except WriteRejected as exc:
            raise HTTPException(409, str(exc)) from None
        return {"name": name, "value": value}

    @app.post("/api/alarms/ack")
    def ack(body: AckRequest, request: Request) -> dict[str, int]:
        if body.id is not None and body.id not in runtime.alarms.alarms:
            raise HTTPException(404, f"unknown alarm {body.id!r}")
        return {"acknowledged": runtime.alarms.acknowledge(body.id, _user(request))}

    @app.websocket("/ws")
    async def live(ws: WebSocket) -> None:
        await ws.accept()
        queue = runtime.subscribe()
        try:
            await ws.send_json(runtime.snapshot())  # full state incl. every journal event
            while True:
                await ws.send_json(await queue.get())
        except (WebSocketDisconnect, asyncio.CancelledError, RuntimeError):
            pass
        finally:
            runtime.unsubscribe(queue)

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
