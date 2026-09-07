# Design note

## Where game state lives

**SQLite, and nowhere else.** There is no in-memory game cache. The process
holds open sockets; it holds no game state at all.

The roadmap originally called for a cache with lazy load from disk. I dropped it:
a single-row read is microseconds, so it bought nothing measurable, and it
created a second source of truth for the one piece of state the service exists to
protect. Without it, "in-progress games survive a server restart" stops being a
feature that has to work and becomes a property of the design — there is no code
path that could lose a game because there is nowhere else for one to be.

*Rejected:* **Postgres** — more production-shaped, but adds a service to a clean
checkout and a pool to configure, and tests nothing the assignment asks about.
*Rejected:* **Redis** — the right answer the moment there is more than one server
process, but we are one process, and its durability needs explicit configuration.
*Rejected:* **Event sourcing** — replaying moves to derive the board is elegant
and gives a free audit log. I store the board directly *and* keep an append-only
`moves` table, which is most of the benefit without the replay machinery.

## How server authority is enforced

Three layers, each blind to the one above it. `game.py` owns the rules and knows
nothing about sessions. `store.py` is the only thing that can turn a cookie into
a mark. `hub.py` owns sockets and knows about neither.

Authority rests on three things:

**1. One mutation point.** The board changes only inside a pure
`apply_move(state, mark, cell)`. State is a frozen dataclass with a tuple board,
so an accepted move returns new state rather than editing old state, and the
socket layer physically cannot write to a board.

**2. The mark is never client-supplied.** It is resolved from the session cookie
against the seat table **on every message**, not read from the connection. A role
captured at connect time is a cached copy of authority, and cached authority goes
stale; `Connection.role` exists only for presence and the UI.

**3. The attack is unrepresentable, not merely rejected.** The entire client
vocabulary is `{"type": "move", "cell": N}`, validated with Pydantic
`strict=True, extra="forbid"`. Strict mode means `true` does not become cell 1 and
`"0"` does not become cell 0 — lenient parsing is how adversarial input becomes a
legal move. `extra="forbid"` means `{"cell":0,"mark":"O"}` is a hard error rather
than a field we quietly ignore. **There is no field in which to name a player**,
so "move as the other player" is not a request that can be made.

Refusals go to the offender only and never close the socket: a buggy client
should not lose its game over one bad frame, and a malicious one gains nothing by
staying, since every message is re-authorised.

## Reconnection and spectators

Both fall out of one line:

> **A seat is owned by a session, not by a connection.**

A session is an opaque 32-byte id in an httpOnly, SameSite=Lax cookie. A seat is
a row mapping `(game_id, mark) → session`. A connection is an open socket that
knows which session it belongs to. Many connections may share one seat.

**Reconnection is therefore not a feature.** Dropping a socket runs
`hub.leave()` and touches nothing else. Reconnecting calls the same idempotent
`join()` a first-time player calls: it returns the seat this session already
holds. There is no reconnect branch, so there is no reconnect branch to be broken
on the unhappy path.

**A spectator is the absence of a seat**, computed at connect time, never a
stored flag. No code path can produce a spectator who can move, because moving
requires a seat row to exist.

Two schema constraints carry the model rather than the code above them:
`PRIMARY KEY (game_id, mark)` caps a game at two seats, and
`UNIQUE (game_id, session)` stops one session holding both.

*Consequence accepted:* a cookie is per browser profile, so two tabs are the same
player sharing one seat. Playing both sides needs a private window. That is the
same mechanism that makes reconnection work.
*Rejected:* **a seat token in `sessionStorage`** — per-tab, so both players could
be demoed in one browser, but readable by any script on the page, and "the client
tells us who it is" is the exact shape of thing being attacked here.

## How persistence works

**Commit before broadcast.** `store.play()` resolves the seat, asks the rules,
writes to SQLite, and only then returns the new state for the hub to fan out. A
client can never observe a move that a restart would forget.

SQLite runs in WAL mode with `synchronous=FULL` — `NORMAL` already survives a
process restart, but the claim being made is about durability and the write
volume is trivial, so the fsyncs are worth more than the microseconds.

Honest caveat: the end-to-end test proves a confirmed move survives a hard
`kill()`, but it cannot prove the *ordering*, because flipping commit and
broadcast would leave a window of microseconds that a kill would essentially
never land in. The ordering guarantee comes from the code structure — one
function, that order, no path around it — not from the test.

## Concurrency

One `asyncio.Lock` per game serialises the read-modify-write. Independent games
never contend.

The lock is **currently redundant**: the critical section contains no `await`, so
asyncio cannot preempt it. I kept it because it is the guarantee that survives
someone swapping stdlib `sqlite3` for an async driver, and that failure would be
silent double-moves. The race test says this out loud rather than implying the
lock is doing work.

*Rejected:* **striped locks** to bound the lock table. Correct, but the leak needs
~100k games to reach 13 MB, and `asyncio.Lock` is not reentrant — two distinct
game ids sharing a stripe would deadlock if any future feature held two game
locks at once. ROADMAP §6.1 has the full reasoning.

## Stack

Python 3.13 + FastAPI + uvicorn, stdlib `sqlite3`, and a frontend of plain HTML,
CSS and JS with no build step, so a clean checkout runs with one install command
and one toolchain.

*Rejected:* **Node + TypeScript** — genuinely the stronger technical fit: shared
protocol types across client and server, and a single-threaded event loop that
makes a move handler atomic without any locking. We give both up. Pydantic buys
back validation at the trust boundary, and the per-game lock handles atomicity
explicitly. Being able to explain that tradeoff is worth more than having dodged
it.
*Rejected:* **React** — two package managers and a build step to render nine divs.

## What I would change first

Multi-process support, which is the top limitation. The fix is not to bound the
lock table but to delete it: move the read-modify-write inside a SQLite
`BEGIN IMMEDIATE` on a per-request connection so the database's own lock manager
serialises writers across processes, and move broadcast onto Redis pub/sub so a
player's socket can be on any worker.
