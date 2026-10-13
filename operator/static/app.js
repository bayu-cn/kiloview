const MODES = [
  ["1080i50", "1080i50"],
  ["1080p60", "1080p60"],
];

const AUDIO = [
  ["1-2", "1-2 · stereo asli"],
  ["1-1", "1-1 · kanal 1 ke kedua sisi"],
  ["2-2", "2-2 · kanal 2 ke kedua sisi"],
];

const sourcesEl = document.querySelector("#sources");
const slotsEl = document.querySelector("#slots");
const userEl = document.querySelector("#user");
const logoutEl = document.querySelector("#logout");

let state = null;

function esc(value) {
  return String(value).replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "\"": "&quot;",
    "'": "&#39;",
  }[ch]));
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (res.status === 401) {
    location.href = "/login";
    return null;
  }
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || "Permintaan gagal");
  return body;
}

function badge(source) {
  if (source.routedTo.length) return ["onair", source.routedTo.join(" · ")];
  if (source.ready) return ["live", "masuk"];
  return ["", "tidak ada sinyal"];
}

function statusText(slot) {
  if (slot.status === "playing") return "Tayang";
  if (slot.status === "starting") return "Mengunci sinyal…";
  if (slot.status === "error") return "Gagal keluar ke SDI";
  return "Kosong";
}

function shell() {
  sourcesEl.innerHTML = state.sources.map((source) => `
    <article class="card" data-source="${esc(source.id)}">
      <div class="preview">
        <iframe src="${esc(source.preview)}" title="${esc(source.label)}" allow="autoplay"></iframe>
      </div>
      <div class="card-foot" draggable="true">
        <span class="name">${esc(source.label)}</span>
        <span class="badge" data-badge="${source.id}"></span>
      </div>
    </article>
  `).join("");

  slotsEl.innerHTML = Object.entries(state.slots).map(([id, slot]) => `
    <article class="slot" data-slot="${id}">
      <div class="slot-top">
        <h3>${slot.label}</h3>
        <span class="meta">DeckLink ${slot.device}</span>
      </div>
      <div class="drop" data-drop="${id}">
        <strong data-assigned="${id}"></strong>
        <span data-hint="${id}"></span>
      </div>
      <p class="status" data-status="${id}"></p>
      <div class="controls">
        <label>Mode video
          <select data-mode="${id}">
            ${MODES.map(([value, label]) => `<option value="${value}">${label}</option>`).join("")}
          </select>
        </label>
        <label>Audio
          <select data-audio="${id}">
            ${AUDIO.map(([value, label]) => `<option value="${value}">${label}</option>`).join("")}
          </select>
        </label>
        <button type="button" data-clear="${id}">Lepas</button>
      </div>
      <pre class="log" data-log="${id}" hidden></pre>
    </article>
  `).join("");

  sourcesEl.querySelectorAll(".card").forEach((card) => {
    const handle = card.querySelector(".card-foot");
    handle.addEventListener("dragstart", (event) => {
      event.dataTransfer.setData("text/plain", card.dataset.source);
      event.dataTransfer.effectAllowed = "copy";
      card.classList.add("dragging");
    });
    handle.addEventListener("dragend", () => card.classList.remove("dragging"));
  });

  slotsEl.querySelectorAll(".slot").forEach((slot) => {
    const id = slot.dataset.slot;
    slot.addEventListener("dragover", (event) => {
      event.preventDefault();
      slot.classList.add("over");
    });
    slot.addEventListener("dragleave", (event) => {
      if (!slot.contains(event.relatedTarget)) slot.classList.remove("over");
    });
    slot.addEventListener("drop", (event) => {
      event.preventDefault();
      slot.classList.remove("over");
      const source = event.dataTransfer.getData("text/plain");
      if (source) save(id, { source });
    });
    slot.querySelector("[data-mode]").addEventListener("change", (event) => {
      save(id, { mode: event.target.value });
    });
    slot.querySelector("[data-audio]").addEventListener("change", (event) => {
      save(id, { audio: event.target.value });
    });
    slot.querySelector("[data-clear]").addEventListener("click", () => {
      save(id, { source: null });
    });
  });
}

function paint() {
  const routed = {};
  Object.entries(state.slots).forEach(([id, slot]) => {
    if (!slot.source) return;
    routed[slot.source] = routed[slot.source] || [];
    routed[slot.source].push(slot.label);
  });

  state.sources.forEach((source) => {
    source.routedTo = routed[source.id] || [];
    const card = sourcesEl.querySelector(`[data-source="${source.id}"]`);
    card.classList.toggle("onair", source.routedTo.length > 0);
    const [kind, text] = badge(source);
    const badgeEl = card.querySelector("[data-badge]");
    badgeEl.className = `badge ${kind}`;
    badgeEl.textContent = text;
  });

  Object.entries(state.slots).forEach(([id, slot]) => {
    const box = slotsEl.querySelector(`[data-slot="${id}"]`);
    box.classList.toggle("playing", slot.status === "playing");
    const assigned = box.querySelector("[data-assigned]");
    const hint = box.querySelector("[data-hint]");
    assigned.textContent = slot.source || "Kosong";
    hint.textContent = slot.source ? "Seret sumber lain untuk mengganti" : "Seret sumber ke sini";
    const status = box.querySelector("[data-status]");
    status.className = `status ${slot.status}`;
    status.textContent = statusText(slot);
    const mode = box.querySelector("[data-mode]");
    const audio = box.querySelector("[data-audio]");
    if (document.activeElement !== mode) mode.value = slot.mode;
    if (document.activeElement !== audio) audio.value = slot.audio;
    const busy = slot.status === "starting";
    mode.disabled = busy;
    audio.disabled = busy;
    box.querySelector("[data-clear]").disabled = busy || !slot.source;
    const log = box.querySelector("[data-log]");
    log.hidden = slot.status !== "error" || !slot.error;
    log.textContent = slot.error || "";
  });
}

async function save(id, patch) {
  const slot = state.slots[id];
  const next = {
    source: Object.prototype.hasOwnProperty.call(patch, "source") ? patch.source : slot.source,
    mode: patch.mode || slot.mode,
    audio: patch.audio || slot.audio,
  };
  try {
    state = await api(`/api/slots/${id}`, { method: "POST", body: JSON.stringify(next) });
    if (state) paint();
  } catch (error) {
    window.alert(error.message);
  }
}

async function tick() {
  try {
    const next = await api("/api/state");
    if (!next) return;
    if (!state) {
      state = next;
      userEl.textContent = next.user ? `${next.user}` : "LAN tanpa login";
      logoutEl.hidden = !next.auth;
      shell();
    } else {
      state = next;
    }
    paint();
  } catch (error) {
    console.error(error);
  }
}

tick();
setInterval(tick, 2000);
