"""
FastAPI application entry point.

Startup sequence:
  1. Init SQLite DB
  2. Start Schwab stream + bot eval loop (background task)
  3. Start WebSocket broadcast loop (5s pushes to dashboard)
  4. Serve API + static React build

Auth flow:
  GET /auth/url          → Schwab OAuth2 URL (open in browser)
  GET /auth/callback     → exchange code for tokens, bot starts
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from backend.api.routes import router
from backend.api.websocket import websocket_endpoint, start_broadcast_loop
from backend.bot_engine import run as run_bot
from backend.data.schwab_auth import get_auth_url, exchange_code, has_tokens
from backend.config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

_bot_task: asyncio.Task = None
_broadcast_task: asyncio.Task = None

FRONTEND_DIST = Path(__file__).parent.parent / "frontend" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _bot_task, _broadcast_task
    logger.info("Starting equities bot server...")

    _broadcast_task = asyncio.create_task(start_broadcast_loop(5))

    if has_tokens():
        _bot_task = asyncio.create_task(run_bot())
        logger.info("Bot engine started (tokens present)")
    else:
        logger.warning("No Schwab tokens. Visit /auth/url to authenticate.")

    yield

    for task in [_bot_task, _broadcast_task]:
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    logger.info("Server shutdown complete")


app = FastAPI(title="Equities Bot", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── API routes ────────────────────────────────────────────────────────────────
app.include_router(router)


# ── WebSocket ─────────────────────────────────────────────────────────────────
@app.websocket("/ws/live")
async def ws_live(websocket: WebSocket):
    await websocket_endpoint(websocket)


# ── Auth ──────────────────────────────────────────────────────────────────────
@app.get("/auth/url")
async def auth_url():
    return {
        "url": get_auth_url(),
        "instructions": (
            "Open this URL in a browser, log in to Schwab, "
            "then copy the full redirect URL and extract the 'code' parameter. "
            "Pass it to GET /auth/callback?code=YOUR_CODE"
        ),
    }


@app.get("/auth/callback")
async def auth_callback(code: str):
    global _bot_task
    try:
        await exchange_code(code)
        if _bot_task is None or _bot_task.done():
            _bot_task = asyncio.create_task(run_bot())
        return {"status": "ok", "message": "Authenticated. Bot starting."}
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


@app.get("/health")
async def health():
    return {"status": "ok", "auth": has_tokens()}


# ── Serve React build (production) ────────────────────────────────────────────
if FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIST / "assets")), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str):
        index = FRONTEND_DIST / "index.html"
        if index.exists():
            return FileResponse(str(index))
        return JSONResponse({"error": "Frontend not built"}, status_code=404)


if __name__ == "__main__":
    uvicorn.run(
        "backend.main:app",
        host="0.0.0.0",
        port=settings.port,
        reload=False,
    )
