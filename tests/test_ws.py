"""Realtime behaviour and server authority.

Identity here comes from an explicit `Cookie` header per socket, which is how
the browser does it too. Test sessions are well-formed ids the server never
issued — accepted as sessions, holding no seat until one is claimed, which is
exactly the documented model.
"""

import json
from contextlib import contextmanager

import pytest
from starlette.testclient import TestClient

from app.main import create_app

ALICE = "a" * 43
BOB = "b" * 43
CAROL = "c" * 43
DAVE = "d" * 43

EMPTY_BOARD = ["."] * 9


def jar(sid: str) -> dict[str, str]:
    return {"Cookie": f"sid={sid}"}


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path / "games.db")
    with TestClient(app) as test_client:
        yield test_client


def new_game(client: TestClient, sid: str) -> str:
    res = client.post("/api/games", headers=jar(sid))
    assert res.status_code == 201
    return res.json()["id"]


@contextmanager
def seated_game(client: TestClient):
    """A game with both seats taken and both sockets drained of setup traffic."""
    game_id = new_game(client, ALICE)
    with (
        client.websocket_connect(f"/ws/{game_id}", headers=jar(ALICE)) as x,
        client.websocket_connect(f"/ws/{game_id}", headers=jar(BOB)) as o,
    ):
        x.receive_json()  # snapshot: you are X, waiting
        o.receive_json()  # snapshot: you are O, in progress
        x.receive_json()  # broadcast: the game started
        yield game_id, x, o


# --- the happy path --------------------------------------------------------

def test_two_players_play_a_full_game(client):
    game_id = new_game(client, ALICE)

    with client.websocket_connect(f"/ws/{game_id}", headers=jar(ALICE)) as x:
        snapshot = x.receive_json()
        assert snapshot["type"] == "snapshot"
        assert snapshot["you"] == {"role": "player", "mark": "X"}
        assert snapshot["game"]["status"] == "waiting"

        with client.websocket_connect(f"/ws/{game_id}", headers=jar(BOB)) as o:
            joined = o.receive_json()
            assert joined["you"] == {"role": "player", "mark": "O"}
            assert joined["game"]["status"] == "in_progress"

            started = x.receive_json()
            assert started["type"] == "state"
            assert started["game"]["status"] == "in_progress"
            assert started["presence"]["players_online"] == ["O", "X"]

            seen_x = seen_o = None
            for socket, cell in [(x, 0), (o, 3), (x, 1), (o, 4), (x, 2)]:
                socket.send_json({"type": "move", "cell": cell})
                seen_x, seen_o = x.receive_json(), o.receive_json()
                assert seen_x["game"] == seen_o["game"]  # one authoritative state

            assert seen_x["game"]["status"] == "finished"
            assert seen_x["game"]["result"] == "x_won"
            assert seen_x["game"]["winning_line"] == [0, 1, 2]


def test_a_finished_game_accepts_nothing_further(client):
    with seated_game(client) as (_, x, o):
        for socket, cell in [(x, 0), (o, 3), (x, 1), (o, 4), (x, 2)]:
            socket.send_json({"type": "move", "cell": cell})
            x.receive_json(), o.receive_json()

        o.send_json({"type": "move", "cell": 5})
        assert o.receive_json()["code"] == "game_over"


# --- authority -------------------------------------------------------------

def test_cannot_move_out_of_turn(client):
    with seated_game(client) as (_, x, o):
        o.send_json({"type": "move", "cell": 0})
        refusal = o.receive_json()
        assert refusal["type"] == "error"
        assert refusal["code"] == "not_your_turn"


def test_cannot_move_twice_in_a_row(client):
    with seated_game(client) as (_, x, o):
        x.send_json({"type": "move", "cell": 0})
        x.receive_json(), o.receive_json()
        x.send_json({"type": "move", "cell": 1})
        assert x.receive_json()["code"] == "not_your_turn"


def test_cannot_take_an_occupied_cell(client):
    with seated_game(client) as (_, x, o):
        x.send_json({"type": "move", "cell": 4})
        x.receive_json(), o.receive_json()
        o.send_json({"type": "move", "cell": 4})
        assert o.receive_json()["code"] == "cell_taken"


def test_a_refusal_goes_only_to_the_offender(client):
    with seated_game(client) as (_, x, o):
        o.send_json({"type": "move", "cell": 0})
        assert o.receive_json()["code"] == "not_your_turn"

        # X must not have been told anything. Prove it by making a real move
        # and checking that is the first thing X sees.
        x.send_json({"type": "move", "cell": 8})
        assert x.receive_json()["type"] == "state"


def test_spectator_can_watch_but_not_move(client):
    with seated_game(client) as (game_id, x, o):
        with client.websocket_connect(f"/ws/{game_id}", headers=jar(CAROL)) as watcher:
            snapshot = watcher.receive_json()
            assert snapshot["you"] == {"role": "spectator", "mark": None}
            assert snapshot["presence"]["spectators"] == 1
            x.receive_json(), o.receive_json()  # roster changed

            watcher.send_json({"type": "move", "cell": 0})
            assert watcher.receive_json()["code"] == "not_a_player"

            # ...but a real move still reaches them live
            x.send_json({"type": "move", "cell": 4})
            x.receive_json(), o.receive_json()
            assert watcher.receive_json()["game"]["board"][4] == "X"


def test_a_socket_with_no_session_is_refused(client):
    game_id = new_game(client, ALICE)
    with client.websocket_connect(f"/ws/{game_id}") as ws:
        refusal = ws.receive_json()
        assert refusal["type"] == "error"
        assert refusal["code"] == "no_session"


def test_a_socket_for_an_unknown_game_is_refused(client):
    with client.websocket_connect(f"/ws/{'z' * 12}", headers=jar(ALICE)) as ws:
        assert ws.receive_json()["code"] == "no_such_game"


def test_a_fourth_arrival_cannot_take_a_seat(client):
    with seated_game(client) as (game_id, x, o):
        with client.websocket_connect(f"/ws/{game_id}", headers=jar(CAROL)) as c:
            assert c.receive_json()["you"]["role"] == "spectator"
            x.receive_json(), o.receive_json()
        with client.websocket_connect(f"/ws/{game_id}", headers=jar(DAVE)) as d:
            assert d.receive_json()["you"]["role"] == "spectator"


# --- adversarial frames ----------------------------------------------------

@pytest.mark.parametrize(
    "raw, code",
    [
        ("this is not json", "malformed_json"),
        (json.dumps({"type": "resign"}), "unknown_message"),
        (json.dumps({"type": "move"}), "unknown_message"),
        (json.dumps({"type": "move", "cell": "0"}), "unknown_message"),
        (json.dumps({"type": "move", "cell": True}), "unknown_message"),
        (json.dumps({"type": "move", "cell": 1.5}), "unknown_message"),
        (json.dumps({"type": "move", "cell": None}), "unknown_message"),
        # the interesting one: there is no field in which to name a player
        (json.dumps({"type": "move", "cell": 0, "mark": "O"}), "unknown_message"),
        (json.dumps({"type": "move", "cell": 0, "sid": BOB}), "unknown_message"),
        # valid shape, illegal value: caught by the rules, not the parser
        (json.dumps({"type": "move", "cell": 99}), "cell_out_of_range"),
        (json.dumps({"type": "move", "cell": -1}), "cell_out_of_range"),
        ("x" * (1024 + 1), "message_too_large"),
    ],
)
def test_bad_frames_are_refused(client, raw, code):
    with seated_game(client) as (_, x, o):
        x.send_text(raw)
        refusal = x.receive_json()
        assert refusal["type"] == "error"
        assert refusal["code"] == code


def test_a_binary_frame_is_refused_without_crashing(client):
    """The protocol is text-only. receive_text() would KeyError on this."""
    with seated_game(client) as (_, x, o):
        x.send_bytes(b"\x00\x01\x02\xff")
        assert x.receive_json()["code"] == "unknown_message"
        x.send_json({"type": "move", "cell": 0})
        assert x.receive_json()["game"]["board"][0] == "X"


def test_a_bad_frame_does_not_drop_the_connection(client):
    """A buggy client should not lose its game over one malformed message."""
    with seated_game(client) as (_, x, o):
        x.send_text("garbage")
        assert x.receive_json()["type"] == "error"
        x.send_json({"type": "move", "cell": 0})
        assert x.receive_json()["game"]["board"][0] == "X"


def test_the_board_is_untouched_by_every_refused_move(client):
    with seated_game(client) as (game_id, x, o):
        for socket, payload in [
            (o, {"type": "move", "cell": 0}),          # out of turn
            (x, {"type": "move", "cell": 99}),         # out of range
            (x, {"type": "move", "cell": 0, "mark": "O"}),  # smuggled identity
        ]:
            socket.send_json(payload)
            assert socket.receive_json()["type"] == "error"

        body = client.get(f"/api/games/{game_id}", headers=jar(CAROL)).json()
        assert body["game"]["board"] == EMPTY_BOARD
        assert body["game"]["version"] == 1  # started, never moved


# --- reconnection ----------------------------------------------------------

def test_a_refresh_keeps_your_seat_and_the_board(client):
    game_id = new_game(client, ALICE)
    with (
        client.websocket_connect(f"/ws/{game_id}", headers=jar(ALICE)) as x,
        client.websocket_connect(f"/ws/{game_id}", headers=jar(BOB)) as o,
    ):
        x.receive_json(), o.receive_json(), x.receive_json()
        x.send_json({"type": "move", "cell": 4})
        x.receive_json(), o.receive_json()

    # both sockets are gone. while O is away, a stranger opens the link:
    with client.websocket_connect(f"/ws/{game_id}", headers=jar(DAVE)) as stranger:
        assert stranger.receive_json()["you"]["role"] == "spectator"

    # O comes back and finds their seat exactly as they left it
    with client.websocket_connect(f"/ws/{game_id}", headers=jar(BOB)) as o2:
        snapshot = o2.receive_json()
        assert snapshot["you"] == {"role": "player", "mark": "O"}
        assert snapshot["game"]["board"][4] == "X"
        assert snapshot["game"]["turn"] == "O"
        assert snapshot["game"]["status"] == "in_progress"

        o2.send_json({"type": "move", "cell": 0})
        assert o2.receive_json()["game"]["board"][0] == "O"


def test_two_tabs_of_one_session_share_one_seat(client):
    with seated_game(client) as (game_id, x, o):
        with client.websocket_connect(f"/ws/{game_id}", headers=jar(ALICE)) as x2:
            assert x2.receive_json()["you"] == {"role": "player", "mark": "X"}
            x.receive_json(), o.receive_json()

            # a move from the second tab is still X's move, and every socket
            # sees it, including the tab that did not send it
            x2.send_json({"type": "move", "cell": 0})
            for socket in (x, o, x2):
                assert socket.receive_json()["game"]["board"][0] == "X"


def test_presence_reflects_who_is_actually_connected(client):
    with seated_game(client) as (game_id, x, o):
        with client.websocket_connect(f"/ws/{game_id}", headers=jar(CAROL)):
            roster = x.receive_json()
            o.receive_json()
            assert roster["presence"] == {"players_online": ["O", "X"], "spectators": 1}

        left = x.receive_json()
        assert left["type"] == "presence"
        assert left["presence"] == {"players_online": ["O", "X"], "spectators": 0}


# --- isolation -------------------------------------------------------------

def test_moves_do_not_leak_between_games(client):
    with seated_game(client) as (_, x1, o1):
        other = new_game(client, CAROL)
        with (
            client.websocket_connect(f"/ws/{other}", headers=jar(CAROL)) as x2,
            client.websocket_connect(f"/ws/{other}", headers=jar(DAVE)) as o2,
        ):
            x2.receive_json(), o2.receive_json(), x2.receive_json()

            x1.send_json({"type": "move", "cell": 0})
            x1.receive_json(), o1.receive_json()

            # if game 1's broadcast had leaked, this would be the next thing
            # game 2's sockets read
            x2.send_json({"type": "move", "cell": 8})
            board = x2.receive_json()["game"]["board"]
            assert board == [".", ".", ".", ".", ".", ".", ".", ".", "X"]
            assert o2.receive_json()["game"]["board"] == board


# --- rematch ---------------------------------------------------------------

def play_to_a_win(x, o):
    """X takes the top row. Both sockets are drained."""
    for socket_, cell in [(x, 0), (o, 3), (x, 1), (o, 4), (x, 2)]:
        socket_.send_json({"type": "move", "cell": cell})
        x.receive_json(), o.receive_json()


def test_a_rematch_needs_both_players(client):
    with seated_game(client) as (_, x, o):
        play_to_a_win(x, o)

        x.send_json({"type": "rematch"})
        after_one = x.receive_json()
        o.receive_json()
        assert after_one["game"]["status"] == "finished"  # board untouched
        assert after_one["game"]["rematch"] == ["X"]

        o.send_json({"type": "rematch"})
        fresh = x.receive_json()
        assert fresh["game"] == o.receive_json()["game"]
        assert fresh["game"]["status"] == "in_progress"
        assert fresh["game"]["board"] == EMPTY_BOARD
        assert fresh["game"]["round"] == 2
        assert fresh["game"]["turn"] == "O"
        assert fresh["game"]["rematch"] == []


def test_you_keep_your_mark_across_a_rematch(client):
    """A rematch must not reassign seats underneath a player."""
    game_id = new_game(client, ALICE)
    with (
        client.websocket_connect(f"/ws/{game_id}", headers=jar(ALICE)) as x,
        client.websocket_connect(f"/ws/{game_id}", headers=jar(BOB)) as o,
    ):
        x.receive_json(), o.receive_json(), x.receive_json()
        play_to_a_win(x, o)
        for socket_ in (x, o):
            socket_.send_json({"type": "rematch"})
            x.receive_json(), o.receive_json()

    with client.websocket_connect(f"/ws/{game_id}", headers=jar(ALICE)) as again:
        snapshot = again.receive_json()
        assert snapshot["you"] == {"role": "player", "mark": "X"}
        assert snapshot["game"]["round"] == 2


def test_a_spectator_cannot_reset_the_board(client):
    with seated_game(client) as (game_id, x, o):
        play_to_a_win(x, o)
        with client.websocket_connect(f"/ws/{game_id}", headers=jar(CAROL)) as watcher:
            watcher.receive_json()
            x.receive_json(), o.receive_json()

            watcher.send_json({"type": "rematch"})
            assert watcher.receive_json()["code"] == "not_a_player"

        body = client.get(f"/api/games/{game_id}", headers=jar(DAVE)).json()
        assert body["game"]["status"] == "finished"
        assert body["game"]["round"] == 1


def test_no_rematch_before_the_game_is_over(client):
    with seated_game(client) as (_, x, o):
        x.send_json({"type": "rematch"})
        assert x.receive_json()["code"] == "game_not_finished"


def test_asking_for_a_rematch_twice_is_refused(client):
    with seated_game(client) as (_, x, o):
        play_to_a_win(x, o)
        x.send_json({"type": "rematch"})
        x.receive_json(), o.receive_json()
        x.send_json({"type": "rematch"})
        assert x.receive_json()["code"] == "already_requested"


@pytest.mark.parametrize(
    "raw",
    [
        json.dumps({"type": "rematch", "mark": "O"}),
        json.dumps({"type": "rematch", "cell": 0}),
    ],
)
def test_a_rematch_message_still_has_no_field_to_impersonate_with(client, raw):
    with seated_game(client) as (_, x, o):
        play_to_a_win(x, o)
        x.send_text(raw)
        assert x.receive_json()["code"] == "unknown_message"
