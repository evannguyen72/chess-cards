const $ = (id) => document.getElementById(id);
const GAME_ID = document.body.dataset.gameId;
const GLYPHS = { k: "♚", q: "♛", r: "♜", b: "♝", n: "♞", p: "♟" };
const FILES = "abcdefgh";
let state = null;
let selected = null;
let lastKey = "";

async function api(url, options = {}) {
  const res = await fetch(url, { headers: { "Content-Type": "application/json" }, ...options });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || "Request failed");
  return data;
}

function parseFen(fen) {
  const pieces = {};
  fen.split(" ")[0].split("/").forEach((row, r) => {
    let f = 0;
    for (const ch of row) {
      if (/\d/.test(ch)) { f += Number(ch); continue; }
      pieces[FILES[f] + (8 - r)] = { type: ch.toLowerCase(), color: ch === ch.toUpperCase() ? "w" : "b" };
      f++;
    }
  });
  return pieces;
}

function renderBoard() {
  const board = $("board");
  board.innerHTML = "";
  const pieces = parseFen(state.fen);
  const flip = state.my_color === "black";
  const ranks = flip ? [1, 2, 3, 4, 5, 6, 7, 8] : [8, 7, 6, 5, 4, 3, 2, 1];
  const files = flip ? [...FILES].reverse() : [...FILES];
  const targets = selected ? state.legal_moves.filter((m) => m.startsWith(selected)).map((m) => m.slice(2, 4)) : [];
  const last = state.last_move ? [state.last_move.slice(0, 2), state.last_move.slice(2, 4)] : [];
  const turnColor = state.turn === "white" ? "w" : "b";

  ranks.forEach((rank, ri) => {
    files.forEach((file, fi) => {
      const sq = file + rank;
      const el = document.createElement("div");
      el.className = "sq " + ((FILES.indexOf(file) + rank) % 2 === 0 ? "dark" : "light");
      el.dataset.sq = sq;
      if (last.includes(sq)) el.classList.add("last");
      if (sq === selected) el.classList.add("selected");
      if (targets.includes(sq)) el.classList.add(pieces[sq] ? "capture" : "target");
      const p = pieces[sq];
      if (p) {
        const span = document.createElement("span");
        span.className = "piece " + p.color;
        span.textContent = GLYPHS[p.type];
        el.appendChild(span);
        if (p.type === "k" && p.color === turnColor && state.in_check) el.classList.add("check");
      }
      if (fi === 0) el.insertAdjacentHTML("beforeend", `<span class="coord rank">${rank}</span>`);
      if (ri === 7) el.insertAdjacentHTML("beforeend", `<span class="coord file">${file}</span>`);
      board.appendChild(el);
    });
  });
}

function playerHTML(p, color) {
  let change = "";
  if (p.elo_change !== null) {
    change = ` <span class="${p.elo_change >= 0 ? "pos" : "neg"}">(${p.elo_change >= 0 ? "+" : ""}${p.elo_change})</span>`;
  }
  const turn = state.status === "active" && state.turn === color ? '<span class="turn-dot"></span>' : "";
  return `${turn}<span class="piece-icon ${color[0]}">♚</span>
    <strong>${p.username}</strong> <span class="muted">${p.elo_before}${change}</span>
    <span class="card-chip" style="--c:${p.card_color}">${p.card}</span>`;
}

function render() {
  const me = state.my_color;
  const opp = me === "white" ? "black" : "white";
  $("top-player").innerHTML = playerHTML(state[opp], opp);
  $("bottom-player").innerHTML = playerHTML(state[me], me);
  renderBoard();

  const moves = $("moves");
  moves.innerHTML = "";
  for (let i = 0; i < state.san_moves.length; i += 2) {
    const li = document.createElement("li");
    li.textContent = `${state.san_moves[i]}  ${state.san_moves[i + 1] || ""}`;
    moves.appendChild(li);
  }
  moves.scrollTop = moves.scrollHeight;

  const active = state.status === "active";
  if (active) {
    const mine = state.turn === me;
    $("status").textContent = (mine ? "Your move" : "Opponent's move") + (state.in_check ? " — check!" : "");
    $("status").className = "status " + (mine ? "yours" : "");
  } else {
    $("status").textContent = "Game over";
    $("status").className = "status";
    const myChange = state[me].elo_change;
    let headline = "Draw";
    if (state.result === "1-0") headline = me === "white" ? "You won!" : "You lost";
    if (state.result === "0-1") headline = me === "black" ? "You won!" : "You lost";
    $("game-over").innerHTML = `<h3>${headline}</h3><p>${state.result} · ${state.termination}</p>
      <p>Your Elo: ${state[me].elo} <span class="${myChange >= 0 ? "pos" : "neg"}">(${myChange >= 0 ? "+" : ""}${myChange})</span></p>
      <a class="btn primary" href="/dashboard">Back to dashboard</a>`;
    $("game-over").classList.remove("hidden");
    const navElo = $("nav-elo");
    if (navElo) navElo.textContent = state[me].elo;
  }

  $("offer-draw").disabled = !active || state.draw_offered_by === me;
  $("offer-draw").textContent = state.draw_offered_by === me ? "Draw offered" : "Offer draw";
  $("resign").disabled = !active;
  $("draw-offer").classList.toggle("hidden", !(active && state.draw_offered_by && state.draw_offered_by !== me));
}

function setState(s) {
  const key = [s.fen, s.status, s.draw_offered_by, s.legal_moves.length].join("|");
  state = s;
  if (key !== lastKey) { lastKey = key; render(); }
}

async function load() {
  try { setState(await api(`/api/game/${GAME_ID}`)); } catch (e) { $("error").textContent = e.message; }
}

async function send(path, body) {
  $("error").textContent = "";
  try { setState(await api(`/api/game/${GAME_ID}/${path}`, { method: "POST", body: JSON.stringify(body || {}) })); }
  catch (e) { $("error").textContent = e.message; load(); }
}

$("board").addEventListener("click", (e) => {
  const sqEl = e.target.closest(".sq");
  if (!sqEl || !state || state.status !== "active" || state.turn !== state.my_color) return;
  const sq = sqEl.dataset.sq;

  if (selected) {
    const candidates = state.legal_moves.filter((m) => m.startsWith(selected + sq));
    if (candidates.length) {
      let move = candidates[0];
      if (candidates.length > 1) move = selected + sq + $("promo").value; // promotion
      selected = null;
      send("move", { move });
      return;
    }
  }
  selected = state.legal_moves.some((m) => m.startsWith(sq)) && sq !== selected ? sq : null;
  renderBoard();
});

$("resign").addEventListener("click", () => { if (confirm("Resign this game?")) send("resign"); });
$("offer-draw").addEventListener("click", () => send("draw", { action: "offer" }));
document.querySelectorAll("[data-draw]").forEach((b) =>
  b.addEventListener("click", () => send("draw", { action: b.dataset.draw }))
);

load();
setInterval(() => { if (!state || state.status === "active") load(); }, 1500);
