"""Connection registry and fan-out.

The hub owns sockets. It does not own game state and it does not decide who may
move — it cannot, because it never sees the seat table. Its whole job is: which
sockets are watching game G, and deliver this message to them.

Connections are ephemeral and live only here. Nothing in this module is
persisted, which is the other half of "state lives in SQLite": if the process
dies, all that is lost is a set of sockets that were already dead anyway.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from fastapi import WebSocket

from app.store import Role

log = logging.getLogger(__name__)


@dataclass(eq=False)
class Connection:
    """One open socket.

    `eq=False` gives identity semantics, which matters: two tabs of the same
    session are two connections sharing one seat, and a set must keep both.

    `role` here is for presence and the UI only. It is never consulted to
    authorise a move — `store.play` re-resolves the seat from the session on
    every single message, because a role captured at connect time is a cached
    copy of authority and cached authority goes stale.
    """

    ws: WebSocket
    sid: str
    role: Role


class Hub:
    def __init__(self) -> None:
        self._rooms: dict[str, set[Connection]] = {}

    def join(self, game_id: str, conn: Connection) -> None:
        self._rooms.setdefault(game_id, set()).add(conn)

    def leave(self, game_id: str, conn: Connection) -> None:
        room = self._rooms.get(game_id)
        if room is None:
            return
        room.discard(conn)
        if not room:
            # Rooms are evicted when they empty, so this table is bounded by
            # *live* games rather than games ever seen.
            del self._rooms[game_id]

    def presence(self, game_id: str) -> dict[str, Any]:
        room = self._rooms.get(game_id, set())
        return {
            "players_online": sorted(
                c.role.mark for c in room if c.role.is_player and c.role.mark
            ),
            "spectators": sum(1 for c in room if not c.role.is_player),
        }

    async def broadcast(
        self,
        game_id: str,
        message: dict[str, Any],
        *,
        skip: Connection | None = None,
    ) -> None:
        """Send to everyone watching this game.

        One dead socket must not stop the others from being told, so sends run
        concurrently and failures are collected rather than raised. A socket
        that errors is dropped: it is gone, and its seat is unaffected because a
        seat was never tied to a connection.
        """
        room = [c for c in self._rooms.get(game_id, set()) if c is not skip]
        if not room:
            return

        results = await asyncio.gather(
            *(conn.ws.send_json(message) for conn in room),
            return_exceptions=True,
        )
        for conn, result in zip(room, results):
            if isinstance(result, BaseException):
                log.debug("dropping dead socket in %s: %r", game_id, result)
                self.leave(game_id, conn)
