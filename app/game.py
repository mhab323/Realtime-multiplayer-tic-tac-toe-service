"""Pure tic-tac-toe rules.

This module is the single place where a board changes (ROADMAP invariant I1).
It imports nothing from FastAPI, sqlite or asyncio on purpose: the rules are the
only part of this service that must be *provably* correct, so they are testable
without a server, a socket, or a database.

Note what is deliberately absent. There is no notion of a session, a connection,
a player id or a spectator in here. `apply_move` takes a *mark* ("X" or "O"), and
the only way a caller obtains a mark is by looking up a seat that the server
itself assigned. A spectator holds no seat, so there is no mark to pass, so a
spectator cannot reach this function at all (invariants I2 and I4).

Every state value is frozen and the board is a tuple, so an accepted move returns
a *new* state rather than editing the old one. Callers cannot accidentally
mutate a game they are only reading.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Literal

Mark = Literal["X", "O"]

EMPTY = "."
MARKS: tuple[Mark, Mark] = ("X", "O")
BOARD_CELLS = 9

WINNING_LINES: tuple[tuple[int, int, int], ...] = (
    (0, 1, 2), (3, 4, 5), (6, 7, 8),  # rows
    (0, 3, 6), (1, 4, 7), (2, 5, 8),  # columns
    (0, 4, 8), (2, 4, 6),             # diagonals
)


class Status(str, Enum):
    WAITING = "waiting"          # created, second seat still open
    IN_PROGRESS = "in_progress"  # both seats taken, moves accepted
    FINISHED = "finished"        # won or drawn, terminal


class Result(str, Enum):
    X_WON = "x_won"
    O_WON = "o_won"
    DRAW = "draw"


class Rejection(str, Enum):
    """Why a move was refused.

    These are *rule* violations only. Refusals that belong to the seat layer
    (`not_a_player`, `no_such_game`) are not here, because this module has no
    concept of who is connected.
    """

    GAME_NOT_STARTED = "game_not_started"
    GAME_OVER = "game_over"
    NOT_YOUR_TURN = "not_your_turn"
    CELL_TAKEN = "cell_taken"
    CELL_OUT_OF_RANGE = "cell_out_of_range"
    UNKNOWN_MARK = "unknown_mark"
    GAME_NOT_FINISHED = "game_not_finished"
    ALREADY_REQUESTED = "already_requested"


@dataclass(frozen=True, slots=True)
class GameState:
    """The whole authoritative state of one game.

    `turn` is only meaningful while `status is IN_PROGRESS`. Once a game is
    FINISHED it keeps whatever value it had; read `result` instead.
    """

    id: str
    board: tuple[str, ...]
    turn: Mark
    status: Status
    result: Result | None = None
    winning_line: tuple[int, int, int] | None = None
    version: int = 0
    # Rounds count games played on this link. Odd rounds start X, even start O,
    # so a rematch does not hand the same player the first-move advantage twice.
    round: int = 1
    # Which marks have asked for a rematch. Both, and a new round begins.
    rematch: frozenset[str] = frozenset()

    def as_dict(self) -> dict:
        """Wire representation. The only shape clients ever see."""
        return {
            "id": self.id,
            "board": list(self.board),
            "turn": self.turn,
            "status": self.status.value,
            "result": self.result.value if self.result else None,
            "winning_line": list(self.winning_line) if self.winning_line else None,
            "version": self.version,
            "round": self.round,
            "rematch": sorted(self.rematch),
        }


@dataclass(frozen=True, slots=True)
class MoveAccepted:
    state: GameState


@dataclass(frozen=True, slots=True)
class MoveRejected:
    reason: Rejection


MoveOutcome = MoveAccepted | MoveRejected


def other(mark: Mark) -> Mark:
    return "O" if mark == "X" else "X"


def new_game(game_id: str) -> GameState:
    return GameState(
        id=game_id,
        board=(EMPTY,) * BOARD_CELLS,
        turn="X",
        status=Status.WAITING,
    )


def start(state: GameState) -> GameState:
    """Both seats are now taken; the game becomes playable.

    Idempotent: calling it on an already-started or finished game is a no-op, so
    a racing second join cannot rewind a game in progress.
    """
    if state.status is not Status.WAITING:
        return state
    return replace(state, status=Status.IN_PROGRESS, version=state.version + 1)


def starting_mark(round_number: int) -> Mark:
    """Odd rounds start X, even start O."""
    return "X" if round_number % 2 == 1 else "O"


def request_rematch(state: GameState, mark: str) -> MoveOutcome:
    """Record one player's request for a rematch; start a new round on the second.

    Consent is mutual on purpose. A one-sided reset would let the loser wipe the
    board out from under a winner who was still looking at it.

    Seats do not change hands — a session that was X stays X, so nobody's
    identity shifts underneath them. Fairness comes from alternating who moves
    first, which is a property of the round number rather than of the seats.
    """
    if mark not in MARKS:
        return MoveRejected(Rejection.UNKNOWN_MARK)
    if state.status is not Status.FINISHED:
        return MoveRejected(Rejection.GAME_NOT_FINISHED)
    if mark in state.rematch:
        return MoveRejected(Rejection.ALREADY_REQUESTED)

    votes = state.rematch | {mark}
    if votes != set(MARKS):
        return MoveAccepted(replace(state, rematch=votes, version=state.version + 1))

    round_number = state.round + 1
    return MoveAccepted(replace(
        state,
        board=(EMPTY,) * BOARD_CELLS,
        turn=starting_mark(round_number),
        status=Status.IN_PROGRESS,
        result=None,
        winning_line=None,
        round=round_number,
        rematch=frozenset(),
        version=state.version + 1,
    ))


def winning_line_for(board: tuple[str, ...], mark: str) -> tuple[int, int, int] | None:
    for line in WINNING_LINES:
        if all(board[i] == mark for i in line):
            return line
    return None


def apply_move(state: GameState, mark: str, cell: object) -> MoveOutcome:
    """Validate and apply one move. The only function that writes to a board.

    `cell` is typed `object` deliberately. It arrives from the network, and this
    function is the last line of defence rather than the first: Pydantic already
    rejects malformed frames at the WebSocket boundary, but a rules engine that
    trusts its caller is one refactor away from being exploitable.
    """
    if mark not in MARKS:
        return MoveRejected(Rejection.UNKNOWN_MARK)

    if state.status is Status.WAITING:
        return MoveRejected(Rejection.GAME_NOT_STARTED)
    if state.status is Status.FINISHED:
        return MoveRejected(Rejection.GAME_OVER)

    # `bool` subclasses `int` in Python, so `True` would otherwise pass the range
    # check below and land a mark on cell 1. Reject it explicitly.
    if isinstance(cell, bool) or not isinstance(cell, int):
        return MoveRejected(Rejection.CELL_OUT_OF_RANGE)
    if not 0 <= cell < BOARD_CELLS:
        return MoveRejected(Rejection.CELL_OUT_OF_RANGE)

    # Turn before occupancy: moving out of turn is the more fundamental
    # violation, and telling an out-of-turn client *which* cells are free would
    # leak nothing useful anyway.
    if mark != state.turn:
        return MoveRejected(Rejection.NOT_YOUR_TURN)
    if state.board[cell] != EMPTY:
        return MoveRejected(Rejection.CELL_TAKEN)

    cells = list(state.board)
    cells[cell] = mark
    board = tuple(cells)
    version = state.version + 1

    line = winning_line_for(board, mark)
    if line is not None:
        return MoveAccepted(replace(
            state,
            board=board,
            status=Status.FINISHED,
            result=Result.X_WON if mark == "X" else Result.O_WON,
            winning_line=line,
            version=version,
        ))

    if EMPTY not in board:
        return MoveAccepted(replace(
            state,
            board=board,
            status=Status.FINISHED,
            result=Result.DRAW,
            version=version,
        ))

    return MoveAccepted(replace(
        state,
        board=board,
        turn=other(mark),  # type: ignore[arg-type]
        version=version,
    ))
