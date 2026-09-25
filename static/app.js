"use strict";

(() => {
  // ------------------------------------------------------------------ utilidades
  const $ = (sel, root = document) => root.querySelector(sel);
  const esc = (s) =>
    String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const reduceMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const clamp = (n, lo, hi) => Math.max(lo, Math.min(hi, n));
  const COP_PER_CHIP = 1000;
  const cop = (chips) => new Intl.NumberFormat("es-CO", { style: "currency", currency: "COP", maximumFractionDigits: 0 }).format(Number(chips || 0) * COP_PER_CHIP);
  const app = $("#app");

  const store = {
    get() { try { return JSON.parse(localStorage.getItem("guayabita.session") || "null"); } catch { return null; } },
    set(v) { try { localStorage.setItem("guayabita.session", JSON.stringify(v)); } catch { /* modo privado */ } },
    clear() { try { localStorage.removeItem("guayabita.session"); } catch { /* nada */ } },
  };

  let session = store.get(); // { code, token }
  let state = null;
  let busy = false;
  let tab = "online";
  let betAmount = 1;
  let betKey = "";
  let lastPot = null;
  let pollTimer = null;
  let toastTimer = null;
  let wallet = Number(localStorage.getItem("guayabita.wallet") || 100000);

  function toast(msg) {
    const t = $("#toast");
    t.textContent = msg;
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => (t.hidden = true), 4200);
  }

  async function api(path, { method = "GET", body } = {}) {
    let res;
    try {
      res = await fetch(path, {
        method,
        headers: body ? { "Content-Type": "application/json" } : undefined,
        body: body ? JSON.stringify(body) : undefined,
      });
    } catch {
      throw new Error("No hay conexión con el servidor. Revisa tu internet.");
    }
    let data = null;
    try { data = await res.json(); } catch { /* sin cuerpo */ }
    if (!res.ok) {
      const detail = data && data.detail;
      const msg = typeof detail === "string" ? detail : Array.isArray(detail) ? "Revisa los datos ingresados." : `Error ${res.status}`;
      const err = new Error(msg);
      err.status = res.status;
      throw err;
    }
    return data;
  }

  // ------------------------------------------------------------------ dados
  const PIPS = { 1: [4], 2: [0, 8], 3: [0, 4, 8], 4: [0, 2, 6, 8], 5: [0, 2, 4, 6, 8], 6: [0, 2, 3, 5, 6, 8] };

  function faceHTML(value) {
    const pips = PIPS[value] || [];
    return `<div class="cube-face"><span class="face-pips">${Array.from({ length: 9 }, (_, i) => `<i class="${pips.includes(i) ? "on" : ""}"></i>`).join("")}</span></div>`;
  }

  function dieHTML(id, value, caption) {
    const label = value ? `Dado en ${value}` : "Dado sin lanzar";
    return `<div class="die-wrap"><div class="die-stage"><div class="die ${value ? "" : "blank"}" id="${id}" data-value="${value || 0}" role="img" aria-label="${label}">${[1, 2, 3, 4, 5, 6].map(faceHTML).join("")}</div></div><span class="cap">${caption}</span></div>`;
  }

  function setDie(el, value) {
    el.dataset.value = value || 0;
    el.setAttribute("aria-label", value ? `Dado en ${value}` : "Dado sin lanzar");
    [...el.children].forEach((face, faceIndex) => {
      const pips = PIPS[((value + faceIndex) % 6) + 1] || [];
      [...face.querySelectorAll("i")].forEach((dot, i) => dot.classList.toggle("on", pips.includes(i)));
    });
  }

  function startRolling(el) {
    if (!el) return () => {};
    el.classList.remove("blank");
    el.classList.add("rolling");
    const timer = setInterval(() => setDie(el, 1 + Math.floor(Math.random() * 6)), 90);
    return () => { clearInterval(timer); el.classList.remove("rolling"); };
  }

  // ------------------------------------------------------------------ sesión y navegación
  function enterGame(code, token) {
    session = { code, token };
    store.set(session);
    state = null;
    lastPot = null;
    betKey = "";
    history.replaceState(null, "", `/?code=${code}`);
    return refresh(true);
  }

  function leave() {
    stopPolling();
    store.clear();
    session = null;
    state = null;
    lastPot = null;
    history.replaceState(null, "", "/");
    renderHome();
  }

  async function refresh(force = false) {
    if (!session) return;
    const q = session.token ? `?token=${encodeURIComponent(session.token)}` : "";
    const next = await api(`/api/games/${session.code}${q}`);
    const changed = force || JSON.stringify(next) !== JSON.stringify(state);
    state = next;
    if (changed) render();
    startPolling();
  }

  function startPolling() {
    if (pollTimer) return;
    pollTimer = setInterval(async () => {
      if (busy || document.hidden || !session) return;
      try { await refresh(); } catch (e) { if (e.status === 404) { toast("La partida ya no existe."); leave(); } }
    }, 2500);
  }
  function stopPolling() { clearInterval(pollTimer); pollTimer = null; }

  function render() {
    if (!state) return renderHome();
    if (state.status === "lobby") return renderLobby();
    return renderTable();
  }

  // ------------------------------------------------------------------ pantalla: inicio
  function renderHome(prefillCode = "") {
    stopPolling();
    app.innerHTML = `
      <main class="home">
        <header>
          <h1 class="wordmark">La Guayabita</h1>
          <p class="tag">Un dado, unas fichas y una mesa con amigos.</p>
        </header>

        <section class="panel">
          <div class="tabs" role="tablist" style="margin-top:0">
            <button class="tab" role="tab" data-action="tab" data-tab="online" aria-selected="${tab === "online"}">Cada uno en su celular</button>
            <button class="tab" role="tab" data-action="tab" data-tab="local" aria-selected="${tab === "local"}">Un solo dispositivo</button>
          </div>

          <form id="form-online" ${tab !== "online" ? "hidden" : ""}>
            <div class="field"><label for="on-name">Tu nombre</label>
              <input id="on-name" name="host" type="text" maxlength="20" required autocomplete="nickname"></div>
            <div class="row">
              <div class="field"><label for="on-ante">Apuesta inicial (COP)</label>
                <input id="on-ante" name="ante" type="number" min="1000" step="1000" value="5000" required></div>
              <div class="field"><label for="on-chips">Saldo por jugador (COP)</label>
                <input id="on-chips" name="chips" type="number" min="2000" step="1000" value="50000" required></div>
            </div>
            <button class="btn big" type="submit">Crear mesa</button>
            <p class="hint" style="margin-top:12px">Recibirás un código para que tus amigos entren desde su celular.</p>
          </form>

          <form id="form-local" ${tab !== "local" ? "hidden" : ""}>
            <div class="field"><span class="lbl">Jugadores (de 2 a 8)</span>
              <div class="names" id="names">
                <input type="text" name="n" maxlength="20" placeholder="Jugador 1" aria-label="Nombre del jugador 1">
                <input type="text" name="n" maxlength="20" placeholder="Jugador 2" aria-label="Nombre del jugador 2">
              </div>
              <button class="btn ghost small" type="button" data-action="add-name" style="justify-self:start">Agregar jugador</button>
            </div>
            <div class="row">
              <div class="field"><label for="lo-ante">Apuesta inicial (COP)</label>
                <input id="lo-ante" name="ante" type="number" min="1000" step="1000" value="5000" required></div>
              <div class="field"><label for="lo-chips">Saldo por jugador (COP)</label>
                <input id="lo-chips" name="chips" type="number" min="2000" step="1000" value="50000" required></div>
            </div>
            <button class="btn big" type="submit">Empezar partida</button>
            <p class="hint" style="margin-top:12px">Todos juegan en esta pantalla y se pasan el turno.</p>
          </form>
        </section>

        <section class="panel">
          <h2>¿Ya tienes un código?</h2>
          <form id="form-join">
            <div class="row">
              <div class="field"><label for="jn-code">Código de la mesa</label>
                <input id="jn-code" name="code" type="text" maxlength="5" value="${esc(prefillCode)}" autocapitalize="characters" autocomplete="off" required></div>
              <div class="field"><label for="jn-name">Tu nombre</label>
                <input id="jn-name" name="name" type="text" maxlength="20" autocomplete="nickname"></div>
            </div>
            <button class="btn" type="submit">Entrar a la mesa</button>
          </form>
        </section>

        <details class="rules">
          <summary>Cómo se juega</summary>
          <ul>
            <li>Cada jugador pone la apuesta inicial en el pozo y arranca con sus fichas restantes.</li>
            <li>En tu turno lanzas el dado. Si sale 1, pones una ficha en el pozo y pierdes el turno. Si sale 6, sacas una ficha del pozo y pierdes el turno.</li>
            <li>Si sale 2, 3, 4 o 5, decides cuánto apuestas o pasas. Puedes apostar hasta lo que haya en el pozo, sin superar tus fichas.</li>
            <li>Tras apostar lanzas de nuevo: necesitas un número mayor al primero. Si lo logras, te llevas lo apostado del pozo. Si sale igual o menor, lo apostado va al pozo.</li>
            <li>La partida termina cuando el pozo queda vacío o solo queda un jugador con fichas. Gana quien tenga más fichas.</li>
          </ul>
        </details>
      </main>`;
  }

  // ------------------------------------------------------------------ pantalla: sala de espera
  function topbar(showCode = true) {
    return `<div class="topbar">
      <h1 class="wordmark">La Guayabita</h1>
      <div class="right">
        ${showCode && state ? `<span class="code" title="Código de la mesa">${esc(state.code)}</span>` : ""}
        <button class="btn ghost small" data-action="leave">Salir</button>
      </div>
    </div>`;
  }

  function renderLobby() {
    const s = state;
    const isHost = !!(s.you && s.you.is_host);
    const host = s.players.find((p) => p.is_host);
    const list = s.players
      .map((p) => `<li><span>${esc(p.name)}${s.you && s.you.seat === p.seat ? " (tú)" : ""}</span><span class="muted">${p.is_host ? "Anfitrión" : "Jugador"}</span></li>`)
      .join("");
    app.innerHTML = `
      ${topbar(false)}
      <section class="panel lobby">
        <h2>Invita a tus amigos</h2>
        <p class="muted" style="margin:0">Que entren con este código o comparte el enlace.</p>
        <div class="bigcode">${esc(s.code)}</div>
        <button class="btn ghost small" data-action="copy-link">Copiar enlace</button>
        <ul class="plist" aria-label="Jugadores en la mesa">${list}</ul>
        <p class="hint">Apuesta inicial de ${cop(s.ante)}. Cada jugador empieza con ${cop(s.initial_chips - s.ante)}.</p>
        ${isHost
          ? `<button class="btn big" data-action="start" data-primary ${s.players.length < 2 ? "disabled" : ""}>Empezar partida</button>
             ${s.players.length < 2 ? `<p class="hint">Se necesitan al menos 2 jugadores.</p>` : ""}`
          : `<p>Esperando a que ${esc(host ? host.name : "el anfitrión")} empiece la partida…</p>`}
      </section>`;
  }

  // ------------------------------------------------------------------ pantalla: mesa
  function renderTable() {
    const s = state;
    const online = s.mode === "online";
    const finished = s.status === "finished";
    const cur = s.players.find((p) => p.seat === s.current_seat) || null;
    const mine = online && s.you && cur && s.you.seat === cur.seat;
    const mePlayer = s.you ? s.players.find((p) => p.seat === s.you.seat) : null;
    const last = s.moves[0] || null;

    // Reinicia la apuesta sugerida al cambiar de turno/fase
    const key = `${s.turn_no}:${s.phase}`;
    if (key !== betKey) {
      betKey = key;
      betAmount = clamp(Math.min(s.ante, s.max_bet), 1, Math.max(1, s.max_bet));
    }

    // Dados a mostrar: el primer tiro en curso o el último turno jugado
    let a = null, b = null;
    if (s.phase === "bet") a = s.first_roll;
    else if (last) { a = last.first_roll; b = last.second_roll; }

    // Mensaje central
    let banner = "";
    if (s.phase === "bet" && cur) {
      const need = a >= 5 ? "6" : `${a + 1} o más`;
      banner = mine ? `Sacaste ${a}. Para ganar necesitas ${need}.` : `${cur.name} sacó ${a}. Para ganar necesita ${need}.`;
    } else if (last) banner = last.message;
    else if (cur) banner = `Empieza la partida. El primer turno es de ${cur.name}.`;

    const bump = lastPot !== null && lastPot !== s.pot;
    lastPot = s.pot;

    const seats = s.players.map((p) => {
      const cls = ["seat", p.seat === s.current_seat && !finished ? "current" : "", p.out ? "out" : "", finished && p.seat === s.winner_seat ? "winner" : ""].join(" ");
      const you = online && s.you && s.you.seat === p.seat ? `<span class="tu">(tú)</span>` : "";
      const refill = p.out && !finished
        ? `<button class="btn ghost small refill-seat" data-action="recharge-player" data-seat="${p.seat}">Recargar</button>`
        : "";
      return `<li class="${cls}" ${p.seat === s.current_seat && !finished ? 'aria-current="true"' : ""}><span class="n">${esc(p.name)}</span>${you}<span class="c">${cop(p.chips)}</span>${refill}</li>`;
    }).join("");

    const log = s.moves.length
      ? `<ol>${s.moves.map((m) => `<li>${esc(m.message)}<div class="meta">Turno ${m.turn_no}, pozo ${m.pot_after}</div></li>`).join("")}</ol>`
      : `<p class="empty">Aún no hay jugadas. Aquí quedará el registro de cada turno.</p>`;

    app.innerHTML = `
      ${topbar()}
      <div class="layout">
        <section class="panel game-panel">
          <div class="wallet-bar"><span><small>Dinero disponible para recargar</small><strong>${cop(wallet / COP_PER_CHIP)}</strong></span><span><small>Saldo en mesa</small><strong>${mePlayer ? cop(mePlayer.chips) : "—"}</strong></span><button class="btn ghost small" data-action="recharge">+ Recargar saldo</button></div>
          <div class="poker-table">
            <ul class="seats" aria-label="Jugadores y fichas">${seats}</ul>
            <div class="felt"><div class="felt-inner">
              <div class="coin ${bump ? "bump" : ""}" role="img" aria-label="Pozo de ${cop(s.pot)}"><span class="num">${cop(s.pot)}</span><span class="lbl">Pozo</span></div>
              <div class="dice">${dieHTML("die-a", a, "Primer tiro")}${dieHTML("die-b", b, "Segundo tiro")}</div>
              <p class="banner" id="banner">${esc(banner)}</p>
              ${finished ? finishedHTML(s) : controlsHTML(s, cur, mine, online, mePlayer)}
            </div></div>
          </div>
        </section>
        <aside class="log" aria-label="Historial de jugadas"><h3>Jugadas recientes</h3>${log}</aside>
      </div>`;
  }

  function controlsHTML(s, cur, mine, online, mePlayer) {
    if (!cur) return "";
    if (mePlayer && mePlayer.out && online) {
      return `<div class="controls out-notice"><p class="turnline">Te quedaste sin saldo en la mesa.</p><p class="hint">Puedes recargar para volver a jugar o salir. Los demás jugadores continúan.</p><button class="btn big" data-action="recharge" data-primary>Recargar saldo</button></div>`;
    }
    const who = online ? (mine ? "Es tu turno" : `Turno de <b>${esc(cur.name)}</b>`) : `Turno de <b>${esc(cur.name)}</b>`;
    if (!s.can_act) return `<div class="controls"><p class="turnline">${who}</p><p class="hint">Esperando a que ${esc(cur.name)} juegue…</p></div>`;

    if (s.phase === "first_roll") {
      return `<div class="controls"><p class="turnline">${who}</p>
        <button class="btn big" data-action="roll" data-primary>Lanzar el dado</button>
        <p class="hint">1 pone una ficha, 6 saca una, y del 2 al 5 puedes apostar.</p></div>`;
    }
    const max = s.max_bet;
    if (max <= 0) {
      return `<div class="controls"><p class="turnline">${who}</p><p class="hint">El pozo está vacío. Puedes pasar; la mesa sigue activa.</p><button class="btn big" data-action="pass" data-primary>Pasar turno</button></div>`;
    }
    return `<div class="controls"><p class="turnline">${who}</p>
      <div class="betbox">
        <div class="stepper">
          <button type="button" data-action="bet-dec" aria-label="Apostar una ficha menos">−</button>
          <input id="bet-input" type="number" min="1" max="${max}" value="${betAmount}" inputmode="numeric" aria-label="Fichas a apostar">
          <button type="button" data-action="bet-inc" aria-label="Apostar una ficha más">+</button>
        </div>
        <div class="quick">
          <button type="button" data-action="bet-set" data-v="1">1</button>
          <button type="button" data-action="bet-set" data-v="half">Mitad</button>
          <button type="button" data-action="bet-set" data-v="max">Todo (${cop(max)})</button>
        </div>
        <div class="actions">
          <button class="btn big" id="bet-go" data-action="bet-go" data-primary>Apostar ${cop(betAmount)} y lanzar</button>
          <button class="btn ghost" data-action="pass">Pasar</button>
        </div>
        <p class="hint">Máximo ${cop(max)}: lo que hay en el pozo o tus fichas, lo que sea menor.</p>
      </div></div>`;
  }

  function finishedHTML(s) {
    const w = s.players.find((p) => p.seat === s.winner_seat);
    const why = s.finish_reason === "todos_sin_saldo" ? "Todos los jugadores se quedaron sin saldo." : "La mesa terminó.";
    return `<div class="winnerbox">
      <p class="big">Ganó ${esc(w ? w.name : "nadie")}</p>
      <p class="muted" style="margin:0">${why} Termina con ${w ? cop(w.chips) : cop(0)}.</p>
      <div class="actions" style="margin-top:12px">
        <button class="btn" data-action="restart-round" data-primary>Volver a apostar</button>
        <button class="btn ghost" data-action="leave">Volver al inicio</button>
      </div></div>`;
  }

  // ------------------------------------------------------------------ acciones de juego
  function updateBetUI() {
    const input = $("#bet-input");
    const go = $("#bet-go");
    if (input) input.value = betAmount;
    if (go) go.textContent = `Apostar ${cop(betAmount)} y lanzar`;
  }

  async function act(dieId, call) {
    if (busy) return;
    busy = true;
    $$buttons(true);
    const stop = startRolling(dieId ? $(dieId) : null);
    try {
      const [res] = await Promise.all([call(), sleep(reduceMotion ? 0 : 750)]);
      state = res.state;
    } catch (e) {
      toast(e.message);
      if (e.status === 409 || e.status === 403) { try { state = await api(`/api/games/${session.code}${session.token ? `?token=${encodeURIComponent(session.token)}` : ""}`); } catch { /* se reintenta en el sondeo */ } }
    } finally {
      stop();
      busy = false;
      render();
      const primary = $("[data-primary]");
      if (primary) primary.focus({ preventScroll: true });
    }
  }
  function $$buttons(disabled) { document.querySelectorAll("#app button").forEach((b) => (b.disabled = disabled)); }

  const bodyBase = () => ({ token: session.token, version: state.version });

  const doRoll = () => act("#die-a", () => api(`/api/games/${session.code}/roll`, { method: "POST", body: bodyBase() }));
  const doBet = (amount) => act(amount > 0 ? "#die-b" : null, () => api(`/api/games/${session.code}/bet`, { method: "POST", body: { ...bodyBase(), amount } }));

  // ------------------------------------------------------------------ eventos
  document.addEventListener("click", async (e) => {
    const el = e.target.closest("[data-action]");
    if (!el) return;
    const action = el.dataset.action;
    try {
      switch (action) {
        case "tab": {
          tab = el.dataset.tab;
          document.querySelectorAll(".tab").forEach((t) => t.setAttribute("aria-selected", String(t.dataset.tab === tab)));
          $("#form-online").hidden = tab !== "online";
          $("#form-local").hidden = tab !== "local";
          break;
        }
        case "add-name": {
          const box = $("#names");
          if (box.children.length >= 8) return toast("Máximo 8 jugadores.");
          const n = box.children.length + 1;
          box.insertAdjacentHTML("beforeend", `<input type="text" name="n" maxlength="20" placeholder="Jugador ${n}" aria-label="Nombre del jugador ${n}">`);
          box.lastElementChild.focus();
          break;
        }
        case "roll": return doRoll();
        case "bet-dec": betAmount = clamp(betAmount - 1, 1, Math.max(1, state.max_bet)); return updateBetUI();
        case "bet-inc": betAmount = clamp(betAmount + 1, 1, Math.max(1, state.max_bet)); return updateBetUI();
        case "bet-set": {
          const v = el.dataset.v;
          const max = Math.max(1, state.max_bet);
          betAmount = v === "max" ? max : v === "half" ? clamp(Math.floor(max / 2), 1, max) : 1;
          return updateBetUI();
        }
        case "bet-go": return doBet(betAmount);
        case "pass": return doBet(0);
        case "start": {
          const res = await api(`/api/games/${session.code}/start`, { method: "POST", body: { token: session.token } });
          state = res; return render();
        }
        case "copy-link": {
          const link = `${location.origin}/?code=${state.code}`;
          try { await navigator.clipboard.writeText(link); toast("Enlace copiado."); } catch { window.prompt("Copia este enlace:", link); }
          break;
        }
        case "recharge":
        case "recharge-player": {
          const target = action === "recharge-player" ? Number(el.dataset.seat) : (state.you ? state.you.seat : null);
          const player = state.players.find((p) => p.seat === target);
          if (!player || !player.out) return toast("La recarga solo está disponible cuando el jugador se queda sin saldo.");
          const amount = Number(window.prompt("¿Cuánto deseas recargar en COP?", "50000"));
          const copAmount = Math.floor(amount / COP_PER_CHIP) * COP_PER_CHIP;
          if (!Number.isFinite(amount) || copAmount < COP_PER_CHIP) return toast("La recarga mínima es de $1.000 COP.");
          if (wallet < copAmount) return toast("No tienes suficiente dinero disponible para esa recarga.");
          const recharged = await api(`/api/games/${session.code}/recharge`, {
            method: "POST",
            body: { token: session.token, seat: target, amount: copAmount / COP_PER_CHIP },
          });
          wallet -= copAmount;
          localStorage.setItem("guayabita.wallet", String(wallet));
          state = recharged;
          toast(`${player.name} recibió ${cop(copAmount / COP_PER_CHIP)} y puede continuar jugando.`);
          return render();
        }
        case "leave":
          if (state && state.status !== "finished" && !confirm("¿Salir de la mesa? Podrás volver con el código.")) return;
          return leave();
        case "restart-round": {
          const d = await api(`/api/games/${session.code}/restart`, {
            method: "POST",
            body: { token: session.token },
          });
          state = d;
          toast("Nueva ronda iniciada. ¡Vuelvan a apostar!");
          return render();
        }
      }
    } catch (err) {
      toast(err.message);
    }
  });

  document.addEventListener("input", (e) => {
    if (e.target.id === "bet-input" && state) {
      const max = Math.max(1, state.max_bet);
      const v = parseInt(e.target.value, 10);
      betAmount = clamp(Number.isFinite(v) ? v : 1, 1, max);
      const go = $("#bet-go");
      if (go) go.textContent = `Apostar ${cop(betAmount)} y lanzar`;
    }
    if (e.target.id === "jn-code") e.target.value = e.target.value.toUpperCase();
  });

  document.addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    const btn = f.querySelector("[type=submit]");
    if (btn) btn.disabled = true;
    try {
      if (f.id === "form-online") {
        const d = await api("/api/games", {
          method: "POST",
          body: { mode: "online", host_name: f.elements.host.value, ante: Math.floor(+f.elements.ante.value / COP_PER_CHIP), initial_chips: Math.floor(+f.elements.chips.value / COP_PER_CHIP) },
        });
        await enterGame(d.code, d.token);
      } else if (f.id === "form-local") {
        const names = [...f.querySelectorAll("input[name=n]")].map((i) => i.value.trim()).filter(Boolean);
        const d = await api("/api/games", {
          method: "POST",
          body: { mode: "local", names, ante: Math.floor(+f.elements.ante.value / COP_PER_CHIP), initial_chips: Math.floor(+f.elements.chips.value / COP_PER_CHIP) },
        });
        await enterGame(d.code, d.token);
      } else if (f.id === "form-join") {
        const code = f.elements.code.value.trim().toUpperCase();
        const info = await api(`/api/games/${encodeURIComponent(code)}`);
        if (info.mode === "local") {
          await enterGame(info.code, null);
        } else {
          const nm = f.elements.name.value.trim();
          if (!nm) throw new Error("Escribe tu nombre para entrar.");
          const d = await api(`/api/games/${info.code}/join`, { method: "POST", body: { name: nm } });
          await enterGame(d.code, d.token);
        }
      }
    } catch (err) {
      toast(err.message);
      if (btn) btn.disabled = false;
    }
  });

  // ------------------------------------------------------------------ arranque
  (async function boot() {
    const urlCode = (new URLSearchParams(location.search).get("code") || "").toUpperCase();
    if (session && (!urlCode || urlCode === session.code)) {
      try { await refresh(true); return; } catch { store.clear(); session = null; }
    }
    renderHome(urlCode);
  })();
})();
