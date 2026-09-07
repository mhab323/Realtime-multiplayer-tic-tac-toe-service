"""HTTP surface: session cookies, game creation, and what a page load must not do.

Each `browser(app)` is an independent cookie jar, which is the whole point —
identity here is a browser profile, so two AsyncClients model two people.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import SESSION_COOKIE, create_app


@pytest.fixture
def app(tmp_path):
    application = create_app(tmp_path / "games.db")
    yield application
    application.state.store.close()


def browser(app) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


async def new_game(client) -> str:
    res = await client.post("/api/games")
    assert res.status_code == 201
    return res.json()["id"]


# --- sessions --------------------------------------------------------------

async def test_session_cookie_is_httponly_and_lax(app):
    async with browser(app) as c:
        res = await c.get("/")
        header = res.headers["set-cookie"].lower()
        assert "httponly" in header
        assert "samesite=lax" in header
        assert "max-age=604800" in header
        # Secure must NOT be set over plain http, or the browser drops it and
        # every seat silently breaks on localhost.
        assert "secure" not in header


async def test_cookie_is_stable_across_requests(app):
    async with browser(app) as c:
        await c.get("/")
        first = c.cookies[SESSION_COOKIE]
        res = await c.get("/")
        assert "set-cookie" not in res.headers  # not re-minted
        assert c.cookies[SESSION_COOKIE] == first


async def test_two_browsers_get_different_sessions(app):
    async with browser(app) as a, browser(app) as b:
        await a.get("/")
        await b.get("/")
        assert a.cookies[SESSION_COOKIE] != b.cookies[SESSION_COOKIE]


async def test_malformed_cookie_is_replaced(app):
    async with browser(app) as c:
        c.cookies.set(SESSION_COOKIE, "not a valid session!!")
        res = await c.get("/")
        assert "set-cookie" in res.headers


async def test_wellformed_but_unknown_cookie_is_kept_and_holds_no_seat(app):
    """A forged session is not an error. It is simply a session with no seat."""
    async with browser(app) as owner:
        game_id = await new_game(owner)

    async with browser(app) as attacker:
        attacker.cookies.set(SESSION_COOKIE, "A" * 43)  # right shape, never issued
        res = await attacker.get(f"/api/games/{game_id}")
        assert "set-cookie" not in res.headers
        assert res.json()["you"] == {"role": "spectator", "mark": None}


# --- creating a game -------------------------------------------------------

async def test_creating_a_game_seats_the_creator_as_x(app):
    async with browser(app) as alice:
        game_id = await new_game(alice)
        body = (await alice.get(f"/api/games/{game_id}")).json()
        assert body["you"] == {"role": "player", "mark": "X"}
        assert body["game"]["status"] == "waiting"


async def test_invite_url_points_at_the_game_page(app):
    async with browser(app) as alice:
        created = (await alice.post("/api/games")).json()
        assert created["url"].endswith(f"/g/{created['id']}")
        assert (await alice.get(f"/g/{created['id']}")).status_code == 200


# --- the thing a page load must not do -------------------------------------

async def test_opening_the_page_does_not_claim_a_seat(app):
    """GET must not have side effects, and link-preview crawlers fetch URLs.

    If loading /g/{id} claimed a seat, pasting the invite into Slack or WhatsApp
    would hand seat O to their unfurler, and the human who clicked would arrive
    as a spectator to their own game.
    """
    async with browser(app) as alice:
        game_id = await new_game(alice)

    async with browser(app) as crawler:
        assert (await crawler.get(f"/g/{game_id}")).status_code == 200
        assert (await crawler.get(f"/g/{game_id}")).status_code == 200

    async with browser(app) as anyone:
        body = (await anyone.get(f"/api/games/{game_id}")).json()
        assert body["game"]["status"] == "waiting"  # seat O still open
        assert body["you"]["role"] == "spectator"


async def test_creator_keeps_x_while_strangers_look(app):
    async with browser(app) as alice, browser(app) as bob:
        game_id = await new_game(alice)
        await bob.get(f"/g/{game_id}")
        await bob.get(f"/api/games/{game_id}")
        body = (await alice.get(f"/api/games/{game_id}")).json()
        assert body["you"] == {"role": "player", "mark": "X"}


# --- QR invites (optional dependency) --------------------------------------

async def test_the_qr_endpoint_renders_the_invite_link(app):
    async with browser(app) as alice:
        game_id = await new_game(alice)
        res = await alice.get(f"/g/{game_id}/qr.svg")
        if res.status_code == 404:
            pytest.skip("segno not installed; the app degrades to no QR")
        assert res.headers["content-type"].startswith("image/svg+xml")
        assert res.text.lstrip().startswith("<svg")


async def test_the_qr_endpoint_refuses_unknown_games(app):
    async with browser(app) as c:
        assert (await c.get("/g/missing-game/qr.svg")).status_code == 404
        assert (await c.get("/g/bad.id/qr.svg")).status_code == 404


# --- missing and malformed ids --------------------------------------------

# "missing-game" is well-formed but was never issued; the rest fail the id
# pattern. A traversal string is not in this list on purpose: httpx normalises
# `..` away before the request is sent, so it would assert nothing.
@pytest.mark.parametrize("game_id", ["missing-game", "bad.id.here", "x", "a" * 200])
async def test_unknown_or_malformed_game_ids_are_404(app, game_id):
    async with browser(app) as c:
        assert (await c.get(f"/api/games/{game_id}")).status_code == 404
        assert (await c.get(f"/g/{game_id}")).status_code == 404
