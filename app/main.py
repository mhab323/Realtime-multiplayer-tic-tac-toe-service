"""Application entry point.

Phase 0: skeleton only. Sessions, games and the WebSocket hub arrive in phases
3 and 4.
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="Realtime Tic-Tac-Toe")

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


@app.get("/")
async def home() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
