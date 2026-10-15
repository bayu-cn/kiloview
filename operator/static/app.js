const MODES = [
  ["1080i50", "1080i50"],
  ["1080p60", "1080p60"],
];

const AUDIO = [
  ["1-2", "1-2 · original stereo"],
  ["1-1", "1-1 · ch 1 to both"],
  ["2-2", "2-2 · ch 2 to both"],
];

const sourcesEl = document.querySelector("#sources");
const slotsEl = document.querySelector("#slots");
const userEl = document.querySelector("#user");
const logoutEl = document.querySelector("#logout");
const sourceForm = document.querySelector("#source-form");
const addSourceButton = document.querySelector("#add-source");

let state = null;
const players = new Map();

function savedVolume(id) {
  const saved = sessionStorage.getItem(`listen-${id}`);
  const level = saved == null ? 0.8 : Number(saved);
  return Number.isFinite(level) ? level : 0.8;
}

function waitIce(pc) {
  if (pc.iceGatheringState === "complete") return Promise.resolve();
  return new Promise((resolve) => {
    const done = () => {
      if (pc.iceGatheringState !== "complete") return;
      pc.removeEventListener("icegatheringstatechange", done);
      clearTimeout(timer);
      resolve();
    };
    const timer = setTimeout(() => {
      pc.removeEventListener("icegatheringstatechange", done);
      resolve();
    }, 1200);
    pc.addEventListener("icegatheringstatechange", done);
  });
}

function closePlayer(video) {
  const prev = players.get(video);
  if (!prev) return;
  prev.closed = true;
  if (prev.pc) prev.pc.close();
  players.delete(video);
  video.srcObject = null;
}

async function connectWhep(video, url, options) {
  const prev = players.get(video);
  if (prev) {
    prev.closed = true;
    if (prev.pc) prev.pc.close();
  }
  const pc = new RTCPeerConnection();
  const record = {
    url,
    pc,
    key: options.key,
    audio: options.audio,
    heard: !!options.heard,
    closed: false,
    movedAt: Date.now(),
    lastTime: 0,
    failedAt: 0,
  };
  players.set(video, record);
  pc.addTransceiver("video", { direction: "recvonly" });
  if (options.audio) pc.addTransceiver("audio", { direction: "recvonly" });
  const stream = new MediaStream();
  video.srcObject = stream;
  video.muted = !record.heard;
  video.volume = options.volume ?? 0;
  pc.ontrack = (event) => {
    stream.addTrack(event.track);
    video.play().catch(() => {});
  };
  try {
    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    await waitIce(pc);
    if (record.closed) return;
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/sdp" },
      body: pc.localDescription.sdp,
    });
    if (!response.ok) throw new Error(String(response.status));
    if (record.closed) return;
    await pc.setRemoteDescription({ type: "answer", sdp: await response.text() });
    video.play().catch(() => {});
  } catch (error) {
    if (!record.closed) record.failedAt = Date.now();
  }
}

function syncMedia() {
  state.sources.forEach((source) => {
    const video = sourcesEl.querySelector(`[data-preview="${source.id}"]`);
    if (!video || !source.whep) return;
    const current = players.get(video);
    if (current && !current.closed && current.key === source.whep) return;
    connectWhep(video, source.whep, { audio: false, key: source.whep, heard: false, volume: 0 });
  });

  Object.entries(state.slots).forEach(([id, slot]) => {
    const video = slotsEl.querySelector(`[data-monitor="${id}"]`);
    if (!video) return;
    if (!slot.source || !slot.whep) {
      closePlayer(video);
      return;
    }
    const key = `${slot.source}|${slot.audio}`;
    const current = players.get(video);
    if (current && !current.closed && current.key === key) return;
    connectWhep(video, slot.whep, {
      audio: true,
      key,
      heard: video.dataset.heard === "1",
      volume: Number(video.dataset.volume || savedVolume(id)),
    });
  });
}

function checkStalls() {
  document.querySelectorAll("video").forEach((video) => {
    const record = players.get(video);
    if (!record || record.closed) return;
    const now = Date.now();
    const stateName = record.pc.connectionState;
    const failed = stateName === "failed" || stateName === "disconnected";
    const httpFailed = record.failedAt && now - record.failedAt > 2000;
    if (failed || httpFailed) {
      connectWhep(video, record.url, {
        audio: record.audio,
        key: record.key,
        heard: record.heard,
        volume: video.volume,
      });
      return;
    }
    if (video.paused) {
      video.play().catch(() => {});
      return;
    }
    const time = video.currentTime || 0;
    if (time > record.lastTime + 0.05) {
      record.lastTime = time;
      record.movedAt = now;
      return;
    }
    if (now - record.movedAt > 5000) {
      connectWhep(video, record.url, {
        audio: record.audio,
        key: record.key,
        heard: record.heard,
        volume: video.volume,
      });
    }
  });
}

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
  if (!res.ok) throw new Error(body.error || "Request failed");
  return body;
}

function badge(source) {
  if (source.routedTo.length) return ["onair", source.routedTo.join(" · ")];
  if (source.ready) return ["live", "live"];
  return ["", "no signal"];
}

function statusText(slot) {
  if (slot.status === "playing") return "On air";
  if (slot.status === "starting") return "Locking...";
  if (slot.status === "waiting") return "No signal";
  if (slot.status === "error") return "SDI output failed";
  return "Empty";
}

function shell() {
  sourcesEl.innerHTML = state.sources.map((source) => state.admin ? `
    <article class="card" data-source="${esc(source.id)}">
      <div class="preview">
        <video data-preview="${esc(source.id)}" playsinline autoplay muted></video>
      </div>
      <div class="card-foot">
        <div class="card-drag" draggable="true">
          <span class="name" data-label="${esc(source.id)}"></span>
          <span class="hint" data-url="${esc(source.id)}"></span>
        </div>
        <button type="button" data-edit="${esc(source.id)}">Edit</button>
        <span class="badge" data-badge="${source.id}"></span>
      </div>
    </article>
  ` : `
    <article class="card" data-source="${esc(source.id)}">
      <div class="preview card-drag" draggable="true">
        <video data-preview="${esc(source.id)}" playsinline autoplay muted></video>
      </div>
      <div class="card-foot">
        <span class="badge" data-badge="${source.id}"></span>
      </div>
    </article>
  `).join("");

  slotsEl.innerHTML = Object.entries(state.slots).map(([id, slot]) => `
    <article class="slot" data-slot="${id}">
      <div class="slot-top">
        <h3>${slot.label}</h3>
        <span class="lock-mark" data-lock-mark="${id}" hidden>Locked</span>
        <button type="button" class="lock" data-lock="${id}">Lock</button>
      </div>
      <div class="preview">
        <video data-monitor="${id}" playsinline autoplay muted></video>
      </div>
      <div class="listen">
        <button type="button" data-hear="${id}">Listen</button>
        <label>Monitor volume
          <input type="range" min="0" max="100" value="${Math.round(savedVolume(id) * 100)}" data-volume="${id}">
        </label>
      </div>
      <div class="drop" data-drop="${id}">
        <strong data-assigned="${id}"></strong>
        <span data-hint="${id}"></span>
      </div>
      <p class="status" data-status="${id}"></p>
      <div class="controls">
        <label>Video mode
          <select data-mode="${id}">
            ${MODES.map(([value, label]) => `<option value="${value}">${label}</option>`).join("")}
          </select>
        </label>
        <label>Audio
          <select data-audio="${id}">
            ${AUDIO.map(([value, label]) => `<option value="${value}">${label}</option>`).join("")}
          </select>
        </label>
        <button type="button" data-clear="${id}">Clear</button>
      </div>
      <p class="note" data-preview-note="${id}" hidden></p>
      <pre class="log" data-log="${id}" hidden></pre>
    </article>
  `).join("");

  sourcesEl.querySelectorAll(".card").forEach((card) => {
    const handle = card.querySelector(".card-drag");
    handle.addEventListener("dragstart", (event) => {
      event.dataTransfer.setData("text/plain", card.dataset.source);
      event.dataTransfer.effectAllowed = "copy";
      card.classList.add("dragging");
    });
    handle.addEventListener("dragend", () => card.classList.remove("dragging"));
    const edit = card.querySelector("[data-edit]");
    if (edit) {
      edit.addEventListener("click", () => {
        const source = state.sources.find((item) => item.id === card.dataset.source);
        if (source) openEditor(source);
      });
    }
  });

  slotsEl.querySelectorAll(".slot").forEach((slot) => {
    const id = slot.dataset.slot;
    const video = slot.querySelector("[data-monitor]");
    video.dataset.volume = String(savedVolume(id));
    video.volume = savedVolume(id);
    const canDrop = () => state.admin || !state.slots[id].locked;
    slot.addEventListener("dragover", (event) => {
      if (!canDrop()) return;
      event.preventDefault();
      slot.classList.add("over");
    });
    slot.addEventListener("dragleave", (event) => {
      if (!slot.contains(event.relatedTarget)) slot.classList.remove("over");
    });
    slot.addEventListener("drop", (event) => {
      if (!canDrop()) return;
      event.preventDefault();
      slot.classList.remove("over");
      const source = event.dataTransfer.getData("text/plain");
      if (source) save(id, { source });
    });
    slot.querySelector("[data-lock]").addEventListener("click", () => {
      const locked = !state.slots[id].locked;
      api(`/api/slots/${id}`, { method: "POST", body: JSON.stringify({ locked }) })
        .then((next) => {
          state = next;
          paint();
        })
        .catch((error) => window.alert(error.message));
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
    slot.querySelector("[data-hear]").addEventListener("click", (event) => {
      const hearing = video.dataset.heard === "1";
      video.dataset.heard = hearing ? "0" : "1";
      video.muted = hearing;
      const record = players.get(video);
      if (record) record.heard = !hearing;
      if (!hearing) video.play().catch(() => {});
      event.currentTarget.textContent = hearing ? "Listen" : "Mute";
    });
    slot.querySelector("[data-volume]").addEventListener("input", (event) => {
      const level = Number(event.target.value) / 100;
      video.volume = level;
      video.dataset.volume = String(level);
      sessionStorage.setItem(`listen-${id}`, String(level));
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
    const label = card.querySelector("[data-label]");
    if (label) label.textContent = source.label;
    const url = card.querySelector("[data-url]");
    if (url) url.textContent = source.url;
  });

  Object.entries(state.slots).forEach(([id, slot]) => {
    const box = slotsEl.querySelector(`[data-slot="${id}"]`);
    const held = slot.locked && !state.admin;
    box.classList.toggle("playing", slot.status === "playing");
    box.classList.toggle("locked", !!slot.locked);
    const assigned = box.querySelector("[data-assigned]");
    const hint = box.querySelector("[data-hint]");
    const named = state.sources.find((source) => source.id === slot.source);
    assigned.textContent = slot.source ? (state.admin ? slot.source : (named ? named.label : "Live")) : "Empty";
    hint.textContent = held
      ? "Locked"
      : (slot.source ? "Drop another source to replace" : "Drop a source here");
    const lockBtn = box.querySelector("[data-lock]");
    const lockMark = box.querySelector("[data-lock-mark]");
    lockBtn.textContent = slot.locked ? "Unlock" : "Lock";
    lockBtn.setAttribute("aria-pressed", slot.locked ? "true" : "false");
    lockMark.hidden = !slot.locked;
    const status = box.querySelector("[data-status]");
    status.className = `status ${slot.status}`;
    status.textContent = statusText(slot);
    const mode = box.querySelector("[data-mode]");
    const audio = box.querySelector("[data-audio]");
    if (document.activeElement !== mode) mode.value = slot.mode;
    if (document.activeElement !== audio) audio.value = slot.audio;
    const busy = slot.status === "starting";
    mode.disabled = busy || held;
    audio.disabled = busy || held;
    box.querySelector("[data-clear]").disabled = busy || held || !slot.source;
    const log = box.querySelector("[data-log]");
    const errorText = slot.error || "";
    log.hidden = slot.status !== "error" || !errorText;
    if (log.textContent !== errorText) log.textContent = errorText;
    const note = box.querySelector("[data-preview-note]");
    const noteText = slot.previewError || "";
    note.hidden = !noteText;
    if (note.textContent !== noteText) note.textContent = noteText;
    const hear = box.querySelector("[data-hear]");
    const video = box.querySelector("[data-monitor]");
    hear.textContent = video.dataset.heard === "1" ? "Mute" : "Listen";
    hear.disabled = !slot.source;
  });
  syncMedia();
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

function accountLabel(next) {
  if (!next.user) return "LAN, no login";
  return `${next.user} · ${next.group || (next.admin ? "IT" : "operator")}`;
}

function applyRole() {
  document.body.classList.toggle("is-operator", !state.admin);
  addSourceButton.hidden = !state.admin;
  const note = document.querySelector("#inputs-note");
  note.textContent = state.admin
    ? "Drag a name onto an SDI slot. Display name and source URL can be edited here."
    : "Drag a preview onto an SDI slot.";
  if (!state.admin) closeEditor();
}

async function tick() {
  try {
    const next = await api("/api/state");
    if (!next) return;
    const sourceKey = next.sources.map((source) => source.id).join(",");
    if (!state) {
      state = next;
      userEl.textContent = accountLabel(next);
      logoutEl.hidden = !next.auth;
      applyRole();
      shell();
    } else if (state.sources.map((source) => source.id).join(",") !== sourceKey || state.admin !== next.admin) {
      state = next;
      applyRole();
      shell();
    } else {
      state = next;
    }
    paint();
    checkStalls();
  } catch (error) {
    console.error(error);
  }
}

function nextUrl() {
  const used = new Set(state.sources.map((source) => source.url));
  let port = 3001;
  let url = `srt://127.0.0.1:${port}?mode=listener`;
  while (used.has(url)) {
    port += 1;
    url = `srt://127.0.0.1:${port}?mode=listener`;
  }
  return url;
}

function openEditor(source) {
  sourceForm.hidden = false;
  sourceForm.elements.label.value = source ? source.label : "";
  sourceForm.elements.id.value = source ? source.id : "";
  sourceForm.elements.id.disabled = Boolean(source);
  sourceForm.elements.url.value = source ? source.url : nextUrl();
  sourceForm.dataset.editing = source ? source.id : "";
  document.querySelector("#delete-source").hidden = !source;
  sourceForm.elements.label.focus();
}

function closeEditor() {
  sourceForm.hidden = true;
  sourceForm.dataset.editing = "";
}

async function applySourceResponse(next) {
  if (!next) return;
  if (next.warning) window.alert(next.warning);
  const sourceKey = next.sources.map((source) => source.id).join(",");
  const changed = !state || state.sources.map((source) => source.id).join(",") !== sourceKey;
  state = next;
  if (changed) shell();
  paint();
  closeEditor();
}

addSourceButton.addEventListener("click", () => openEditor(null));
document.querySelector("#cancel-source").addEventListener("click", closeEditor);
document.querySelector("#delete-source").addEventListener("click", async () => {
  const sourceId = sourceForm.dataset.editing;
  if (!sourceId) return;
  try {
    const next = await api(`/api/sources/${encodeURIComponent(sourceId)}/delete`, { method: "POST", body: "{}" });
    await applySourceResponse(next);
  } catch (error) {
    window.alert(error.message);
  }
});
sourceForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const editing = sourceForm.dataset.editing;
  const body = {
    label: sourceForm.elements.label.value.trim(),
    url: sourceForm.elements.url.value.trim(),
  };
  if (!editing) body.id = sourceForm.elements.id.value.trim();
  const path = editing ? `/api/sources/${encodeURIComponent(editing)}` : "/api/sources";
  try {
    const next = await api(path, { method: "POST", body: JSON.stringify(body) });
    await applySourceResponse(next);
  } catch (error) {
    window.alert(error.message);
  }
});

tick();
setInterval(tick, 2000);
