"""HTTP surface: sessions, game creation, and the pages.

The session cookie is minted here and is the only thing that identifies a
player. Everything downstream — seats, marks, whether you may move — is derived
from it server-side. No endpoint accepts an identity from the client.
"""

from __future__ import annotations

import json
import os
import re
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, ValidationError

from app.hub import Connection, Hub
from app.store import GameStore, Rejected

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

# A move is about 30 bytes. Anything approaching this is not a client of ours.
MAX_FRAME_BYTES = 1024

ERROR_MESSAGES = {
    "no_such_game": "That game does not exist.",
    "no_session": "No session — open the game page first.",
    "not_a_player": "You are watching this game.",
    "not_your_turn": "It is not your turn.",
    "cell_taken": "That square is already taken.",
    "cell_out_of_range": "That is not a square.",
    "game_over": "This game is already finished.",
    "game_not_started": "Still waiting for an opponent.",
    "unknown_mark": "Unrecognised mark.",
    "malformed_json": "That was not JSON.",
    "unknown_message": "Unrecognised message.",
    "message_too_large": "Message too large.",
}


class MoveMessage(BaseModel):
    """The entire client vocabulary. There are no other messages.

    `strict=True` so `true` does not become cell 1 and `"0"` does not become
    cell 0 — lenient parsing is how an adversarial input becomes a legal move.

    `extra="forbid"` so there is no field in which to smuggle an identity.
    `{"type":"move","cell":0,"mark":"O"}` is a hard error rather than a field we
    quietly ignore. Combined with deriving the mark from the session, "move as
    the other player" is not a request the protocol can express.
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    type: Literal["move"]
    cell: int


async def _error(websocket: WebSocket, code: str) -> None:
    """Refuse one message. Deliberately does not close the socket.

    A buggy client should not lose its game over a bad frame, and a malicious
    one gains nothing by staying connected — every message is re-authorised.
    """
    await websocket.send_json(
        {"type": "error", "code": code, "message": ERROR_MESSAGES.get(code, "Refused.")}
    )


async def _reject(websocket: WebSocket, code: str) -> None:
    """Refuse the connection itself: say why, then close."""
    await _error(websocket, code)
    await websocket.close(code=1008)  # policy violation


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
    app.state.hub = Hub()
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

    # --- realtime ----------------------------------------------------------

    async def state_message(game_id: str) -> dict:
        state = await app.state.store.get_game(game_id)
        return {
            "type": "state",
            "game": state.as_dict(),
            "presence": app.state.hub.presence(game_id),
        }

    async def handle_message(websocket: WebSocket, game_id: str, sid: str, raw: str) -> None:
        if len(raw) > MAX_FRAME_BYTES:
            return await _error(websocket, "message_too_large")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return await _error(websocket, "malformed_json")
        try:
            message = MoveMessage.model_validate(payload)
        except ValidationError:
            return await _error(websocket, "unknown_message")

        # The seat is resolved from the session on *every* message, not read
        # from the connection. A role captured at connect time is a cached copy
        # of authority, and this is the one place that must not be stale.
        outcome = await app.state.store.play(game_id, sid, message.cell)
        if isinstance(outcome, Rejected):
            # Refusals go to the offender only. Nobody else needs to know that
            # someone tried something.
            return await _error(websocket, outcome.code)

        await app.state.hub.broadcast(
            game_id,
            {
                "type": "state",
                "game": outcome.state.as_dict(),
                "presence": app.state.hub.presence(game_id),
            },
        )

    @app.websocket("/ws/{game_id}")
    async def game_socket(websocket: WebSocket, game_id: str) -> None:
        await websocket.accept()

        if not _GAME_ID_RE.match(game_id):
            return await _reject(websocket, "no_such_game")

        sid = websocket.cookies.get(SESSION_COOKIE)
        if sid is None or not _SID_RE.match(sid):
            # A cookie cannot be minted on a WebSocket handshake, and we will
            # not take an identity from the message body. A client that never
            # loaded a page therefore has no session and cannot hold a seat.
            return await _reject(websocket, "no_session")

        # This is where seats are claimed — not on the page GET. See game_page.
        role = await app.state.store.join(game_id, sid)
        if role is None:
            return await _reject(websocket, "no_such_game")

        state = await app.state.store.get_game(game_id)
        if state is None:  # pragma: no cover - nothing deletes games
            return await _reject(websocket, "no_such_game")

        conn = Connection(ws=websocket, sid=sid, role=role)
        app.state.hub.join(game_id, conn)
        try:
            await websocket.send_json(
                {
                    "type": "snapshot",
                    "you": role.as_dict(),
                    "game": state.as_dict(),
                    "presence": app.state.hub.presence(game_id),
                }
            )
            # Everyone else learns the roster changed — and this may be the join
            # that started the game.
            await app.state.hub.broadcast(game_id, await state_message(game_id), skip=conn)

            while True:
                # receive() rather than receive_text(): a binary frame has no
                # "text" key, so receive_text() would raise KeyError and take
                # the handler down. The protocol is text-only, so say so.
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    break
                raw = message.get("text")
                if raw is None:
                    await _error(websocket, "unknown_message")
                    continue
                await handle_message(websocket, game_id, sid, raw)
        except WebSocketDisconnect:
            pass
        finally:
            # Dropping a socket never touches the seat. That is the whole of
            # reconnection: there is nothing to restore.
            app.state.hub.leave(game_id, conn)
            await app.state.hub.broadcast(
                game_id,
                {"type": "presence", "presence": app.state.hub.presence(game_id)},
            )

    return app


app = create_app()
