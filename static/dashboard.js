const $ = (id) => document.getElementById(id);
let nextPackSeconds = 0;
let pollTimer = null;

async function api(url, options = {}) {
  const res = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || "Request failed");
  return data;
}

function cardHTML(c, withButton) {
  return `
    <div class="chess-card" style="--c:${c.color}">
      <div class="card-tier">Tier ${c.tier}</div>
      <div class="card-name">${c.name}</div>
      <div class="card-range">${c.label}</div>
      <div class="card-elo">Opponent ${c.opponent}</div>
      <div class="card-rarity">${c.rarity}</div>
      ${withButton ? `<button class="btn small primary" data-card="${c.id}">Play this card</button>` : ""}
    </div>`;
}

function renderTimer() {
  const h = Math.floor(nextPackSeconds / 3600);
  const m = Math.floor((nextPackSeconds % 3600) / 60);
  const s = nextPackSeconds % 60;
  $("pack-timer").textContent = `Next pack in ${h}h ${m}m ${s}s (resets at midnight UTC)`;
}

async function refresh() {
  const me = await api("/api/me");
  nextPackSeconds = me.next_pack_seconds;

  $("open-pack").disabled = !me.pack_available;
  $("open-pack").textContent = me.pack_available ? "Open pack" : "Pack opened today";
  $("pack-timer").classList.toggle("hidden", me.pack_available);
  renderTimer();

  if (me.active_game) {
    $("active-game-banner").classList.remove("hidden");
    $("active-game-link").href = `/game/${me.active_game}`;
  }

  const owned = me.cards.filter((c) => c.status === "owned");
  const canPlay = !me.queue && !me.active_game;
  $("my-cards").innerHTML = owned.map((c) => cardHTML(c, canPlay)).join("");
  $("no-cards").classList.toggle("hidden", owned.length > 0 || !!me.queue);

  if (me.queue) {
    $("queue-panel").classList.remove("hidden");
    const c = me.queue.card;
    $("queue-info").textContent = `${c.name} card · looking for ${c.opponent}`;
    startPolling();
  } else {
    $("queue-panel").classList.add("hidden");
  }
}

function startPolling() {
  if (pollTimer) return;
  pollTimer = setInterval(async () => {
    try {
      const s = await api("/api/queue/status");
      if (s.game_id) { window.location = `/game/${s.game_id}`; return; }
      if (!s.queued) { stopPolling(); refresh(); return; }
      const c = $("queue-info").textContent.split(" · waiting")[0];
      $("queue-info").textContent = `${c} · waiting players: ${s.others_waiting}`;
    } catch (e) { /* ignore transient errors */ }
  }, 2000);
}

function stopPolling() { clearInterval(pollTimer); pollTimer = null; }

$("open-pack").addEventListener("click", async () => {
  $("open-pack").disabled = true;
  try {
    const { card } = await api("/api/pack/open", { method: "POST" });
    const pack = $("pack");
    pack.classList.add("shaking");
    setTimeout(() => {
      pack.classList.remove("shaking");
      pack.innerHTML = cardHTML(card, false);
      pack.classList.add("revealed");
      refresh();
    }, 900);
  } catch (e) {
    alert(e.message);
    refresh();
  }
});

$("my-cards").addEventListener("click", async (e) => {
  const id = e.target.dataset.card;
  if (!id) return;
  try {
    const r = await api("/api/queue/join", { method: "POST", body: JSON.stringify({ card_id: Number(id) }) });
    if (r.game_id) { window.location = `/game/${r.game_id}`; return; }
    refresh();
  } catch (err) { alert(err.message); }
});

$("play-bot").addEventListener("click", async () => {
  try {
    const r = await api("/api/queue/bot", { method: "POST" });
    window.location = `/game/${r.game_id}`;
  } catch (e) { alert(e.message); refresh(); }
});

$("leave-queue").addEventListener("click", async () => {
  stopPolling();
  await api("/api/queue/leave", { method: "POST" });
  refresh();
});

setInterval(() => { if (nextPackSeconds > 0) { nextPackSeconds--; renderTimer(); } }, 1000);
refresh();
