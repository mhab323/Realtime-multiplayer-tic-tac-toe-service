"""Store tests: seat ownership, durability, and per-game isolation.

The rules are already proven in test_game.py. What is under test here is the
mapping from a *session* to a *seat*, and the claim that SQLite is the only
place state lives.
"""

import asyncio

import pytest

from app.game import EMPTY, MoveAccepted, Status
from app.store import GameStore, Rejected, Refused


@pytest.fixture
def store(tmp_path):
    s = GameStore(tmp_path / "games.db")
    yield s
    s.close()


async def two_player_game(store) -> str:
    game = await store.create_game("sid-x")
    await store.join(game.id, "sid-o")
    return game.id


# --- seat claiming ---------------------------------------------------------

async def test_creator_is_seated_as_x_and_game_waits(store):
    game = await store.create_game("sid-x")
    assert game.status is Status.WAITING
    role = await store.role_of(game.id, "sid-x")
    assert role.is_player and role.mark == "X"


async def test_second_session_takes_o_and_starts_the_game(store):
    game = await store.create_game("sid-x")
    role = await store.join(game.id, "sid-o")
    assert role.is_player and role.mark == "O"
    assert (await store.get_game(game.id)).status is Status.IN_PROGRESS


async def test_third_session_is_a_spectator(store):
    game_id = await two_player_game(store)
    role = await store.join(game_id, "sid-watcher")
    assert role.role == "spectator"
    assert role.mark is None


async def test_joining_twice_returns_the_same_seat(store):
    """This is reconnection. A returning player takes this exact path."""
    game = await store.create_game("sid-x")
    await store.join(game.id, "sid-o")
    again = await store.join(game.id, "sid-o")
    assert again.mark == "O"


async def test_one_session_cannot_hold_both_seats(store):
    game = await store.create_game("sid-x")
    role = await store.join(game.id, "sid-x")
    assert role.mark == "X"
    assert (await store.get_game(game.id)).status is Status.WAITING


async def test_join_on_a_missing_game_returns_none(store):
    assert await store.join("nope", "sid") is None
    assert await store.role_of("nope", "sid") is None


async def test_watching_does_not_quietly_seat_you(store):
    """`join` claims a free seat, so it must not hand one to a late arrival."""
    game_id = await two_player_game(store)
    await store.join(game_id, "sid-watcher")
    assert (await store.role_of(game_id, "sid-watcher")).role == "spectator"
    assert (await store.role_of(game_id, "sid-x")).mark == "X"
    assert (await store.role_of(game_id, "sid-o")).mark == "O"


# --- authority -------------------------------------------------------------

async def test_spectator_cannot_move(store):
    game_id = await two_player_game(store)
    await store.join(game_id, "sid-watcher")
    outcome = await store.play(game_id, "sid-watcher", 0)
    assert outcome == Rejected(Refused.NOT_A_PLAYER.value)


async def test_unknown_session_cannot_move(store):
    """A forged or absent cookie is just a session with no seat."""
    game_id = await two_player_game(store)
    outcome = await store.play(game_id, "sid-forged", 0)
    assert outcome == Rejected(Refused.NOT_A_PLAYER.value)


async def test_move_on_a_missing_game_is_refused(store):
    outcome = await store.play("nope", "sid", 0)
    assert outcome == Rejected(Refused.NO_SUCH_GAME.value)


async def test_cannot_move_out_of_turn(store):
    game_id = await two_player_game(store)
    assert await store.play(game_id, "sid-o", 0) == Rejected("not_your_turn")


async def test_cannot_move_before_the_opponent_joins(store):
    game = await store.create_game("sid-x")
    assert await store.play(game.id, "sid-x", 0) == Rejected("game_not_started")


async def test_rejected_moves_leave_no_trace(store):
    game_id = await two_player_game(store)
    before = await store.get_game(game_id)
    await store.play(game_id, "sid-o", 0)         # out of turn
    await store.play(game_id, "sid-watcher", 0)   # not a player
    await store.play(game_id, "sid-x", 99)        # out of range
    assert await store.get_game(game_id) == before
    assert await store.move_log(game_id) == []


# --- persistence -----------------------------------------------------------

async def test_accepted_move_is_persisted_and_audited(store):
    game_id = await two_player_game(store)
    outcome = await store.play(game_id, "sid-x", 4)
    assert isinstance(outcome, MoveAccepted)
    assert (await store.get_game(game_id)).board[4] == "X"
    assert await store.move_log(game_id) == [(1, "X", 4)]


async def test_in_progress_game_survives_a_restart(tmp_path):
    """The exit criterion for this phase.

    Nothing is carried over in memory: the second GameStore only sees the file.
    """
    db = tmp_path / "games.db"

    store = GameStore(db)
    game = await store.create_game("sid-x")
    await store.join(game.id, "sid-o")
    await store.play(game.id, "sid-x", 4)
    await store.play(game.id, "sid-o", 0)
    before = await store.get_game(game.id)
    log_before = await store.move_log(game.id)
    store.close()

    reopened = GameStore(db)
    try:
        assert await reopened.get_game(game.id) == before
        assert await reopened.move_log(game.id) == log_before

        # seats survive too, so nobody loses their side
        assert (await reopened.role_of(game.id, "sid-x")).mark == "X"
        assert (await reopened.role_of(game.id, "sid-o")).mark == "O"
        assert (await reopened.role_of(game.id, "sid-other")).role == "spectator"

        # and the game continues from where it stopped
        resumed = await reopened.play(game.id, "sid-x", 1)
        assert isinstance(resumed, MoveAccepted)
        assert resumed.state.version == before.version + 1
    finally:
        reopened.close()


# --- isolation and concurrency --------------------------------------------

async def test_concurrent_games_do_not_cross_talk(store):
    g1 = await store.create_game("sid-a")
    g2 = await store.create_game("sid-b")
    await store.join(g1.id, "sid-b")  # same session, different seat per game
    await store.join(g2.id, "sid-a")

    await store.play(g1.id, "sid-a", 0)

    assert (await store.get_game(g1.id)).board[0] == "X"
    assert (await store.get_game(g2.id)).board == (EMPTY,) * 9
    assert (await store.role_of(g1.id, "sid-b")).mark == "O"
    assert (await store.role_of(g2.id, "sid-b")).mark == "X"


async def test_two_moves_racing_for_one_cell_produce_exactly_one(store):
    """Invariant I6.

    Honest caveat: today this passes for two independent reasons. The critical
    section in `play` contains no `await`, so asyncio cannot preempt it, and the
    per-game lock also serialises it. The lock is the reason that survives
    someone swapping stdlib sqlite3 for an async driver.
    """
    game_id = await two_player_game(store)

    results = await asyncio.gather(
        store.play(game_id, "sid-x", 4),
        store.play(game_id, "sid-x", 4),
    )

    accepted = [r for r in results if isinstance(r, MoveAccepted)]
    refused = [r for r in results if isinstance(r, Rejected)]
    assert len(accepted) == 1
    assert len(refused) == 1
    assert refused[0].code in {"cell_taken", "not_your_turn"}
    assert len(await store.move_log(game_id)) == 1


async def test_many_games_in_flight_stay_independent(store):
    games = [await store.create_game(f"sid-{i}") for i in range(25)]
    for i, game in enumerate(games):
        await store.join(game.id, f"opponent-{i}")

    await asyncio.gather(*(store.play(g.id, f"sid-{i}", i % 9) for i, g in enumerate(games)))

    for i, game in enumerate(games):
        state = await store.get_game(game.id)
        assert state.board[i % 9] == "X"
        assert sum(1 for c in state.board if c != EMPTY) == 1
