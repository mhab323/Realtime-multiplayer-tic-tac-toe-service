"""End-to-end tests against a real uvicorn process over real WebSockets.

Everything else in the suite runs the app in-process. That is fast and it is
where most bugs are, but it cannot prove the two claims that matter most here:
that a game survives the process dying, and that the service holds up when
several games and a hostile client are in flight at once.

The server is killed with `Popen.kill()`, never `terminate()`. A graceful
shutdown would let the app flush on the way out, which would prove nothing about
durability. This is the abrupt death that "survives a server restart" has to
mean.
"""

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from contextlib import AsyncExitStack
from pathlib import Path

import httpx
import pytest
import websockets

ROOT = Path(__file__).resolve().parent.parent

ALICE = "a" * 43
BOB = "b" * 43
CAROL = "c" * 43
DAVE = "d" * 43


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Server:
    """A real uvicorn process, restartable against the same database file."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.port = free_port()
        self.proc: subprocess.Popen | None = None

    @property
    def http(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def ws(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    def start(self) -> None:
        self.proc = subprocess.Popen(
            [
                sys.executable, "-m", "uvicorn", "app.main:app",
                "--host", "127.0.0.1", "--port", str(self.port),
                "--log-level", "warning",
            ],
            cwd=ROOT,
            env={**os.environ, "TTT_DB": str(self.db_path)},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + 30
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"server exited with {self.proc.returncode}")
            try:
                if httpx.get(f"{self.http}/healthz", timeout=0.5).status_code == 200:
                    return
            except httpx.HTTPError:
                time.sleep(0.1)
        raise RuntimeError("server never became ready")

    def kill(self) -> None:
        """Abrupt. No shutdown hooks, no flush, no goodbye to open sockets."""
        assert self.proc is not None
        self.proc.kill()
        self.proc.wait(timeout=10)
        self.proc = None

    def __enter__(self) -> "Server":
        self.start()
        return self

    def __exit__(self, *_exc) -> None:
        if self.proc is not None:
            self.kill()


@pytest.fixture
def server(tmp_path):
    with Server(tmp_path / "games.db") as running:
        yield running


# --- helpers ---------------------------------------------------------------

async def create_game(server: Server, sid: str) -> str:
    async with httpx.AsyncClient(base_url=server.http) as client:
        res = await client.post("/api/games", headers={"Cookie": f"sid={sid}"})
        assert res.status_code == 201
        return res.json()["id"]


def open_socket(server: Server, game_id: str, sid: str):
    return websockets.connect(
        f"{server.ws}/ws/{game_id}", additional_headers={"Cookie": f"sid={sid}"}
    )


async def recv(ws, timeout: float = 5.0) -> dict:
    return json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))


async def move(ws, cell: int) -> None:
    await ws.send(json.dumps({"type": "move", "cell": cell}))


async def drain(ws, timeout: float = 0.3) -> list[dict]:
    seen = []
    while True:
        try:
            seen.append(json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout)))
        except (asyncio.TimeoutError, TimeoutError):
            return seen


async def assert_silent(ws, timeout: float = 0.6) -> None:
    try:
        stray = await asyncio.wait_for(ws.recv(), timeout=timeout)
    except (asyncio.TimeoutError, TimeoutError):
        return
    raise AssertionError(f"expected no further messages, got {stray}")


async def seat_both(stack: AsyncExitStack, server: Server, game_id: str,
                    x_sid: str = ALICE, o_sid: str = BOB):
    """Both seats taken, setup traffic drained, game in progress."""
    x = await stack.enter_async_context(open_socket(server, game_id, x_sid))
    await recv(x)  # snapshot
    o = await stack.enter_async_context(open_socket(server, game_id, o_sid))
    await recv(o)  # snapshot
    await recv(x)  # the join that started the game
    return x, o


# --- durability ------------------------------------------------------------

async def test_a_game_in_progress_survives_a_hard_kill(server):
    game_id = await create_game(server, ALICE)

    async with AsyncExitStack() as stack:
        x, o = await seat_both(stack, server, game_id)
        for socket_, cell in [(x, 0), (o, 3), (x, 1)]:
            await move(socket_, cell)
            await recv(x), await recv(o)

    server.kill()
    server.start()

    async with AsyncExitStack() as stack:
        x = await stack.enter_async_context(open_socket(server, game_id, ALICE))
        snapshot = await recv(x)
        assert snapshot["you"] == {"role": "player", "mark": "X"}
        assert "".join(snapshot["game"]["board"]) == "XX.O....."
        assert snapshot["game"]["turn"] == "O"
        assert snapshot["game"]["status"] == "in_progress"

        # a stranger still cannot take the absent player's seat
        stranger = await stack.enter_async_context(open_socket(server, game_id, CAROL))
        assert (await recv(stranger))["you"]["role"] == "spectator"
        await recv(x)  # roster changed

        # and the real opponent picks up exactly where they left off
        o = await stack.enter_async_context(open_socket(server, game_id, BOB))
        assert (await recv(o))["you"] == {"role": "player", "mark": "O"}
        await recv(x), await recv(stranger)

        await move(o, 4)
        await recv(x), await recv(o), await recv(stranger)
        await move(x, 2)
        finished = await recv(x)
        assert finished["game"]["result"] == "x_won"
        assert finished["game"]["winning_line"] == [0, 1, 2]


async def test_a_move_the_client_saw_confirmed_is_never_lost(server):
    """Invariant I5, end to end.

    The server commits before it broadcasts. So if a client has *seen* a move
    land, no restart may forget it — there is no window in which the client
    knows something the database does not.
    """
    game_id = await create_game(server, ALICE)

    async with AsyncExitStack() as stack:
        x, o = await seat_both(stack, server, game_id)
        await move(x, 4)
        confirmed = await recv(x)
        assert confirmed["game"]["board"][4] == "X"

    server.kill()  # immediately after the client saw it
    server.start()

    async with AsyncExitStack() as stack:
        x = await stack.enter_async_context(open_socket(server, game_id, ALICE))
        snapshot = await recv(x)
        assert snapshot["game"]["board"][4] == "X"
        assert snapshot["game"]["version"] == confirmed["game"]["version"]


async def test_a_game_that_never_started_also_survives(server):
    game_id = await create_game(server, ALICE)
    server.kill()
    server.start()

    async with AsyncExitStack() as stack:
        # the creator keeps X across the restart even though nobody had connected
        x = await stack.enter_async_context(open_socket(server, game_id, ALICE))
        assert (await recv(x))["you"] == {"role": "player", "mark": "X"}
        o = await stack.enter_async_context(open_socket(server, game_id, BOB))
        assert (await recv(o))["you"] == {"role": "player", "mark": "O"}


# --- concurrency -----------------------------------------------------------

async def test_two_tabs_racing_one_cell_produce_exactly_one_move(server):
    """Matrix 12, over real sockets rather than in-process coroutines."""
    game_id = await create_game(server, ALICE)

    async with AsyncExitStack() as stack:
        x, o = await seat_both(stack, server, game_id)
        x2 = await stack.enter_async_context(open_socket(server, game_id, ALICE))
        await recv(x2)                    # snapshot: same seat, second tab
        await recv(x), await recv(o)      # roster changed

        await asyncio.gather(move(x, 4), move(x2, 4))

        # the observer is the honest witness: exactly one state, then silence
        landed = await recv(o)
        assert landed["type"] == "state"
        assert landed["game"]["board"][4] == "X"
        assert landed["game"]["version"] == 2  # 1 = started, 2 = one move
        await assert_silent(o)


async def test_twenty_concurrent_games_stay_independent(server):
    async with AsyncExitStack() as stack:
        games = []
        for i in range(20):
            x_sid, o_sid = f"{'x' * 40}{i:03d}", f"{'o' * 40}{i:03d}"
            game_id = await create_game(server, x_sid)
            x, o = await seat_both(stack, server, game_id, x_sid, o_sid)
            games.append((game_id, x, o, i % 9))

        await asyncio.gather(*(move(x, cell) for _, x, _, cell in games))

        for game_id, x, o, cell in games:
            from_x, from_o = await recv(x), await recv(o)
            assert from_x["game"] == from_o["game"]
            assert from_x["game"]["id"] == game_id
            assert from_x["game"]["board"][cell] == "X"
            # exactly one mark: no other game's move leaked in
            assert sum(1 for c in from_x["game"]["board"] if c != ".") == 1


# --- hostile clients -------------------------------------------------------

async def test_a_flooding_client_cannot_disturb_another_game(server):
    victim_id = await create_game(server, CAROL)
    hostile_id = await create_game(server, ALICE)

    async with AsyncExitStack() as stack:
        vx, vo = await seat_both(stack, server, victim_id, CAROL, DAVE)

        attacker = await stack.enter_async_context(open_socket(server, hostile_id, ALICE))
        await recv(attacker)
        for _ in range(150):
            await attacker.send("}{ not json")
        refusals = await drain(attacker, timeout=1.0)
        assert refusals, "the server should have refused each frame"
        assert all(m["type"] == "error" for m in refusals)

        # the other game is completely unaffected
        await move(vx, 0)
        assert (await recv(vx))["game"]["board"][0] == "X"
        assert (await recv(vo))["game"]["board"][0] == "X"

        # and the flooding socket is still usable, having lost nothing but time
        await move(attacker, 0)
        assert (await recv(attacker))["code"] == "game_not_started"


async def test_a_copied_session_cookie_is_the_player(server):
    """A known, documented limitation rather than a surprise.

    The session cookie is a bearer token: whoever holds it is that player. This
    is the same mechanism that lets your own second tab share your seat, so it
    cannot be closed without giving up multi-tab support. Fixing it properly
    means accounts.
    """
    game_id = await create_game(server, ALICE)

    async with AsyncExitStack() as stack:
        x, o = await seat_both(stack, server, game_id)
        thief = await stack.enter_async_context(open_socket(server, game_id, ALICE))
        assert (await recv(thief))["you"] == {"role": "player", "mark": "X"}
        await recv(x), await recv(o)

        await move(thief, 0)
        assert (await recv(thief))["game"]["board"][0] == "X"
