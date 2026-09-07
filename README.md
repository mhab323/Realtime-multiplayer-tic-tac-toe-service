# Realtime multiplayer tic-tac-toe

Start a game, get a link, send it to someone, play in realtime. The server owns
the game state and validates every move; the browser is a renderer.

- **Design note:** [DESIGN.md](DESIGN.md) — decisions and rejected alternatives

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

138 tests, about 20 seconds (most of that is the end-to-end suite starting and
killing real server processes).

| File | Tests | What it covers |
|---|---|---|
| `tests/test_game.py` | 55 | Rules in isolation: every win line, draws, every rejection, rematch |
| `tests/test_store.py` | 25 | Seat ownership, forged sessions, racing moves, restart, schema migration |
| `tests/test_http.py` | 15 | Cookie attributes, link-preview crawlers, malformed ids, QR |
| `tests/test_ws.py` | 36 | Server authority, adversarial frames, reconnection, isolation, rematch |
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
- **Rematch.** Either player can offer one when a game ends; it starts only when
  both agree. Seats never change hands, and the first move alternates by round so
  the same player does not keep X's advantage.
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
4. **An explicit "take the open seat / just watch" prompt**, instead of
   first-come seat claiming.
5. **Rate limiting and backpressure** on the socket.

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
   Deliberately not fixed — see DESIGN.md for the striped-lock alternative I
   rejected and why.
7. **Half-open connection detection relies on uvicorn's default ping interval**
   rather than anything I configured, so presence can briefly show a ghost.
8. **SQLite writes run on the event loop.** Sub-millisecond at this size; would
   move to a thread executor under real load.
9. **The test suite uses `starlette.testclient`**, which now emits a deprecation
   warning in favour of `httpx2`. Works today.

## Playing on a phone — packaged build

> **Not part of the assignment.** This is a demo convenience: a standalone
> executable that serves the game to other devices on your Wi-Fi. It adds no
> game features and the service itself is unchanged.

```powershell
.\.venv\Scripts\python -m pip install -r packaging\requirements-packaging.txt
.\packaging\build.ps1
.\dist\realtime-ttt.exe
```

Produces a single ~17 MB `dist/realtime-ttt.exe` with Python bundled in. It binds
`0.0.0.0` and prints both addresses:

```
   On this computer : http://localhost:8000
   On your phone    : http://192.168.1.20:8000   (same Wi-Fi)
```

Open the invite on your laptop and **scan the QR code** with the phone's camera.
That is not decoration: `http://192.168.x.x` is not a *secure context*, so the
Clipboard API is unavailable on exactly the device you most want to play on. The
QR is rendered server-side at `/g/{id}/qr.svg`, so it needs no JavaScript library
and works offline. `segno` is optional — without it the endpoint 404s and the UI
hides the QR rather than showing a broken image.

Notes:

- Windows will ask to allow the app through the firewall. Say yes for **private**
  networks, or the phone cannot connect.
- The database is written to `dist/data/games.db`, **next to the executable**.
  A one-file PyInstaller build unpacks itself into a temp directory that is
  deleted on exit, so a database written there would be silently wiped on every
  run. `app/main.py` resolves read-only assets through `sys._MEIPASS` and
  writable data through `sys.executable`.
- Binding `0.0.0.0` exposes the service to everyone on your network. It is a
  tic-tac-toe game with no accounts, but it is not nothing.
- Set `TTT_PORT` to use a different port.
- The exe is unsigned, so Windows SmartScreen will warn on first run.

### A public link, without deploying

For a link that works from any phone rather than just your own Wi-Fi, put a
tunnel in front of the local server:

```powershell
winget install Cloudflare.cloudflared
cloudflared tunnel --url http://localhost:8000
```

It prints an `https://….trycloudflare.com` URL. Because that is a *secure
context*, the clipboard button works and the session cookie is issued with
`Secure` — neither of which is true over plain http on a LAN.

Verified through a live tunnel: `https://` invite links, the QR endpoint, and a
full game played over `wss://`.

The link lives only while `cloudflared` is running, changes every time, and
exposes your local server to anyone who has it.

**On proxies:** uvicorn trusts `X-Forwarded-Proto` from `127.0.0.1` by default,
which is exactly where `cloudflared` connects from, so invite links come out as
`https://` with no extra flags. On a hosted platform the proxy is *not* on
localhost, so you would need `--forwarded-allow-ips="*"` — without it the invite
link and QR code would encode `http://` on an https page.

**Serverless platforms cannot host this** — Vercel, Netlify, Cloudflare Workers.
Not a configuration problem: there is no long-lived process to hold a WebSocket,
the filesystem is ephemeral so SQLite is wiped, and instances share no memory so
two players in one game can land on different ones. This needs a persistent
process — a container host such as Fly.io, Render or Railway.

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
packaging/              beyond the assignment - standalone .exe for phone play
  launcher.py
  build.ps1
```

Each layer is blind to the one above it: the rules know nothing about sessions,
the store knows nothing about sockets, the hub knows nothing about seats.
