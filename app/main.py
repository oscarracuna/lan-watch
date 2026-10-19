from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.config import load_config
from app.map_positions import JUAREZ_CENTER, clear_position, load_positions, save_position
from app.notify import TelegramNotifier, load_secrets
from app.pinger import Hub, PingEngine
from app.store import Store

WEB_DIR = Path(__file__).resolve().parent / "web"


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_secrets()
    settings = load_config()
    store = Store()
    hub = Hub()
    notifier = TelegramNotifier()
    notifier.ready(settings)
    engine = PingEngine(settings, store, hub, notifier)
    app.state.settings = settings
    app.state.store = store
    app.state.hub = hub
    app.state.engine = engine
    ping_task = asyncio.create_task(engine.run())
    cmd_task = asyncio.create_task(notifier.poll_commands(engine))
    try:
        yield
    finally:
        engine.stop()
        notifier.stop()
        ping_task.cancel()
        cmd_task.cancel()
        for task in (ping_task, cmd_task):
            try:
                await task
            except asyncio.CancelledError:
                pass
        store.close()


app = FastAPI(title="LAN Watch", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/favicon.ico")
async def favicon() -> FileResponse:
    png = WEB_DIR / "favicon.png"
    if png.exists():
        return FileResponse(png)
    return FileResponse(WEB_DIR / "favicon.svg")


@app.get("/map")
async def city_map() -> FileResponse:
    return FileResponse(WEB_DIR / "map.html")


@app.get("/api/status")
async def status() -> dict[str, Any]:
    return app.state.engine.snapshot()


class MapPin(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)


def _gateway_or_404(host_key: str):
    state = app.state.engine.states.get(host_key)
    if state is None or state.role != "gateway":
        raise HTTPException(status_code=404, detail="Unknown gateway")
    return state


@app.get("/api/map")
async def map_layout() -> dict[str, Any]:
    engine: PingEngine = app.state.engine
    positions = load_positions()
    gateways = []
    for state in engine.snapshot()["hosts"]:
        if state.get("role") != "gateway":
            continue
        pin = positions.get(state["host_key"])
        gateways.append({**state, "pin": pin})
    return {
        "center": {"lat": JUAREZ_CENTER[0], "lng": JUAREZ_CENTER[1]},
        "zoom": 12,
        "gateways": gateways,
    }


@app.put("/api/map/{host_key}")
async def place_gateway(host_key: str, pin: MapPin) -> dict[str, Any]:
    state = _gateway_or_404(host_key)
    try:
        saved = save_position(state.host_key, pin.lat, pin.lng)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"host_key": state.host_key, "pin": saved}


@app.delete("/api/map/{host_key}")
async def unplace_gateway(host_key: str) -> dict[str, Any]:
    state = _gateway_or_404(host_key)
    clear_position(state.host_key)
    return {"host_key": state.host_key, "pin": None}


@app.get("/api/incidents")
async def incidents() -> dict[str, Any]:
    engine: PingEngine = app.state.engine
    since = time.time() - engine.settings.history_hours * 3600
    return {"incidents": app.state.store.incidents(since)}


@app.get("/api/history/{host_key}")
async def history(host_key: str, hours: float | None = None) -> dict[str, Any]:
    engine: PingEngine = app.state.engine
    store: Store = app.state.store
    if host_key not in engine.states:
        raise HTTPException(status_code=404, detail="Unknown host")
    window = hours if hours and hours > 0 else engine.settings.history_hours
    since = time.time() - window * 3600
    samples = store.history(host_key, since)
    total = len(samples)
    ok = sum(1 for sample in samples if sample["ok"])
    uptime_pct = (100.0 * ok / total) if total else None
    state = engine.states[host_key]
    return {
        "host": state.as_dict(),
        "hours": window,
        "samples": samples,
        "uptime_pct": uptime_pct,
        "sample_count": total,
    }


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


@app.get("/events")
async def events() -> StreamingResponse:
    engine: PingEngine = app.state.engine
    hub: Hub = app.state.hub
    queue = hub.subscribe()

    async def stream():
        try:
            yield _sse("snapshot", engine.snapshot())
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=15)
                    yield _sse(item["event"], item["data"])
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            hub.unsubscribe(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
