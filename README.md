# Realtime multiplayer tic-tac-toe

Start a game, get a link, send it to someone, play in realtime. The server owns
the game state and validates every move; the browser is a renderer.

- **Design note:** [DESIGN.md](DESIGN.md) — decisions and rejected alternatives
- **Build log:** [ROADMAP.md](ROADMAP.md) — the plan, plus a log of where the
  plan changed and why

## Run it

Needs Python 3.11+. Nothing else — no database server, no npm, no build step.

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt        # Windows
.venv/Scripts/python -m uvicorn app.main:app --port 8000
```

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt            # macOS / Linux
.venv/bin/python -m uvicorn app.main:app --port 8000
```

Open <http://localhost:8000>, click **New game**, and send the link to someone.

> **Do not run with `--workers 2` or more.** The connection registry is
> in-process memory, so two workers means two players in the same game may never
> see each other. See [Limitations](#limitations).

The SQLite file is created at `data/games.db`, anchored to the repo rather than
your working directory. Override with `TTT_DB=/some/path.db`.

### Playing both sides yourself

Open the invite link in a **private window** or a second browser. Identity is an
httpOnly cookie, which is per browser profile — so two tabs of the same browser
are deliberately the *same* player, sharing one seat and staying in sync. That
is the same mechanism that makes reconnection work; see DESIGN.md.

## Tests

```bash
.venv/Scripts/python -m pytest
```

112 tests, about 20 seconds (most of that is the end-to-end suite starting and
killing real server processes).

| File | Tests | What it covers |
|---|---|---|
| `tests/test_game.py` | 45 | Rules in isolation: every win line, draws, every rejection |
| `tests/test_store.py` | 18 | Seat ownership, forged sessions, racing moves, restart |
| `tests/test_http.py` | 13 | Cookie attributes, link-preview crawlers, malformed ids |
| `tests/test_ws.py` | 29 | Server authority, adversarial frames, reconnection, isolation |
| `tests/test_endtoend.py` | 7 | Real uvicorn process: hard kill + restart, 20 concurrent games, flooding client |

The end-to-end suite kills the server with `Popen.kill()`, never `terminate()` —
a graceful shutdown would let the app flush on the way out and prove nothing
about durability.

## What's done

- **Create and share.** `POST /api/games` seats you as X and returns an
  unguessable link.
- **Realtime play.** Both sides see moves immediately over one WebSocket per tab.
- **Server authority.** Every move is re-authorised from the session cookie
  against the seat table. Illegal moves, out-of-turn moves, moves by spectators
  and moves on finished games are all refused. "Move as the other player" is not
  expressible in the protocol — see DESIGN.md.
- **Win/draw detection** with the winning line highlighted and a clear end state.
- **Reconnection.** Refresh, drop your connection, or restart the server — you
  rejoin the same game in its current state with your seat intact. The client
  reconnects on its own with exponential backoff.
- **Concurrent games.** Independent games, independent locks, no cross-talk.
- **Spectators.** Anyone with the link who is not one of the two players watches
  live and cannot move.
- **Persistence.** Games survive an abrupt process kill.
- **Responsive UI.** Works on a phone, light and dark, keyboard accessible,
  screen-reader labelled.

## What I'd do next

Roughly in the order I would actually do them:

1. **Browser-level tests.** The protocol behaviour is fully automated, but the
   UI claims above (reconnect banner, pending-move state, board disabling) rest
   on manual verification. Playwright would close that gap. *This is the
   honest hole in the current suite.*
2. **Multi-process support.** Move the read-modify-write into a SQLite
   `BEGIN IMMEDIATE` on a per-request connection and put broadcast on Redis
   pub/sub. That deletes the in-process lock table rather than bounding it.
3. **Abandonment handling.** Timeouts, forfeits, and cleanup of games nobody
   returns to.
4. **A rematch button.** Right now a finished game is a dead end.
5. **An explicit "take the open seat / just watch" prompt**, instead of
   first-come seat claiming.
6. **Rate limiting and backpressure** on the socket.

## Limitations

Stated up front rather than left to be discovered.

1. **Single process only.** The connection registry lives in process memory. Two
   uvicorn workers means players in the same game on different workers never see
   each other. There is no guard against this beyond the warning above.
2. **The session cookie is a bearer token.** Whoever holds it is that player.
   Copying it steals the seat — there is a test asserting exactly this. It is
   the same mechanism that lets your own second tab share your seat, so it
   cannot be closed without giving up multi-tab support. The real fix is
   accounts.
3. **Seats are first-come.** Paste the link into a group chat and whoever clicks
   first takes the seat. (Link-preview crawlers *cannot*, because seats are
   claimed on WebSocket connect, not on page load.)
4. **No abandonment handling.** A player who never returns leaves the game open
   forever. Nothing is ever cleaned up, so the database grows without bound.
5. **No rate limiting or backpressure.** A client can spam moves; each is cheap
   and refused, but nothing caps it, and one slow socket delays a broadcast.
6. **The per-game lock table is unbounded** — one lock per game id ever seen,
   never evicted. Measured at 136 bytes each, so ~13 MB at 100k games.
   Deliberately not fixed; ROADMAP §6.1 has the striped-lock design I rejected
   and the reason.
7. **Half-open connection detection relies on uvicorn's default ping interval**
   rather than anything I configured, so presence can briefly show a ghost.
8. **SQLite writes run on the event loop.** Sub-millisecond at this size; would
   move to a thread executor under real load.
9. **The test suite uses `starlette.testclient`**, which now emits a deprecation
   warning in favour of `httpx2`. Works today.

## Agent transcript

Built with Claude Code. The raw session log is one JSON object per line, so it is
exported to readable Markdown at [transcript/session.md](transcript/session.md):

```bash
python tools/export_transcript.py --list                       # find session logs
python tools/export_transcript.py <session.jsonl> -o transcript/session.md
```

Tool arguments and results are clipped so long file writes show their shape
rather than their contents. Regenerate after the final change so the export
covers the whole session.

## Layout

```
app/
  game.py     pure rules - no FastAPI, no sqlite, no asyncio
  store.py    seats, persistence, per-game locks
  hub.py      connection registry and fan-out
  main.py     HTTP surface, session cookie, WebSocket endpoint
static/       no build step; plain HTML, CSS and JS
tests/
tools/
  export_transcript.py   turns a Claude Code session log into readable markdown
```

Each layer is blind to the one above it: the rules know nothing about sessions,
the store knows nothing about sockets, the hub knows nothing about seats.
