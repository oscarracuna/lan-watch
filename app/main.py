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

from app.config import load_config
from app.pinger import Hub, PingEngine
from app.store import Store

WEB_DIR = Path(__file__).resolve().parent / "web"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_config()
    store = Store()
    hub = Hub()
    engine = PingEngine(settings, store, hub)
    app.state.settings = settings
    app.state.store = store
    app.state.hub = hub
    app.state.engine = engine
    ping_task = asyncio.create_task(engine.run())
    try:
        yield
    finally:
        engine.stop()
        ping_task.cancel()
        try:
            await ping_task
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


@app.get("/api/status")
async def status() -> dict[str, Any]:
    return app.state.engine.snapshot()


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
