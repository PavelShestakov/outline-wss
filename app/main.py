"""Subset of Outline Shadowbox Management API for a WSS Outline node."""
from __future__ import annotations

import asyncio
import os
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, StrictInt

from .core import Manager, ManagerError, Settings, Unavailable

_manager: Manager | None = None
_stop = threading.Event()


def manager() -> Manager:
    global _manager
    if _manager is None:
        _manager = Manager(Settings.from_env())
    return _manager


def _poll_loop():
    while not _stop.is_set():
        try:
            manager().poll()
        except Exception:
            # We deliberately do not log secret-bearing exceptions.
            pass
        _stop.wait(manager().s.poll_seconds)


@asynccontextmanager
async def lifespan(app: FastAPI):
    manager()
    _stop.clear()
    thread = threading.Thread(target=_poll_loop, daemon=True, name="traffic-poller")
    thread.start()
    yield
    _stop.set()
    thread.join(timeout=3)


app = FastAPI(title="Outline WSS Manager", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

ADMIN_TOKEN = os.environ.get("OUTLINE_ADMIN_TOKEN", "")
if not ADMIN_TOKEN or len(ADMIN_TOKEN) < 24:
    raise RuntimeError("Set OUTLINE_ADMIN_TOKEN (>=24 random URL-safe characters)")
ROOT = f"/m/{ADMIN_TOKEN}"


@app.middleware("http")
async def protect_url(request: Request, call_next):
    if not (request.url.path == ROOT or request.url.path.startswith(ROOT + "/")):
        return Response(status_code=404)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(KeyError)
async def key_not_found(request: Request, exc):
    return JSONResponse(status_code=404, content={"message": "Access key not found"})


@app.exception_handler(ValueError)
async def bad_input(request: Request, exc):
    return JSONResponse(status_code=400, content={"message": str(exc)})


@app.exception_handler(ManagerError)
async def management_problem(request: Request, exc):
    return JSONResponse(status_code=503, content={"message": "Management backend unavailable"})


class PortInput(BaseModel):
    port: int = Field(default=636, ge=1, le=65535)


class ByteLimit(BaseModel):
    bytes: StrictInt = Field(ge=0)


class LimitInput(BaseModel):
    limit: ByteLimit


class NameInput(BaseModel):
    name: str


@app.get(ROOT + "/health")
def health():
    return manager().healthy()


@app.get(ROOT + "/access-keys/")
def keys():
    return {"accessKeys": manager().list()}


@app.post(ROOT + "/access-keys/", status_code=201)
def create_key(body: PortInput | None = None):
    port = body.port if body is not None else int(os.getenv("OUTLINE_WS_PORT", "636"))
    return manager().create(port=port)


@app.delete(ROOT + "/access-keys/{key_id}", status_code=204)
def delete_key(key_id: str):
    manager().delete(key_id)
    return Response(status_code=204)


@app.put(ROOT + "/access-keys/{key_id}/name", status_code=204)
async def rename_key(key_id: str, request: Request):
    ct = request.headers.get("content-type", "")
    if "multipart/form-data" in ct or "application/x-www-form-urlencoded" in ct:
        form = await request.form()
        new_name = form.get("name")
    else:
        new_name = (await request.json()).get("name")
    if not isinstance(new_name, str):
        raise ValueError("Name must be text")
    manager().rename(key_id, new_name)
    return Response(status_code=204)


@app.put(ROOT + "/access-keys/{key_id}/data-limit", status_code=204)
def put_limit(key_id: str, body: LimitInput):
    manager().set_limit(key_id, body.limit.bytes)
    return Response(status_code=204)


@app.delete(ROOT + "/access-keys/{key_id}/data-limit", status_code=204)
def delete_limit(key_id: str):
    manager().remove_limit(key_id)
    return Response(status_code=204)


@app.get(ROOT + "/metrics/transfer")
def transferred():
    return {"bytesTransferredByUserId": manager().metrics()}


@app.get(ROOT + "/server")
def server():
    return {
        "name": "Outline WSS Node", "serverId": "outline-wss",
        "metricsEnabled": True, "version": "outline-wss-manager/0.1.0",
        "portForNewAccessKeys": int(os.getenv("OUTLINE_WS_PORT", "636")),
        "hostnameForAccessKeys": manager().s.ws_base_url.removeprefix("wss://").split("/")[0],
    }


@app.get(ROOT + "/metrics/enabled")
def metrics_enabled():
    return {"metricsEnabled": True}


@app.put(ROOT + "/metrics/enabled", status_code=204)
def set_metrics(body: dict):
    if body.get("metricsEnabled") is not True:
        raise HTTPException(status_code=409, detail="Metrics required for quota enforcement")
    return Response(status_code=204)


@app.put(ROOT + "/name", status_code=204)
def set_name(body: NameInput):
    # Shadowbox-compatible no-op: the node name is managed externally.
    return Response(status_code=204)


@app.put(ROOT + "/server/port-for-new-access-keys", status_code=204)
def set_port(body: PortInput):
    if body.port != int(os.getenv("OUTLINE_WS_PORT", "636")):
        raise HTTPException(status_code=409, detail="WSS uses a configured shared local port")
    return Response(status_code=204)


@app.put(ROOT + "/server/hostname-for-access-keys")
def set_hostname(body: dict):
    raise HTTPException(status_code=409, detail="Change OUTLINE_WS_BASE and regenerate YAML instead")


@app.put(ROOT + "/server/access-key-data-limit", status_code=204)
def global_limit(body: LimitInput):
    manager().bulk_limit(body.limit.bytes)
    return Response(status_code=204)


@app.delete(ROOT + "/server/access-key-data-limit", status_code=204)
def remove_global_limit():
    manager().bulk_limit(None)
    return Response(status_code=204)
