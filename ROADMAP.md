# Realtime Tic-Tac-Toe — Roadmap

Working plan for the take-home. Written before any code, so the decisions are
choices rather than rationalisations. This file is also the raw material for the
required `DESIGN.md`.

---

## 0. How this will actually be graded

Their brief lists features, but the rubric grades five things. Mapping one to the
other, so we build for the rubric and not for the feature list:

| Rubric line | What actually earns the mark | Where we prove it |
|---|---|---|
| Architecture & judgment | Named alternatives we rejected, with reasons | `DESIGN.md` §Decisions |
| Correctness under adversarial input | Automated tests that *attack* the server | `tests/test_adversarial.py` |
| State modeling | Seats/sessions/connections are distinct things, not tangled | `app/models.py`, `DESIGN.md` §State model |
| How you steer the agent | Visible course-correction in the transcript | The session transcript |
| Communication | An honest README that names limitations | `README.md` §Limitations |

Two sentences from the brief that set the bar:

> "We will not penalize an unfinished feature. We will penalize a
> confident-looking thing that's silently broken."

> "A deliberately scoped, correct, well-explained submission beats a broad one
> that breaks under light pressure."

Read together: **cut scope before cutting correctness.** Anything we don't finish
goes in README §What I'd do next, honestly labelled.

---

## 1. Decisions (locked before coding)

Each decision below needs a rejected alternative attached. That's what the design
note is graded on.

### 1.1 Stack — Python 3.13 + FastAPI + uvicorn

FastAPI has first-class WebSocket support, Pydantic gives us schema validation at
the trust boundary for free, and it runs from a clean checkout with
`pip install -r requirements.txt`.

*Rejected:* **Node + TypeScript.** Genuinely the stronger technical fit — shared
protocol types across client and server, and Node's single-threaded event loop
makes a move handler atomic without any locking. We give both of those up. We buy
back the type-safety with Pydantic at the WS boundary, and we handle atomicity
explicitly with a per-game `asyncio.Lock` (§2.3, I6). Being able to explain *that
specific tradeoff* is worth more than having dodged it.

*Rejected:* **Go.** Best raw fit for many concurrent connections. Not installed,
and the concurrency story here is trivial at this scale — the bottleneck is
correctness, not throughput.

### 1.2 Transport — WebSocket, full-state broadcast

One WS per open tab. Every state change broadcasts the **entire game state**, not
a delta.

*Why full state:* the payload is nine cells. Delta-sync is where realtime games
develop drift bugs that only surface after a reconnect — precisely the failure
mode they will probe. Sending everything makes reconnection and first-load the
*same code path* instead of two.

*Rejected:* **Server-Sent Events + POST for moves.** Simpler reconnect semantics
(EventSource auto-retries) and it would work. Costs a second transport to reason
about and doesn't reduce the state work.

*Rejected:* **HTTP polling.** Meets "realtime" only in a weak sense and burns the
"both see moves immediately" requirement.

### 1.3 Persistence — SQLite, write-through on every accepted move

Stdlib `sqlite3` in WAL mode. Every accepted move is committed **before** it is
broadcast (invariant I5 — this is not an implementation detail, it is the whole
answer to "survives a restart").

*Rejected:* **Postgres via Docker Compose.** More production-shaped, but adds a
service to a "clean checkout" run and a connection pool to configure. Buys us
nothing the assignment tests.

*Rejected:* **Redis.** The right answer the moment we run more than one server
process (we would need pub/sub for cross-instance broadcast). We are one process,
and Redis durability needs explicit config — a weaker answer to "survives
restart". Named in the design note as the first thing to change when scaling out.

*Rejected:* **JSON file.** No transactions, torn writes on crash.

*Rejected:* **Event sourcing** (append moves, replay to derive the board). Elegant,
and gives a free audit log. We store the board directly *and* keep an append-only
`moves` table for audit — most of the benefit, none of the replay complexity.

### 1.4 Identity — opaque session id in an httpOnly cookie

Server mints a 32-byte random `sid` on first page load. `httpOnly`,
`SameSite=Lax`, `Secure` when served over TLS, 7-day max-age. The client can never
read it, and **never sends its identity in a message** — the server derives it
from the cookie on the WS handshake.

*Rejected:* **Seat token in sessionStorage.** Per-tab, so both players could be
demoed in one browser. But it is readable by any script on the page, and "the
client tells us who it is" is exactly the shape of thing they intend to attack.
The cookie is the more defensible model.

*Consequence we accept and document:* a cookie is per-browser-profile, so two tabs
in the same browser are the **same** player. To play both sides yourself you need
a private window. This falls out of the model rather than being a bug: a seat is
owned by a session, and one session may have many tabs open — which is also
exactly why reconnection works.

*Rejected:* **Accounts / login.** Out of scope; the assignment implies link-based
access.

### 1.5 Seat claiming — implicit, first-come

Creator gets X at creation. The next distinct session to open the link gets O.
Everyone after that is a spectator. No "join" button.

*Why:* matches the brief's story exactly — "start a game, get a shareable link,
send it to someone". An explicit join step is one more state to model and test.

*Consequence we document:* paste the link in a group chat and whoever clicks first
takes the seat. An explicit "Take the open seat / Just watch" prompt is the fix,
listed under What I'd do next.

### 1.6 Frontend — static HTML/CSS/vanilla JS, no build step

Served by FastAPI from `static/`.

*Why:* "runnable from a clean checkout" stays literally true — no npm, no bundler,
no second toolchain in a Python repo. The UI is a 3x3 grid and a status line; a
framework earns nothing here.

*Rejected:* **React + Vite.** Two package managers and a build step in the run
instructions, to render nine divs.

---

## 2. State model

The part they will interrogate hardest. Four distinct concepts — the failure mode
is conflating any two of them.

### 2.1 Entities

**Session** — an opaque `sid` in a cookie. Identifies a *browser profile*, not a
person and not a connection. Long-lived.

**Game** — `id`, `board` (9 cells of `.`/`X`/`O`), `turn`, `status`
(`waiting` / `in_progress` / `finished`), `result` (`x_won`/`o_won`/`draw`/null),
`winning_line`, `version` (monotonic int), timestamps.

**Seat** — a mapping `(game_id, mark) -> sid`. At most two per game. A uniqueness
constraint on `(game_id, sid)` stops one session holding both seats.

**Connection** — an open WebSocket belonging to `(game_id, sid)`. **Many
connections may share one seat.** Ephemeral; never persisted.

### 2.2 The load-bearing idea

> A seat is owned by a **session**, not by a **connection**.

Everything else follows from this one line:

- **Reconnection** is not a feature. Closing a socket does not touch the seat, so
  reopening one and finding your seat again is just the normal path. There is no
  "reconnect" branch to get wrong. (This is the answer to their "does reconnection
  actually work, or just appear to on the happy path?")
- **Spectator** is not a stored role. It is *the absence of a seat*, computed at
  connect time. No code path can produce a spectator who can move, because moving
  requires a seat row to exist.
- **Multiple tabs** are coherent by construction: same session, same seat, both
  tabs receive every broadcast.

### 2.3 Invariants (each one becomes a test)

- **I1 — Single mutation point.** The board changes only inside a pure
  `apply_move(game, mark, cell) -> Ok | Rejected`. The WebSocket layer never
  touches board state. Rules are unit-testable with no server running.
- **I2 — Identity is never client-supplied.** No message contains a mark or a
  player id. The client can only ever say `{"type":"move","cell":N}`. The mark is
  looked up server-side from cookie → seat, on every single message.
- **I3 — Seats survive disconnects.** See §2.2.
- **I4 — Spectator = no seat.** Not a flag.
- **I5 — Commit before broadcast.** A move is written to SQLite and only then sent
  to clients. No client can ever observe a move that a restart would forget.
- **I6 — One `asyncio.Lock` per game.** Serialises the read-modify-write of a
  move. Two moves arriving in the same event-loop tick cannot both pass the
  "cell is empty" check. *(This is the lock Node would have given us for free —
  §1.1.)*
- **I7 — Monotonic `version`.** Every broadcast carries it; clients discard any
  state with `version <= current`. Kills out-of-order rendering after a reconnect.

### 2.4 Schema

```sql
games(id TEXT PK, board TEXT, turn TEXT, status TEXT, result TEXT,
      winning_line TEXT, version INTEGER, created_at, updated_at)

seats(game_id TEXT, mark TEXT, sid TEXT, claimed_at,
      PRIMARY KEY(game_id, mark), UNIQUE(game_id, sid))

moves(game_id TEXT, seq INTEGER, mark TEXT, cell INTEGER, at,
      PRIMARY KEY(game_id, seq))          -- append-only audit
```

### 2.5 Wire protocol

Client to server — the entire client vocabulary:

```json
{"type": "move", "cell": 0}
```

Server to client:

```json
{"type": "snapshot", "you": {"role": "player", "mark": "X"}, "game": {}, "presence": {}}
{"type": "state", "game": {}, "presence": {}}
{"type": "error", "code": "not_your_turn", "message": "..."}
```

Errors go **only to the offender**, never broadcast. `snapshot` is sent once on
connect and tells you your role; `state` is every subsequent change.

### 2.6 HTTP surface

```
GET  /                 home, "New game" button
POST /api/games        create game, claim seat X, -> {id, url}
GET  /g/{id}           game page (mints the cookie if absent)
GET  /api/games/{id}   JSON state — for tests and debugging
WS   /ws/{id}          realtime channel
```

---

## 3. Build phases

Each phase has an **exit criterion** — something observable, not "looks done".
After each one I write a short explanation of what was built and why, so you can
walk the reviewer through it.

**Working agreement:** at every phase gate I state what I would challenge about my
own output, and you push back where you disagree. That back-and-forth *is* the
"how you steer the agent" deliverable — it needs to be in the transcript, not just
in our heads.

### Phase 0 — Skeleton (15 min)

`requirements.txt`, package layout, `.gitignore`, uvicorn boots, `/` returns a page.

**Exit:** `uvicorn app.main:app` serves a page on localhost.

### Phase 1 — Domain core, pure and offline (25 min)

`app/game.py`: board representation, `apply_move`, win/draw detection, all eight
winning lines. Zero imports from FastAPI or sqlite. Unit tests alongside.

**Exit:** `pytest tests/test_game.py` green, covering every win line, the draw, and
each rejection reason.

**Why first:** the rules are the only part that must be *provably* right. Isolating
them from transport means the adversarial tests later are testing the *wiring*,
not re-testing the rules.

### Phase 2 — Persistence (25 min)

`app/store.py`: schema init, WAL, create/load/save game, claim seat, per-game
`asyncio.Lock`, in-memory cache of loaded games with lazy load from disk.

**Exit:** a test that writes a game, drops the cache, reloads from the file, and
gets identical state.

### Phase 3 — HTTP + sessions (20 min)

Cookie minting, `POST /api/games`, `GET /g/{id}`, `GET /api/games/{id}`, seat
claiming (§1.5).

**Exit:** create a game with curl, hit the page with two different cookie jars,
see X and O claimed; a third jar sees spectator.

### Phase 4 — WebSocket hub + server authority (40 min) — *the core*

Connection registry per game, connect → resolve role from cookie → send snapshot,
`move` handler under the lock, commit-then-broadcast, disconnect cleanup, presence.

**Exit:** two scripted `websockets` clients play a full game; a third watches and
is refused a move.

**Why this phase matters most:** every requirement they named — authority,
realtime, reconnection, spectators — lands here or nowhere.

### Phase 5 — Frontend (25 min)

Grid, status line, copy-link button, role badge, WS client with
exponential-backoff reconnect, `version` guard, board disabled when it is not your
turn.

**Exit:** two browser windows play a real game; refreshing mid-game restores it.

*Note:* the UI disabling a cell is a **hint, not a rule**. The server rejects it
regardless — that is the point of I1/I2.

### Phase 6 — Adversarial + restart tests (30 min)

The matrix in §4.

**Exit:** full suite green, including the kill-and-restart test.

### Phase 7 — Deliverables (25 min)

`README.md` (run instructions, what's done, what's not, limitations), `DESIGN.md`
(~1 page, distilled from §1 and §2), export the transcript.

**Exit:** a clean `git clone` → follow the README → working game.

**Running total: ~3h05.** That is the whole budget.

### Phase 8 — Rematch / lobby / chat (GATED, +~2h)

Only after Phase 7 is complete and committed. Re-read §0 before starting — the
brief argues against this and so do I. If we do it: rematch first (cheapest,
arguably part of "a clear end state"), lobby and chat after.

---

## 4. Adversarial test matrix

They said they will try to make illegal moves, move out of turn, and move as the
other player. So we test that first, and ship the proof.

| # | Attack | Expected |
|---|---|---|
| 1 | Move on an occupied cell | rejected, board + version unchanged |
| 2 | X moves twice in a row | rejected `not_your_turn` |
| 3 | Spectator sends a move | rejected `not_a_player` |
| 4 | Move as the other player | **impossible to express** — no mark in the protocol (I2) |
| 5 | Forged / garbage cookie | treated as a new session → spectator, never inherits a seat |
| 6 | No cookie at all | spectator |
| 7 | Move after the game is finished | rejected `game_over` |
| 8 | `cell` = -1, 9, 1.5, "0", null, missing | rejected, connection survives |
| 9 | Malformed JSON / oversized frame | rejected, server does not crash |
| 10 | Move on a game id that does not exist | rejected, no game created as a side effect |
| 11 | Third session tries to claim a seat | spectator |
| 12 | Two moves fired in the same tick | exactly one applies (I6) |
| 13 | Same session, two tabs | same seat, both in sync, no theft |
| 14 | Two concurrent games | zero cross-talk — no id leaks between them |
| 15 | Play 3 moves → kill server → restart → reconnect | same board, seats, turn; game continues |

Row 4 is worth calling out in the design note: the strongest defence against "move
as the other player" is not validation, it is making the attack
**unrepresentable in the protocol**.

---

## 5. Deliverables checklist

- [ ] Code, runnable from a clean checkout
- [ ] `README.md` — how to run, what's done, what's next, **honest limitations**
- [ ] `DESIGN.md` — ~1 page: where state lives, how authority is enforced, how
      reconnection and spectators are modeled, how persistence works, plus
      rejected alternatives
- [ ] Agent transcript(s) exported
- [ ] Clean git history with meaningful commits (evidence of how it was built)

---

## 6. Known limitations to state honestly in the README

Written down now so they land in the README as deliberate disclosure rather than
getting discovered by the reviewer.

1. **Single process only.** The connection registry is in-process memory. Two
   uvicorn workers means players in the same game on different workers never see
   each other. Fix: Redis pub/sub, or route by game id. *We must not silently ship
   a multi-worker command in the README.*
2. **The session cookie is a bearer token.** Whoever holds it is that player.
   Copying the cookie steals the seat. Real fix is accounts; out of scope.
3. **No abandonment handling.** A player who never returns leaves the game open
   forever. No timeouts, no forfeit, no cleanup of old games.
4. **No rate limiting.** A client can spam moves; each is cheap and rejected, but
   there is no backpressure.
5. **Seat claiming is first-come** (§1.5) — a curious spectator can take a seat.
6. **SQLite writes run on the event loop.** Sub-millisecond at this size, so
   deliberate; would move to a thread executor under real load.
7. **The per-game lock table is unbounded.** One `asyncio.Lock` per game id ever
   seen, never evicted. Measured rather than guessed: a lock is 136 bytes, so
   100k games costs ~13 MB and 1M games ~130 MB. Deliberately not fixed — see
   below.

### 6.1 Why the lock table is not fixed

Considered and rejected, recorded because "we didn't get to it" and "we decided
not to" are different answers.

*Rejected:* **striped locks** — a fixed array of 1024 locks indexed by
`hash(game_id) % 1024`. Correct (a coarser lock is a superset; collisions cost
false contention, never a wrong answer), hard-bounded, and with no eviction
logic to get wrong. Not built because the leak needs ~100k games to be worth a
paragraph, and because `asyncio.Lock` is not reentrant: two *distinct* game ids
sharing a stripe would deadlock instantly if any future feature ever held two
game locks at once, and non-deterministically, since Python's string hash is
randomised per process.

*Rejected:* **`weakref.WeakValueDictionary`** — verified to work (entries evict
once idle, a waiter keeps its lock alive, mutual exclusion holds), and bounded by
*live* games rather than games ever seen. Rejected because its correctness rests
on the caller holding a strong reference, an invariant nothing in the code makes
visible. Striping is the better fix precisely because it is dumber.

*The real answer, when it matters:* per-process locks are worthless the moment
limitation 1 is addressed, because two workers do not share a lock table. Moving
the read-modify-write inside a SQLite `BEGIN IMMEDIATE` on a per-request
connection lets the database's own lock manager serialise writers across
processes, and deletes the lock table rather than bounding it. That is the change
to make, and it is not a memory optimisation.

---

## 7. Revisions to this plan

Kept as a log rather than edited in place, so the reasoning stays visible.

**Phase 1 — dropped timestamps from `GameState`.** §2.1 listed `created_at` /
`updated_at` on the game. A domain module that calls `datetime.now()` is
non-deterministic to test, and clocks are a persistence concern. They live on the
`games` row instead; the pure rules never see a clock.

**Phase 3 — seats are claimed on WebSocket connect, not on page load.** §1.5 said
"the next distinct session to open the link gets O", and Phase 3's exit criterion
was written as two cookie jars loading the page and taking X and O. That design
hands seat O to a link-preview crawler: paste the invite into Slack, WhatsApp or
iMessage and their unfurler fetches `/g/{id}`, so the human who clicks arrives as
a spectator to their own game. It also violates HTTP's rule that GET has no side
effects. The page load now only mints a cookie; the seat is claimed when a real
browser opens a socket, which no crawler does. First-come seat claiming (§1.5) is
otherwise unchanged.

**Phase 2 — dropped the in-memory game cache.** §2 planned "an in-memory cache of
loaded games with lazy load from disk". Removed: a single-row SQLite read is
microseconds, so the cache bought nothing measurable, and it introduced a second
source of truth for the one piece of state the whole service is about. Without
it, "in-progress games survive a server restart" stops being a feature that has
to work and becomes a property of the design — the process holds sockets and
nothing else. The per-game lock stayed.

