// Phase 0 placeholder: POST /api/games lands in phase 3.
const button = document.getElementById("new-game");
const error = document.getElementById("error");

button.addEventListener("click", async () => {
  button.disabled = true;
  try {
    const res = await fetch("/api/games", { method: "POST" });
    if (!res.ok) throw new Error(`server said ${res.status}`);
    const game = await res.json();
    window.location.href = `/g/${game.id}`;
  } catch (err) {
    error.textContent = `Could not create a game: ${err.message}`;
    error.hidden = false;
    button.disabled = false;
  }
});
