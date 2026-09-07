"""Rules tests. No server, no socket, no database — see app/game.py.

These cover invariant I1: if the rules are wrong here, nothing downstream can
save us. The adversarial suite in phase 6 tests the *wiring* and assumes these
pass.
"""

import dataclasses

import pytest

from app.game import (
    BOARD_CELLS,
    EMPTY,
    MARKS,
    WINNING_LINES,
    GameState,
    MoveAccepted,
    MoveRejected,
    Rejection,
    Result,
    Status,
    apply_move,
    new_game,
    other,
    start,
    winning_line_for,
)


# --- helpers ---------------------------------------------------------------

def playable(board=None, turn="X", version=1) -> GameState:
    return GameState(
        id="g",
        board=tuple(board) if board else (EMPTY,) * BOARD_CELLS,
        turn=turn,
        status=Status.IN_PROGRESS,
        version=version,
    )


def accept(state: GameState, mark: str, cell) -> GameState:
    outcome = apply_move(state, mark, cell)
    assert isinstance(outcome, MoveAccepted), f"expected accepted, got {outcome}"
    return outcome.state


def expect_rejected(state: GameState, mark: str, cell, reason: Rejection) -> None:
    outcome = apply_move(state, mark, cell)
    assert isinstance(outcome, MoveRejected), f"expected rejected, got {outcome}"
    assert outcome.reason is reason


def scatter(count: int, avoid: tuple[int, ...]) -> list[int]:
    """Pick `count` cells outside `avoid` that do not themselves form a line.

    Naively taking the first free cells produces boards where the *opponent* has
    already won (e.g. reserve the top row for O and the first three free cells
    are 3,4,5 — a line for X). Such a position is unreachable in a real game, so
    a test asserting anything about it proves nothing.
    """
    chosen: list[int] = []
    for i in range(BOARD_CELLS):
        if i in avoid or len(chosen) == count:
            continue
        candidate = chosen + [i]
        if any(all(c in candidate for c in line) for line in WINNING_LINES):
            continue
        chosen.append(i)
    assert len(chosen) == count
    return chosen


# --- lifecycle -------------------------------------------------------------

def test_new_game_starts_empty_and_waiting():
    state = new_game("abc")
    assert state.id == "abc"
    assert state.board == (EMPTY,) * BOARD_CELLS
    assert state.turn == "X"
    assert state.status is Status.WAITING
    assert state.result is None
    assert state.winning_line is None
    assert state.version == 0


def test_no_moves_before_the_opponent_joins():
    expect_rejected(new_game("abc"), "X", 0, Rejection.GAME_NOT_STARTED)


def test_start_makes_the_game_playable_and_bumps_version():
    state = start(new_game("abc"))
    assert state.status is Status.IN_PROGRESS
    assert state.version == 1


def test_start_is_idempotent():
    """A racing second join must not rewind a game already in progress."""
    state = start(new_game("abc"))
    state = accept(state, "X", 0)
    restarted = start(state)
    assert restarted is state
    assert restarted.board[0] == "X"


# --- winning and drawing ---------------------------------------------------

@pytest.mark.parametrize("mark", MARKS)
@pytest.mark.parametrize("line", WINNING_LINES)
def test_every_winning_line_is_detected(line, mark):
    a, b, final = line
    opponent = other(mark)

    cells = [EMPTY] * BOARD_CELLS
    cells[a] = cells[b] = mark
    # X always has exactly one more mark than O when X is to move, so the
    # opponent's count depends on who is about to win. Keeping the position
    # reachable is what makes this test mean something.
    for i in scatter(2 if mark == "X" else 3, avoid=line):
        cells[i] = opponent

    board = tuple(cells)
    assert winning_line_for(board, opponent) is None, "fixture is already won"

    state = accept(playable(board, turn=mark, version=4), mark, final)

    assert state.status is Status.FINISHED
    assert state.result is (Result.X_WON if mark == "X" else Result.O_WON)
    assert state.winning_line == line
    assert state.version == 5


def test_full_board_with_no_line_is_a_draw():
    #  X O X
    #  X O O
    #  O X X
    state = start(new_game("abc"))
    mark = "X"
    for cell in (0, 1, 2, 4, 3, 5, 7, 6, 8):
        state = accept(state, mark, cell)
        mark = other(mark)

    assert state.status is Status.FINISHED
    assert state.result is Result.DRAW
    assert state.winning_line is None
    assert EMPTY not in state.board


def test_turn_alternates():
    state = start(new_game("abc"))
    assert state.turn == "X"
    state = accept(state, "X", 0)
    assert state.turn == "O"
    state = accept(state, "O", 4)
    assert state.turn == "X"


# --- rejections ------------------------------------------------------------

def test_cannot_move_out_of_turn():
    expect_rejected(playable(), "O", 0, Rejection.NOT_YOUR_TURN)


def test_cannot_move_twice_in_a_row():
    state = accept(playable(), "X", 0)
    expect_rejected(state, "X", 1, Rejection.NOT_YOUR_TURN)


def test_cannot_take_an_occupied_cell():
    state = accept(playable(), "X", 4)
    expect_rejected(state, "O", 4, Rejection.CELL_TAKEN)


def test_cannot_move_after_the_game_is_over():
    won = accept(playable(["X", "X", EMPTY, "O", "O", EMPTY, EMPTY, EMPTY, EMPTY]), "X", 2)
    assert won.status is Status.FINISHED
    expect_rejected(won, "O", 5, Rejection.GAME_OVER)


@pytest.mark.parametrize("cell", [-1, 9, 100, 1.5, "0", None, [0], {"cell": 0}])
def test_out_of_range_and_wrong_typed_cells_are_rejected(cell):
    expect_rejected(playable(), "X", cell, Rejection.CELL_OUT_OF_RANGE)


@pytest.mark.parametrize("cell", [True, False])
def test_booleans_are_not_cells(cell):
    """bool subclasses int, so `True` would otherwise land a mark on cell 1."""
    expect_rejected(playable(), "X", cell, Rejection.CELL_OUT_OF_RANGE)


@pytest.mark.parametrize("mark", ["Z", "x", "", None, 1])
def test_unknown_marks_are_rejected(mark):
    expect_rejected(playable(), mark, 0, Rejection.UNKNOWN_MARK)


# --- immutability ----------------------------------------------------------

def test_a_rejected_move_changes_nothing():
    before = accept(playable(), "X", 0)
    apply_move(before, "X", 1)  # out of turn
    apply_move(before, "O", 0)  # occupied
    apply_move(before, "O", 99)  # out of range
    assert before.board == ("X",) + (EMPTY,) * 8
    assert before.version == 2
    assert before.turn == "O"


def test_apply_move_does_not_mutate_its_input():
    before = playable()
    accept(before, "X", 0)
    assert before.board == (EMPTY,) * BOARD_CELLS
    assert before.version == 1


def test_state_is_frozen():
    state = playable()
    with pytest.raises(dataclasses.FrozenInstanceError):
        state.board = ()


# --- wire shape ------------------------------------------------------------

def test_as_dict_is_json_safe():
    state = accept(playable(["X", "X", EMPTY, "O", "O", EMPTY, EMPTY, EMPTY, EMPTY]), "X", 2)
    payload = state.as_dict()
    assert payload["status"] == "finished"
    assert payload["result"] == "x_won"
    assert payload["winning_line"] == [0, 1, 2]
    assert payload["board"] == ["X", "X", "X", "O", "O", ".", ".", ".", "."]
    import json
    json.dumps(payload)  # must not raise
