"""Persistence and seat ownership.

SQLite is the *only* place game state lives. The process holds sockets and
nothing else, which is what makes "in-progress games survive a server restart"
true by construction rather than by a synchronisation routine that has to be
right.

Layering: `app/game.py` owns the rules and knows nothing about who is
connected. This module owns the mapping from a *session* to a *seat*, and is
therefore the only place that can turn "this cookie" into "this mark". The
WebSocket hub (phase 4) owns connections and knows neither.
"""

from __future__ import annotations

import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Iterator, Literal

import asyncio

from app.game import (
    BOARD_CELLS,
    EMPTY,
    MARKS,
    GameState,
    Mark,
    MoveAccepted,
    MoveRejected,
    Result,
    Status,
    apply_move,
    new_game,
    request_rematch,
    start,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    id           TEXT PRIMARY KEY,
    board        TEXT NOT NULL,
    turn         TEXT NOT NULL,
    status       TEXT NOT NULL,
    result       TEXT,
    winning_line TEXT,
    version      INTEGER NOT NULL,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    round        INTEGER NOT NULL DEFAULT 1,
    rematch      TEXT NOT NULL DEFAULT ''
);

-- PRIMARY KEY (game_id, mark) caps a game at two seats.
-- UNIQUE (game_id, sid) stops one session holding both seats.
-- Both invariants are enforced by the schema, not only by the code above it.
CREATE TABLE IF NOT EXISTS seats (
    game_id    TEXT NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    mark       TEXT NOT NULL CHECK (mark IN ('X', 'O')),
    sid        TEXT NOT NULL,
    claimed_at TEXT NOT NULL,
    PRIMARY KEY (game_id, mark),
    UNIQUE (game_id, sid)
);

-- Append-only audit log. Not needed to rebuild state (the board is stored
-- directly) but it makes "was this move actually authorised" answerable after
-- the fact, which a board snapshot alone cannot do.
CREATE TABLE IF NOT EXISTS moves (
    game_id TEXT NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    seq     INTEGER NOT NULL,
    mark    TEXT NOT NULL,
    cell    INTEGER NOT NULL,
    at      TEXT NOT NULL,
    PRIMARY KEY (game_id, seq)
);
"""


class Refused(str, Enum):
    """Refusals that belong to the seat layer, not to the rules.

    `app.game.Rejection` covers rule violations. These two cannot be expressed
    there because that module has no concept of sessions or games-by-id.
    """

    NO_SUCH_GAME = "no_such_game"
    NOT_A_PLAYER = "not_a_player"


@dataclass(frozen=True, slots=True)
class Role:
    """What a given session is allowed to do in a given game.

    A spectator is the *absence* of a seat, computed on demand — never a stored
    flag. There is no way to be a spectator with a mark (invariant I4).
    """

    role: Literal["player", "spectator"]
    mark: Mark | None = None

    @property
    def is_player(self) -> bool:
        return self.role == "player"

    def as_dict(self) -> dict:
        return {"role": self.role, "mark": self.mark}


SPECTATOR = Role("spectator")


@dataclass(frozen=True, slots=True)
class Rejected:
    """A refusal flattened to one wire code, whatever layer produced it.

    The hub sends this to exactly one socket. Callers never need to know whether
    the rules or the seat table said no.
    """

    code: str


PlayOutcome = MoveAccepted | Rejected


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_id() -> str:
    # ~72 bits. Unguessable matters: the URL is the only access control there is,
    # so a short sequential id would let anyone enumerate live games.
    return secrets.token_urlsafe(9)


def _encode_line(line: tuple[int, int, int] | None) -> str | None:
    return ",".join(str(i) for i in line) if line else None


def _decode_line(raw: str | None) -> tuple[int, int, int] | None:
    if not raw:
        return None
    a, b, c = (int(i) for i in raw.split(","))
    return (a, b, c)


def _row_to_state(row: sqlite3.Row) -> GameState:
    board = tuple(row["board"])
    if len(board) != BOARD_CELLS or any(c not in (EMPTY, *MARKS) for c in board):
        raise ValueError(f"corrupt board for game {row['id']!r}: {row['board']!r}")
    return GameState(
        id=row["id"],
        board=board,
        turn=row["turn"],
        status=Status(row["status"]),
        result=Result(row["result"]) if row["result"] else None,
        winning_line=_decode_line(row["winning_line"]),
        version=row["version"],
        round=row["round"],
        rematch=frozenset(row["rematch"]),
    )


class GameStore:
    def __init__(self, path: str | Path) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            path,
            check_same_thread=False,
            isolation_level=None,  # we manage transactions explicitly, below
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        # FULL, not NORMAL. We claim games survive a restart; NORMAL in WAL mode
        # can lose the last commits to a power cut. A handful of writes per game
        # makes the durability worth more than the microseconds.
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._locks: dict[str, asyncio.Lock] = {}

    def _migrate(self) -> None:
        """Add columns a database written by an older build will not have.

        `CREATE TABLE IF NOT EXISTS` silently does nothing when the table
        already exists, so a schema change alone would leave existing databases
        broken — and the whole point of this service is that games written by a
        previous process are still there.
        """
        existing = {row["name"] for row in self._conn.execute("PRAGMA table_info(games)")}
        for column, definition in (
            ("round", "INTEGER NOT NULL DEFAULT 1"),
            ("rematch", "TEXT NOT NULL DEFAULT ''"),
        ):
            if column not in existing:
                self._conn.execute(f"ALTER TABLE games ADD COLUMN {column} {definition}")

    def close(self) -> None:
        self._conn.close()

    # --- concurrency -------------------------------------------------------

    def _lock(self, game_id: str) -> asyncio.Lock:
        """One lock per game. Independent games never contend (requirement:
        many concurrent games with no cross-talk).

        Note there is no `await` between the lookup and the insert, so this is
        itself atomic under asyncio.
        """
        lock = self._locks.get(game_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[game_id] = lock
        return lock

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    # --- reads (private, assume the caller holds the game lock) ------------

    def _read_game(self, game_id: str) -> GameState | None:
        row = self._conn.execute(
            "SELECT * FROM games WHERE id = ?", (game_id,)
        ).fetchone()
        return _row_to_state(row) if row else None

    def _read_seats(self, game_id: str) -> dict[str, str]:
        rows = self._conn.execute(
            "SELECT mark, sid FROM seats WHERE game_id = ?", (game_id,)
        ).fetchall()
        return {row["mark"]: row["sid"] for row in rows}

    def _write_game(self, state: GameState) -> None:
        self._conn.execute(
            """UPDATE games
                  SET board = ?, turn = ?, status = ?, result = ?,
                      winning_line = ?, version = ?, updated_at = ?,
                      round = ?, rematch = ?
                WHERE id = ?""",
            (
                "".join(state.board),
                state.turn,
                state.status.value,
                state.result.value if state.result else None,
                _encode_line(state.winning_line),
                state.version,
                _utcnow(),
                state.round,
                "".join(sorted(state.rematch)),
                state.id,
            ),
        )

    # --- public API --------------------------------------------------------

    async def create_game(self, sid: str) -> GameState:
        """Create a game and seat the creator as X (ROADMAP 1.5)."""
        now = _utcnow()
        for _ in range(5):  # id collision is ~impossible; failing loudly is not
            state = new_game(_new_id())
            async with self._lock(state.id):
                try:
                    with self._transaction():
                        self._conn.execute(
                            """INSERT INTO games (id, board, turn, status, result,
                                                  winning_line, version, created_at, updated_at)
                               VALUES (?, ?, ?, ?, NULL, NULL, ?, ?, ?)""",
                            (
                                state.id,
                                "".join(state.board),
                                state.turn,
                                state.status.value,
                                state.version,
                                now,
                                now,
                            ),
                        )
                        self._conn.execute(
                            "INSERT INTO seats (game_id, mark, sid, claimed_at) VALUES (?, 'X', ?, ?)",
                            (state.id, sid, now),
                        )
                except sqlite3.IntegrityError:
                    continue
                return state
        raise RuntimeError("could not allocate a game id")

    async def get_game(self, game_id: str) -> GameState | None:
        async with self._lock(game_id):
            return self._read_game(game_id)

    async def role_of(self, game_id: str, sid: str) -> Role | None:
        """Report a session's role without claiming anything. None = no game."""
        async with self._lock(game_id):
            if self._read_game(game_id) is None:
                return None
            for mark, seat_sid in self._read_seats(game_id).items():
                if seat_sid == sid:
                    return Role("player", mark)  # type: ignore[arg-type]
            return SPECTATOR

    async def join(self, game_id: str, sid: str) -> Role | None:
        """Resolve a session's role, claiming the open seat if there is one.

        Idempotent: a session that already holds a seat gets that same seat back.
        This is the whole of reconnection — a returning player takes this exact
        path, because a seat was never tied to a connection in the first place.
        """
        async with self._lock(game_id):
            state = self._read_game(game_id)
            if state is None:
                return None

            seats = self._read_seats(game_id)
            for mark, seat_sid in seats.items():
                if seat_sid == sid:
                    return Role("player", mark)  # type: ignore[arg-type]

            if state.status is not Status.WAITING:
                return SPECTATOR

            free = [m for m in MARKS if m not in seats]
            if not free:
                return SPECTATOR

            mark = free[0]
            now = _utcnow()
            with self._transaction():
                self._conn.execute(
                    "INSERT INTO seats (game_id, mark, sid, claimed_at) VALUES (?, ?, ?, ?)",
                    (game_id, mark, sid, now),
                )
                if len(seats) + 1 == len(MARKS):
                    self._write_game(start(state))
            return Role("player", mark)

    async def play(self, game_id: str, sid: str, cell: object) -> PlayOutcome:
        """The only way a board ever changes.

        Order matters: resolve the seat from the session, ask the rules, commit,
        and only then hand the new state back for broadcast (invariant I5). A
        client can never observe a move that a restart would forget.
        """
        async with self._lock(game_id):
            state = self._read_game(game_id)
            if state is None:
                return Rejected(Refused.NO_SUCH_GAME.value)

            # The mark comes from the seat table, never from the client. There
            # is no message field a caller could use to name a different one.
            mark = next(
                (m for m, seat_sid in self._read_seats(game_id).items() if seat_sid == sid),
                None,
            )
            if mark is None:
                return Rejected(Refused.NOT_A_PLAYER.value)

            outcome = apply_move(state, mark, cell)
            if isinstance(outcome, MoveRejected):
                return Rejected(outcome.reason.value)

            # Continue the audit log across rounds rather than deriving the
            # sequence from board occupancy: a rematch resets the board, which
            # would restart the count and collide on (game_id, seq).
            seq = self._next_move_seq(game_id)
            with self._transaction():
                self._write_game(outcome.state)
                self._conn.execute(
                    "INSERT INTO moves (game_id, seq, mark, cell, at) VALUES (?, ?, ?, ?, ?)",
                    (game_id, seq, mark, cell, _utcnow()),
                )
            return outcome

    def _next_move_seq(self, game_id: str) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS next FROM moves WHERE game_id = ?",
            (game_id,),
        ).fetchone()
        return row["next"]

    async def request_rematch(self, game_id: str, sid: str) -> PlayOutcome:
        """Ask for a rematch. Same authority path as a move.

        The mark comes from the seat table, so a spectator cannot reset a game
        they are only watching.
        """
        async with self._lock(game_id):
            state = self._read_game(game_id)
            if state is None:
                return Rejected(Refused.NO_SUCH_GAME.value)

            mark = next(
                (m for m, seat_sid in self._read_seats(game_id).items() if seat_sid == sid),
                None,
            )
            if mark is None:
                return Rejected(Refused.NOT_A_PLAYER.value)

            outcome = request_rematch(state, mark)
            if isinstance(outcome, MoveRejected):
                return Rejected(outcome.reason.value)

            with self._transaction():
                self._write_game(outcome.state)
            return outcome

    async def move_log(self, game_id: str) -> list[tuple[int, str, int]]:
        async with self._lock(game_id):
            rows = self._conn.execute(
                "SELECT seq, mark, cell FROM moves WHERE game_id = ? ORDER BY seq",
                (game_id,),
            ).fetchall()
            return [(r["seq"], r["mark"], r["cell"]) for r in rows]
