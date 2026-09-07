"""HTTP surface: sessions, game creation, and the pages.

The session cookie is minted here and is the only thing that identifies a
player. Everything downstream — seats, marks, whether you may move — is derived
from it server-side. No endpoint accepts an identity from the client.
"""

from __future__ import annotations

import os
import re
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.store import GameStore

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = PROJECT_ROOT / "static"
# Anchored to the repo, not the process working directory, so the database does
# not appear in a different place depending on where uvicorn was launched from.
DEFAULT_DB = Path(os.environ.get("TTT_DB") or PROJECT_ROOT / "data" / "games.db")

SESSION_COOKIE = "sid"
SESSION_MAX_AGE = 7 * 24 * 60 * 60  # 7 days

# A session id is 32 random bytes, urlsafe-encoded to 43 characters. The pattern
# exists to throw out junk cheaply, not to authenticate: a forged cookie is
# simply a session that holds no seat, which is already the safe default.
_SID_RE = re.compile(r"^[A-Za-z0-9_-]{20,64}$")
_GAME_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,32}$")

NOT_FOUND_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>No such game</title><link rel="stylesheet" href="/static/style.css"></head>
<body><main class="card"><h1>No such game</h1>
<p class="muted">That link is wrong, or the game never existed.</p>
<a class="primary" href="/">Start a new one</a></main></body></html>
"""


def create_app(db_path: str | Path | None = None) -> FastAPI:
    """Build an app bound to one database.

    A factory rather than a module-level singleton so tests get an isolated
    database per case instead of sharing global state.
    """

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        yield
        application.state.store.close()

    app = FastAPI(title="Realtime Tic-Tac-Toe", lifespan=lifespan)
    app.state.store = GameStore(db_path or DEFAULT_DB)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.middleware("http")
    async def session_cookie(request: Request, call_next):
        """Give every visitor a stable, opaque session id.

        httpOnly so no script on the page can read it, SameSite=Lax so it is not
        sent on cross-site POSTs, and Secure only when actually served over TLS
        (marking it Secure on plain-http localhost would stop the browser
        storing it at all, silently breaking every seat).

        The cookie is unsigned on purpose. Signing would let us detect a forged
        value, but forging one gains nothing: an invented session holds no seat
        and can only spectate. The only useful attack is guessing a *live*
        session id, and that is 256 bits.
        """
        sid = request.cookies.get(SESSION_COOKIE)
        mint = sid is None or not _SID_RE.match(sid)
        if mint:
            sid = secrets.token_urlsafe(32)
        request.state.sid = sid

        response = await call_next(request)

        if mint:
            response.set_cookie(
                SESSION_COOKIE,
                sid,
                max_age=SESSION_MAX_AGE,
                httponly=True,
                samesite="lax",
                secure=request.url.scheme == "https",
                path="/",
            )
        return response

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True}

    @app.get("/")
    async def home() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.post("/api/games", status_code=201)
    async def create_game(request: Request) -> JSONResponse:
        """Create a game and seat the creator as X.

        A POST, so having a side effect is honest. This is the only endpoint
        that claims a seat over HTTP; the opponent's seat is claimed when their
        browser opens the WebSocket (see `game_page` for why).
        """
        state = await app.state.store.create_game(request.state.sid)
        return JSONResponse(
            {"id": state.id, "url": f"{request.base_url}g/{state.id}"},
            status_code=201,
        )

    @app.get("/api/games/{game_id}")
    async def read_game(game_id: str, request: Request) -> dict:
        """Read-only view of a game and the caller's role in it."""
        if not _GAME_ID_RE.match(game_id):
            raise HTTPException(status_code=404, detail="no such game")
        state = await app.state.store.get_game(game_id)
        if state is None:
            raise HTTPException(status_code=404, detail="no such game")
        role = await app.state.store.role_of(game_id, request.state.sid)
        return {"game": state.as_dict(), "you": role.as_dict()}

    @app.get("/g/{game_id}")
    async def game_page(game_id: str, request: Request):
        """Serve the game shell. Deliberately claims nothing.

        Seats are claimed on WebSocket connect, not here, for two reasons. HTTP
        says GET must not have side effects, and more concretely: paste a game
        link into Slack, WhatsApp or iMessage and their link-preview crawler
        fetches this URL. If a GET claimed a seat, the unfurler would take it and
        the human following the link would arrive as a spectator to their own
        game. Crawlers do not open WebSockets.
        """
        if not _GAME_ID_RE.match(game_id):
            return HTMLResponse(NOT_FOUND_HTML, status_code=404)
        if await app.state.store.get_game(game_id) is None:
            return HTMLResponse(NOT_FOUND_HTML, status_code=404)
        return FileResponse(STATIC_DIR / "game.html")

    return app


app = create_app()
