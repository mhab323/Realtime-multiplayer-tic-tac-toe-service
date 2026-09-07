/* Realtime client.
 *
 * The WebSocket snapshot is the only source of truth for this page. There is no
 * fetch of /api/games/{id} on purpose: a second source would render a role
 * before the socket confirmed one, and "you are watching" flashing on screen
 * before "you are O" is exactly the wrong thing to be wrong about.
 *
 * Nothing here is security. The board disables itself when it is not your turn
 * because a dead-looking button is honest UI, not because it stops anything.
 * The server refuses every illegal move regardless of what this file believes.
 */

const GAME_ID = window.location.pathname.split("/").pop();
const FATAL = new Set(["no_such_game", "no_session"]);
const RECONNECT_CAP_MS = 8000;

const el = {
  players: document.getElementById("players"),
  chipX: document.getElementById("chip-x"),
  chipO: document.getElementById("chip-o"),
  whoX: document.getElementById("who-x"),
  whoO: document.getElementById("who-o"),
  watchers: document.getElementById("watchers"),
  board: document.getElementById("board"),
  status: document.getElementById("status"),
  flash: document.getElementById("flash"),
  banner: document.getElementById("banner"),
  invite: document.getElementById("invite"),
  inviteUrl: document.getElementById("invite-url"),
  copy: document.getElementById("copy"),
};

let you = null;        // { role, mark }
let game = null;       // last authoritative state
let presence = { players_online: [], spectators: 0 };
let version = -1;      // invariant I7: never render an older state
let pending = null;    // cell we have sent but the server has not confirmed
let socket = null;
let attempt = 0;
let live = false;
let stopped = false;   // fatal error: do not reconnect
let flashTimer = null;

const cells = [];
const POSITIONS = ["top left", "top centre", "top right",
                   "middle left", "centre", "middle right",
                   "bottom left", "bottom centre", "bottom right"];

// --- board ---------------------------------------------------------------

for (let i = 0; i < 9; i++) {
  const cell = document.createElement("button");
  cell.type = "button";
  cell.className = "cell";
  cell.addEventListener("click", () => play(i));
  cells.push(cell);
  el.board.append(cell);
}

function myTurn() {
  return (
    live &&
    game !== null &&
    you?.role === "player" &&
    game.status === "in_progress" &&
    game.turn === you.mark &&
    pending === null
  );
}

function play(cell) {
  // A hint check, mirrored authoritatively on the server. Its only job is to
  // avoid sending traffic we already know is pointless.
  if (!myTurn() || game.board[cell] !== ".") return;
  pending = cell;
  hideFlash();
  send({ type: "move", cell });
  render();
}

// --- rendering -----------------------------------------------------------

function opponentMark() {
  return you?.mark === "X" ? "O" : "X";
}

function statusText() {
  if (!game) return "Connecting…";

  if (game.status === "waiting") {
    return you.role === "player"
      ? "Waiting for your opponent"
      : "Waiting for players";
  }

  if (game.status === "finished") {
    if (game.result === "draw") return "Draw";
    const winner = game.result === "x_won" ? "X" : "O";
    if (you.role !== "player") return `${winner} won`;
    return winner === you.mark ? "You won" : `${winner} won`;
  }

  if (you.role !== "player") return `${game.turn} to play`;
  if (pending !== null) return "Sending…";
  if (game.turn === you.mark) return "Your turn";

  const other = opponentMark();
  return presence.players_online.includes(other)
    ? `Waiting for ${other}`
    : `${other} is offline`;
}

function statusTone() {
  if (!game || game.status !== "finished") return "";
  if (game.result === "draw" || you.role !== "player") return "done";
  const winner = game.result === "x_won" ? "X" : "O";
  return winner === you.mark ? "done win" : "done lose";
}

function renderChip(chip, who, mark) {
  // While our own socket is down we do not know who is connected. Showing the
  // last known roster would be a confident-looking lie, so claim nothing.
  const online = live && presence.players_online.includes(mark);
  chip.classList.toggle("online", online);
  chip.classList.toggle(
    "turn",
    game?.status === "in_progress" && game.turn === mark
  );

  if (you?.mark === mark) who.textContent = "you";
  else if (!live) who.textContent = "";
  else who.textContent = online ? "" : "away";
}

function render() {
  if (!game) return;

  el.players.hidden = false;
  renderChip(el.chipX, el.whoX, "X");
  renderChip(el.chipO, el.whoO, "O");

  const watching = presence.spectators;
  el.watchers.textContent = watching
    ? `${watching} ${watching === 1 ? "person is" : "people are"} watching`
    : "";

  const winning = game.winning_line || [];
  cells.forEach((cell, i) => {
    const mark = game.board[i];
    const isPending = pending === i;

    if (mark === ".") {
      if (isPending) {
        cell.textContent = you.mark;
        cell.classList.add("pending");
        cell.dataset.mark = you.mark;
      } else {
        cell.textContent = "";
        cell.classList.remove("pending");
        delete cell.dataset.mark;
      }
    } else {
      cell.textContent = mark;
      cell.dataset.mark = mark;
      cell.classList.remove("pending");
    }

    cell.classList.toggle("win", winning.includes(i));
    cell.disabled = !myTurn() || game.board[i] !== ".";
    cell.setAttribute(
      "aria-label",
      `${POSITIONS[i]}, ${mark === "." ? "empty" : mark}`
    );
  });

  el.board.classList.toggle("playable", myTurn());

  el.status.textContent = statusText();
  el.status.className = `status ${statusTone()}`.trim();

  const inviting = you.role === "player" && game.status === "waiting";
  el.invite.hidden = !inviting;
  if (inviting && !el.inviteUrl.value) el.inviteUrl.value = window.location.href;
}

function flash(message) {
  el.flash.textContent = message;
  el.flash.hidden = false;
  clearTimeout(flashTimer);
  flashTimer = setTimeout(hideFlash, 3200);
}

function hideFlash() {
  clearTimeout(flashTimer);
  el.flash.hidden = true;
}

function banner(message) {
  if (message) {
    el.banner.textContent = message;
    el.banner.hidden = false;
  } else {
    el.banner.hidden = true;
  }
}

// --- socket --------------------------------------------------------------

function socketUrl() {
  const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${scheme}//${window.location.host}/ws/${GAME_ID}`;
}

function send(message) {
  if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify(message));
}

function handle(message) {
  switch (message.type) {
    case "snapshot":
      // Always applied: a fresh socket is authoritative and carries our role.
      you = message.you;
      game = message.game;
      presence = message.presence;
      version = message.game.version;
      pending = null;
      render();
      break;

    case "state":
      // I7: a state older than what we already have is dropped, not rendered.
      if (message.game.version <= version) return;
      version = message.game.version;
      game = message.game;
      presence = message.presence;
      pending = null;
      render();
      break;

    case "presence":
      presence = message.presence;
      render();
      break;

    case "error":
      pending = null;
      if (FATAL.has(message.code)) {
        stopped = true;
        banner(message.message);
      } else {
        flash(message.message);
      }
      render();
      break;
  }
}

function connect() {
  socket = new WebSocket(socketUrl());

  socket.addEventListener("open", () => {
    live = true;
    attempt = 0;
    banner(null);
    render();
  });

  socket.addEventListener("message", (event) => {
    let message;
    try {
      message = JSON.parse(event.data);
    } catch {
      return;
    }
    handle(message);
  });

  socket.addEventListener("close", () => {
    live = false;
    pending = null;
    render();
    if (stopped) return;

    // Exponential backoff with jitter, so a server restart does not get a
    // thundering herd from every open tab at once.
    const delay = Math.min(500 * 2 ** attempt, RECONNECT_CAP_MS);
    attempt += 1;
    banner("Connection lost — reconnecting…");
    setTimeout(connect, delay + Math.random() * 250);
  });
}

// Reconnect immediately when the tab wakes up or the network returns, instead
// of waiting out a backoff that started while nobody was looking.
function nudge() {
  if (stopped || live) return;
  if (document.visibilityState === "hidden") return;
  attempt = 0;
  connect();
}

document.addEventListener("visibilitychange", nudge);
window.addEventListener("online", nudge);

el.copy.addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(window.location.href);
    el.copy.textContent = "Copied";
    setTimeout(() => (el.copy.textContent = "Copy"), 1600);
  } catch {
    el.inviteUrl.select();
  }
});

connect();
