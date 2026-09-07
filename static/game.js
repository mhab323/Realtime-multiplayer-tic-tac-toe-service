// Phase 3 placeholder. The realtime client lands in phase 5; for now this only
// proves the page can read its own game and the server's view of who we are.
const gameId = window.location.pathname.split("/").pop();
const roleEl = document.getElementById("role");
const statusEl = document.getElementById("status");

document.getElementById("copy-link").addEventListener("click", async () => {
  await navigator.clipboard.writeText(window.location.href);
});

async function load() {
  const res = await fetch(`/api/games/${gameId}`);
  if (!res.ok) {
    roleEl.textContent = "This game is gone.";
    return;
  }
  const { game, you } = await res.json();
  roleEl.textContent =
    you.role === "player" ? `You are ${you.mark}` : "You are watching";
  statusEl.textContent = `status: ${game.status} · turn: ${game.turn} · v${game.version}`;
}

load();
