/* Trackline UI - vanilla JS, no build step. */
"use strict";

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

// ------------------------------------------------------------------ state
const S = {
  cfg: null,
  classes: {},            // id -> name for the active model
  meta: null,             // current video metadata
  frameImg: null,         // HTMLImageElement of scrubbed frame
  frameIndex: 0,
  selected: null,         // selected line id
  drag: null,             // {mode, id, end, start, orig}
  tab: "studio",
  run: null,
  modelInfo: null,
};

// ------------------------------------------------------------------ utils
function status(msg, kind = "") {
  const el = $("#statusline");
  el.textContent = `${new Date().toLocaleTimeString()}  ${msg}`;
  el.className = `statusline mono ${kind}`;
}

async function api(path, opts = {}) {
  const init = { ...opts, headers: { ...(opts.headers || {}) } };
  if (opts.json !== undefined) {
    init.body = JSON.stringify(opts.json);
    init.headers["Content-Type"] = "application/json";
  }
  const res = await fetch(path, init);
  const ct = res.headers.get("content-type") || "";
  const body = ct.includes("json") ? await res.json() : await res.text();
  if (!res.ok) {
    const detail = typeof body === "object" ? (body.detail ?? JSON.stringify(body)) : body;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return body;
}

function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

const fmtTime = (s) => {
  if (s == null || !isFinite(s)) return "--:--.---";
  const m = Math.floor(s / 60), r = s - m * 60;
  return `${String(m).padStart(2, "0")}:${r.toFixed(3).padStart(6, "0")}`;
};
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const uid = () => Math.random().toString(16).slice(2, 10);

// ------------------------------------------------------------------ tabs
function setTab(name) {
  S.tab = name;
  $$(".tab-btn").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".tab-pane").forEach((p) => p.classList.toggle("active", p.id === `tab-${name}`));
  if (name === "studio") requestAnimationFrame(resizeCanvas);
  if (name === "live") loadLiveStatus();
  history.replaceState(null, "", `#${name}`);
  syncStream();
}
$$(".tab-btn").forEach((b) => b.addEventListener("click", () => setTab(b.dataset.tab)));

// ------------------------------------------------------------------ config sync
// The UI state is the source of truth while editing: patches are applied
// locally at once and coalesced into a single debounced PUT. The server
// applies each PUT atomically, so a running pipeline sees it on its next frame.
let queuedPatch = null;
function patchConfig(patch) {
  Object.assign(S.cfg, patch);
  queuedPatch = Object.assign(queuedPatch || {}, patch);
  flushPatch();
}
const flushPatch = debounce(async () => {
  const p = queuedPatch; queuedPatch = null;
  if (!p) return;
  try { await api("/api/config", { method: "PUT", json: p }); }
  catch (e) { status(`config: ${e.message}`, "err"); }
}, 60);

const pushLines = () => patchConfig({ lines: S.cfg.lines.map((l) => ({ ...l })) });

// ------------------------------------------------------------------ models
async function loadModels() {
  try {
    const r = await api("/api/models");
    const sel = $("#model-select");
    sel.innerHTML = `<option value="">-- /models/*.pt (${r.available.length}) --</option>` +
      r.available.map((m) => `<option value="${esc(m.name)}">${esc(m.name)}  ${m.size_mb}MB</option>`).join("");
    sel.value = r.active || r.loading || "";
    applyModelInfo(r);
  } catch (e) { status(`models: ${e.message}`, "err"); }
}

function applyModelInfo(info) {
  const prevGen = S.modelInfo && S.modelInfo.generation;
  S.modelInfo = info;
  const el = $("#model-info");
  let stateCls = { ready: "ok", loading: "warn", error: "err" }[info.state] || "";
  el.innerHTML =
    `state   <span class="${stateCls}">${esc(info.state)}${info.loading ? " " + esc(info.loading) : ""}</span>\n` +
    `active  ${esc(info.active || "-")}\n` +
    `device  ${esc(info.device)}${info.load_ms ? `   load ${info.load_ms} ms` : ""}\n` +
    `classes ${Object.keys(info.classes || {}).length}` +
    (info.error ? `\n<span class="err">${esc(info.error)}</span>` : "");
  $("#sb-model").textContent = info.loading ? `${info.loading}...` : (info.active || "-");
  $("#sb-device").textContent = info.device || "-";
  if (info.generation !== prevGen) {
    S.classes = info.classes || {};
    renderClasses();
  }
}

$("#model-select").addEventListener("change", async (e) => {
  const name = e.target.value;
  if (!name) return;
  status(`loading ${name} ...`);
  e.target.disabled = true;
  try {
    const info = await api("/api/models/select", { method: "POST", json: { name } });
    applyModelInfo(info);
    S.cfg.model = name;
    status(`model ${name} active (${info.load_ms} ms, ${info.device})`, "ok");
  } catch (err) {
    status(err.message, "err");
    loadModels();
  } finally { e.target.disabled = false; }
});
$("#btn-models-refresh").addEventListener("click", loadModels);

// ------------------------------------------------------------------ classes
function renderClasses() {
  const list = $("#class-list");
  const ids = Object.keys(S.classes).map(Number).sort((a, b) => a - b);
  if (!ids.length) { list.innerHTML = `<div class="dim small">load a model to list classes</div>`; updateClassSummary(); return; }
  const q = $("#class-filter").value.trim().toLowerCase();
  const sel = new Set(S.cfg.classes);
  list.innerHTML = ids
    .filter((id) => !q || String(id) === q || S.classes[id].toLowerCase().includes(q))
    .map((id) => `<label class="${sel.has(id) ? "on" : ""}"><input type="checkbox" data-id="${id}" ${sel.has(id) ? "checked" : ""}>` +
      `<span class="cid">${id}</span>${esc(S.classes[id])}</label>`).join("") || `<div class="dim small">no match</div>`;
  updateClassSummary();
}
function updateClassSummary() {
  const c = S.cfg ? S.cfg.classes : [];
  $("#class-summary").textContent = c.length
    ? c.slice(0, 4).map((id) => S.classes[id] ?? id).join(", ") + (c.length > 4 ? ` +${c.length - 4}` : "")
    : "all";
}
$("#class-list").addEventListener("change", (e) => {
  const id = Number(e.target.dataset.id);
  const set = new Set(S.cfg.classes);
  e.target.checked ? set.add(id) : set.delete(id);
  e.target.parentElement.classList.toggle("on", e.target.checked);
  patchConfig({ classes: [...set].sort((a, b) => a - b) });
  updateClassSummary();
});
$("#class-filter").addEventListener("input", renderClasses);
$("#btn-cls-none").addEventListener("click", () => { patchConfig({ classes: [] }); renderClasses(); });

// ------------------------------------------------------------------ parameters
function bindParams() {
  const c = S.cfg;
  const conf = $("#p-conf"), iou = $("#p-iou");
  conf.value = c.conf; $("#v-conf").textContent = (+c.conf).toFixed(2);
  iou.value = c.iou; $("#v-iou").textContent = (+c.iou).toFixed(2);
  $("#p-tracker").value = c.tracker;
  $("#p-imgsz").value = String(c.imgsz);
  $("#p-anchor").value = c.anchor;
  $("#p-realtime").checked = c.realtime;
}
$("#p-conf").addEventListener("input", (e) => { $("#v-conf").textContent = (+e.target.value).toFixed(2); patchConfig({ conf: +e.target.value }); });
$("#p-iou").addEventListener("input", (e) => { $("#v-iou").textContent = (+e.target.value).toFixed(2); patchConfig({ iou: +e.target.value }); });
$("#p-tracker").addEventListener("change", (e) => patchConfig({ tracker: e.target.value }));
$("#p-imgsz").addEventListener("change", (e) => patchConfig({ imgsz: +e.target.value }));
$("#p-anchor").addEventListener("change", (e) => patchConfig({ anchor: e.target.value }));
$("#p-realtime").addEventListener("change", (e) => patchConfig({ realtime: e.target.checked }));

$("#btn-save").addEventListener("click", async () => {
  const btn = $("#btn-save");
  btn.disabled = true;
  const out = $("#save-result");
  try {
    await flushNow();
    const r = await api("/api/config/save", { method: "POST" });
    out.innerHTML = `<span class="ok">saved</span> ${esc(r.saved)}`;
    status("config saved to " + r.saved, "ok");
  } catch (e) { out.innerHTML = `<span class="err">${esc(e.message)}</span>`; status(e.message, "err"); }
  finally { btn.disabled = false; }
});

async function flushNow() {
  if (!queuedPatch) return;
  const p = queuedPatch; queuedPatch = null;
  await api("/api/config", { method: "PUT", json: p });
}

// ------------------------------------------------------------------ videos
async function loadVideos() {
  try {
    const r = await api("/api/videos");
    const opts = `<option value="">-- /videos (${r.videos.length}) --</option>` +
      r.videos.map((v) => `<option value="${esc(v.name)}">${esc(v.name)}  ${v.size_mb}MB</option>`).join("");
    $("#video-select").innerHTML = opts;
    $("#run-video").innerHTML = opts;
    const cur = S.cfg.video && r.videos.some((v) => v.name === S.cfg.video) ? S.cfg.video : "";
    $("#video-select").value = cur;
    $("#run-video").value = cur;
    if (cur && (!S.meta || S.meta.name !== cur)) await openVideo(cur);
  } catch (e) { status(`videos: ${e.message}`, "err"); }
}

async function openVideo(name) {
  if (!name) { S.meta = null; S.frameImg = null; updateSourceUI(); draw(); return; }
  try {
    S.meta = await api(`/api/videos/${encodeURIComponent(name)}/meta`);
    const scrub = $("#scrub");
    scrub.max = Math.max(0, S.meta.frames - 1);
    scrub.disabled = false;
    scrub.value = 0;
    updateSourceUI();
    await requestFrame(0);
    status(`opened ${name}`);
  } catch (e) { status(e.message, "err"); }
}

function updateSourceUI() {
  const m = S.meta;
  $("#src-meta").textContent = m ? `${m.name}  ${m.width}x${m.height}  ${m.fps}fps  ${m.frames}f  ${fmtTime(m.duration_s)}` : "no source";
  $("#canvas-empty").classList.toggle("hidden", !!m);
  if (!m) { $("#scrub").disabled = true; $("#scrub-readout").textContent = "0 / 0"; }
}

function onVideoChosen(name) {
  $("#video-select").value = name;
  $("#run-video").value = name;
  patchConfig({ video: name || null });
  openVideo(name);
}
$("#video-select").addEventListener("change", (e) => onVideoChosen(e.target.value));
$("#run-video").addEventListener("change", (e) => onVideoChosen(e.target.value));
$("#btn-videos-refresh").addEventListener("click", loadVideos);

$("#video-upload").addEventListener("change", (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const bar = $("#upload-progress");
  bar.classList.remove("hidden");
  const fd = new FormData();
  fd.append("file", file);
  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/videos/upload");
  xhr.upload.onprogress = (ev) => { if (ev.lengthComputable) bar.firstElementChild.style.width = `${(ev.loaded / ev.total) * 100}%`; };
  xhr.onload = async () => {
    bar.classList.add("hidden"); bar.firstElementChild.style.width = "0";
    e.target.value = "";
    if (xhr.status >= 300) { status(`upload failed: ${xhr.responseText}`, "err"); return; }
    const r = JSON.parse(xhr.responseText);
    status(`uploaded ${r.name}`, "ok");
    S.cfg.video = r.name;
    await loadVideos();
    onVideoChosen(r.name);
  };
  xhr.onerror = () => { bar.classList.add("hidden"); status("upload failed", "err"); };
  xhr.send(fd);
  status(`uploading ${file.name} ...`);
});

// ------------------------------------------------------------------ scrubber
// Latest-wins frame fetching: at most one request in flight, newest index queued.
let frameInflight = false, frameQueued = null, frameURL = null;
async function requestFrame(idx) {
  if (!S.meta) return;
  if (frameInflight) { frameQueued = idx; return; }
  frameInflight = true;
  try {
    const res = await fetch(`/api/videos/${encodeURIComponent(S.meta.name)}/frame?index=${idx}`);
    if (!res.ok) throw new Error(await res.text());
    const real = +res.headers.get("X-Frame-Index");
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const img = new Image();
    await new Promise((ok, err) => { img.onload = ok; img.onerror = err; img.src = url; });
    if (frameURL) URL.revokeObjectURL(frameURL);
    frameURL = url;
    const first = !S.frameImg;
    S.frameImg = img;
    S.frameIndex = real;
    $("#scrub-readout").textContent = `${real} / ${S.meta.frames - 1}`;
    $("#scrub-time").textContent = fmtTime(real / S.meta.fps);
    first ? resizeCanvas() : draw();
  } catch (e) { status(`frame: ${e.message}`, "err"); }
  finally {
    frameInflight = false;
    if (frameQueued !== null) { const q = frameQueued; frameQueued = null; requestFrame(q); }
  }
}
$("#scrub").addEventListener("input", (e) => {
  const i = +e.target.value;
  $("#scrub-readout").textContent = `${i} / ${S.meta ? S.meta.frames - 1 : 0}`;
  requestFrame(i);
});
function stepFrame(d) {
  if (!S.meta) return;
  const s = $("#scrub");
  s.value = Math.min(+s.max, Math.max(0, +s.value + d));
  requestFrame(+s.value);
}
$$("[data-step]").forEach((b) => b.addEventListener("click", () => stepFrame(+b.dataset.step)));

// ------------------------------------------------------------------ canvas
const canvas = $("#canvas");
const ctx = canvas.getContext("2d");
const HANDLE = 7;      // css px
const HIT = 9;

function resizeCanvas() {
  const wrap = $("#canvas-wrap");
  const pad = 24;
  const W = wrap.clientWidth - pad * 2, H = wrap.clientHeight - pad * 2;
  if (W <= 0 || H <= 0) return;
  const ar = S.meta ? S.meta.width / S.meta.height : 16 / 9;
  let w = W, h = W / ar;
  if (h > H) { h = H; w = H * ar; }
  const dpr = window.devicePixelRatio || 1;
  canvas.style.width = `${Math.floor(w)}px`;
  canvas.style.height = `${Math.floor(h)}px`;
  canvas.width = Math.floor(w * dpr);
  canvas.height = Math.floor(h * dpr);
  draw();
}
new ResizeObserver(() => S.tab === "studio" && resizeCanvas()).observe($("#canvas-wrap"));

const cssW = () => canvas.width / (window.devicePixelRatio || 1);
const cssH = () => canvas.height / (window.devicePixelRatio || 1);
const toPx = (p) => [p[0] * cssW(), p[1] * cssH()];
const toNorm = (x, y) => [Math.min(1, Math.max(0, x / cssW())), Math.min(1, Math.max(0, y / cssH()))];

function draw() {
  const dpr = window.devicePixelRatio || 1;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const w = cssW(), h = cssH();
  ctx.clearRect(0, 0, w, h);
  if (S.frameImg) ctx.drawImage(S.frameImg, 0, 0, w, h);
  else { ctx.fillStyle = "#0f0f12"; ctx.fillRect(0, 0, w, h); }
  if (!S.cfg) return;

  ctx.font = "11px " + getComputedStyle(document.body).getPropertyValue("--mono");
  for (const ln of S.cfg.lines) drawLine(ln, ln.id === S.selected);
  if (S.drag && S.drag.mode === "new" && S.drag.preview) drawLine(S.drag.preview, true);
}

function drawLine(ln, sel) {
  const [ax, ay] = toPx(ln.p1), [bx, by] = toPx(ln.p2);
  const color = sel ? "#f5a524" : "#fafafa";
  ctx.lineCap = "square";
  ctx.strokeStyle = "rgba(0,0,0,.85)"; ctx.lineWidth = 5;
  ctx.beginPath(); ctx.moveTo(ax, ay); ctx.lineTo(bx, by); ctx.stroke();
  ctx.strokeStyle = color; ctx.lineWidth = 2;
  ctx.beginPath(); ctx.moveTo(ax, ay); ctx.lineTo(bx, by); ctx.stroke();

  // IN-direction normal (matches backend: n = (-dy, dx) in pixel space).
  const dx = (ln.p2[0] - ln.p1[0]) * cssW(), dy = (ln.p2[1] - ln.p1[1]) * cssH();
  const len = Math.hypot(dx, dy) || 1;
  const nx = -dy / len, ny = dx / len;
  const mx = (ax + bx) / 2, my = (ay + by) / 2, L = 22;
  const tx = mx + nx * L, ty = my + ny * L;
  ctx.lineWidth = 1.5;
  ctx.beginPath(); ctx.moveTo(mx, my); ctx.lineTo(tx, ty);
  ctx.moveTo(tx, ty); ctx.lineTo(tx - nx * 6 + ny * 4, ty - ny * 6 - nx * 4);
  ctx.moveTo(tx, ty); ctx.lineTo(tx - nx * 6 - ny * 4, ty - ny * 6 + nx * 4);
  ctx.stroke();
  label(ctx, "IN", tx + nx * 8 - 7, ty + ny * 8 + 4, color);

  for (const [x, y] of [[ax, ay], [bx, by]]) {
    ctx.fillStyle = "#09090b"; ctx.fillRect(x - HANDLE / 2 - 1, y - HANDLE / 2 - 1, HANDLE + 2, HANDLE + 2);
    ctx.fillStyle = color; ctx.fillRect(x - HANDLE / 2, y - HANDLE / 2, HANDLE, HANDLE);
  }
  if (ln.name) {
    const t = ln.name;
    const tw = ctx.measureText(t).width;
    const lx = Math.min(Math.max(ax + 6, 2), cssW() - tw - 10), ly = Math.max(ay - 10, 14);
    ctx.fillStyle = "rgba(9,9,11,.9)"; ctx.fillRect(lx - 4, ly - 11, tw + 8, 15);
    ctx.fillStyle = color; ctx.fillText(t, lx, ly);
  }
}
function label(c, t, x, y, color) {
  const tw = c.measureText(t).width;
  c.fillStyle = "rgba(9,9,11,.9)"; c.fillRect(x - 3, y - 11, tw + 6, 15);
  c.fillStyle = color; c.fillText(t, x, y);
}

function hitTest(x, y) {
  if (!S.cfg) return null;
  for (const ln of [...S.cfg.lines].reverse()) {
    const a = toPx(ln.p1), b = toPx(ln.p2);
    if (Math.hypot(x - a[0], y - a[1]) <= HIT) return { id: ln.id, end: "p1" };
    if (Math.hypot(x - b[0], y - b[1]) <= HIT) return { id: ln.id, end: "p2" };
  }
  for (const ln of [...S.cfg.lines].reverse()) {
    const a = toPx(ln.p1), b = toPx(ln.p2);
    const vx = b[0] - a[0], vy = b[1] - a[1];
    const t = Math.max(0, Math.min(1, ((x - a[0]) * vx + (y - a[1]) * vy) / (vx * vx + vy * vy || 1)));
    if (Math.hypot(x - (a[0] + t * vx), y - (a[1] + t * vy)) <= 6) return { id: ln.id, end: null };
  }
  return null;
}

function evPos(e) {
  const r = canvas.getBoundingClientRect();
  return [e.clientX - r.left, e.clientY - r.top];
}

canvas.addEventListener("pointerdown", (e) => {
  if (!S.meta || !S.cfg || e.button !== 0) return;
  canvas.setPointerCapture(e.pointerId);
  const [x, y] = evPos(e);
  const hit = hitTest(x, y);
  if (hit) {
    S.selected = hit.id;
    const ln = S.cfg.lines.find((l) => l.id === hit.id);
    S.drag = { mode: hit.end ? "end" : "move", id: hit.id, end: hit.end, start: toNorm(x, y), orig: { p1: [...ln.p1], p2: [...ln.p2] } };
  } else {
    S.selected = null;
    const p = toNorm(x, y);
    S.drag = { mode: "new", start: p, preview: null };
  }
  renderZones(); draw();
});

canvas.addEventListener("pointermove", (e) => {
  const [x, y] = evPos(e);
  if (!S.drag) {
    const hit = hitTest(x, y);
    canvas.style.cursor = hit ? (hit.end ? "grab" : "move") : "crosshair";
    return;
  }
  const p = toNorm(x, y);
  const d = S.drag;
  if (d.mode === "new") {
    d.preview = { id: "_preview", name: "", p1: d.start, p2: p };
  } else {
    const ln = S.cfg.lines.find((l) => l.id === d.id);
    if (!ln) return;
    if (d.mode === "end") ln[d.end] = p;
    else {
      // Move whole line, clamped so both endpoints stay inside the frame.
      let dx = p[0] - d.start[0], dy = p[1] - d.start[1];
      const xs = [d.orig.p1[0], d.orig.p2[0]], ys = [d.orig.p1[1], d.orig.p2[1]];
      dx = Math.min(1 - Math.max(...xs), Math.max(-Math.min(...xs), dx));
      dy = Math.min(1 - Math.max(...ys), Math.max(-Math.min(...ys), dy));
      ln.p1 = [d.orig.p1[0] + dx, d.orig.p1[1] + dy];
      ln.p2 = [d.orig.p2[0] + dx, d.orig.p2[1] + dy];
    }
    pushLines();   // live: running pipeline picks it up next frame
    renderZones(true);
  }
  draw();
});

function endDrag() {
  const d = S.drag;
  S.drag = null;
  if (!d) return;
  if (d.mode === "new" && d.preview) {
    const [ax, ay] = toPx(d.preview.p1), [bx, by] = toPx(d.preview.p2);
    if (Math.hypot(bx - ax, by - ay) >= 12) {
      const id = uid();
      const n = nextLineName();
      S.cfg.lines.push({ id, name: n, p1: d.preview.p1, p2: d.preview.p2 });
      S.selected = id;
      pushLines();
      status(`added ${n}`);
    }
  } else if (d.mode !== "new") pushLines();
  renderZones(); draw();
}
canvas.addEventListener("pointerup", endDrag);
canvas.addEventListener("pointercancel", endDrag);

function nextLineName() {
  const used = new Set(S.cfg.lines.map((l) => l.name));
  let i = 1;
  while (used.has(`L${i}`)) i++;
  return `L${i}`;
}

// ------------------------------------------------------------------ zones panel
function renderZones(coordsOnly = false) {
  const lines = S.cfg ? S.cfg.lines : [];
  $("#zone-count").textContent = lines.length;
  const box = $("#zone-list");
  const f = (p) => `${p[0].toFixed(3)},${p[1].toFixed(3)}`;
  if (coordsOnly) {
    for (const ln of lines) {
      const c = box.querySelector(`[data-id="${ln.id}"] .coords`);
      if (c) c.textContent = `(${f(ln.p1)}) → (${f(ln.p2)})`;
    }
    return;
  }
  if (!lines.length) { box.innerHTML = `<div class="dim mono small">no lines - drag on the canvas</div>`; return; }
  box.innerHTML = lines.map((ln) => `
    <div class="zone ${ln.id === S.selected ? "sel" : ""}" data-id="${ln.id}">
      <span class="sw"></span>
      <input type="text" value="${esc(ln.name)}" maxlength="32" spellcheck="false">
      <button class="btn tiny" data-act="flip" title="swap IN/OUT direction">FLIP</button>
      <button class="btn tiny" data-act="del" title="delete">DEL</button>
      <span class="coords">(${f(ln.p1)}) → (${f(ln.p2)})</span>
    </div>`).join("");
}
$("#zone-list").addEventListener("click", (e) => {
  const row = e.target.closest(".zone");
  if (!row) return;
  const id = row.dataset.id;
  const act = e.target.dataset.act;
  if (act === "del") deleteLine(id);
  else if (act === "flip") {
    const ln = S.cfg.lines.find((l) => l.id === id);
    [ln.p1, ln.p2] = [ln.p2, ln.p1];
    pushLines(); renderZones(); draw();
  } else if (S.selected !== id) { S.selected = id; renderZones(); draw(); }
});
$("#zone-list").addEventListener("input", (e) => {
  const row = e.target.closest(".zone");
  if (!row || e.target.tagName !== "INPUT") return;
  const ln = S.cfg.lines.find((l) => l.id === row.dataset.id);
  ln.name = e.target.value.trim() || ln.name;
  pushLines(); draw();
});
function deleteLine(id) {
  S.cfg.lines = S.cfg.lines.filter((l) => l.id !== id);
  if (S.selected === id) S.selected = null;
  pushLines(); renderZones(); draw();
}
$("#btn-zones-clear").addEventListener("click", () => {
  if (!S.cfg.lines.length || !confirm("Remove all lines?")) return;
  S.cfg.lines = []; S.selected = null; pushLines(); renderZones(); draw();
});

document.addEventListener("keydown", (e) => {
  if (S.tab !== "studio" || /INPUT|SELECT|TEXTAREA/.test(document.activeElement.tagName)) return;
  if ((e.key === "Delete" || e.key === "Backspace") && S.selected) { e.preventDefault(); deleteLine(S.selected); }
  else if (e.key === "ArrowLeft") { e.preventDefault(); stepFrame(e.shiftKey ? -10 : -1); }
  else if (e.key === "ArrowRight") { e.preventDefault(); stepFrame(e.shiftKey ? 10 : 1); }
  else if (e.key === "Escape") { S.selected = null; renderZones(); draw(); }
});

// ------------------------------------------------------------------ execution
const ACTIVE = new Set(["starting", "running", "paused"]);

$("#btn-start").addEventListener("click", async () => {
  const video = $("#run-video").value || S.cfg.video;
  if (!video) { status("select a video first", "err"); return; }
  try {
    await flushNow();
    const r = await api("/api/run/start", { method: "POST", json: { video } });
    applyRun(r);
    status(`started on ${video}`, "ok");
  } catch (e) { status(e.message, "err"); }
});
$("#btn-pause").addEventListener("click", async () => {
  const paused = S.run && S.run.state === "paused";
  try { applyRun(await api(paused ? "/api/run/resume" : "/api/run/pause", { method: "POST" })); }
  catch (e) { status(e.message, "err"); }
});
$("#btn-stop").addEventListener("click", async () => {
  try { applyRun(await api("/api/run/stop", { method: "POST" })); status("stopped"); }
  catch (e) { status(e.message, "err"); }
});
$("#btn-reset").addEventListener("click", async () => {
  try { applyRun(await api("/api/run/reset", { method: "POST" })); status("counts reset"); }
  catch (e) { status(e.message, "err"); }
});

let streamOn = false;
function syncStream() {
  const img = $("#stream");
  const st = S.run ? S.run.state : "idle";
  const want = S.tab === "execute" && ACTIVE.has(st);
  if (want && !streamOn) {
    img.src = `/api/run/stream.mjpg?t=${Date.now()}`;
    streamOn = true;
  } else if (!want && streamOn) {
    // Release the long-lived MJPEG connection; show the final frame as a still.
    streamOn = false;
    img.src = S.tab === "execute" && S.run && S.run.frame > 0 ? `/api/run/frame.jpg?t=${Date.now()}` : "";
  } else if (!want && S.tab === "execute" && S.run && S.run.frame > 0 && !img.getAttribute("src")) {
    img.src = `/api/run/frame.jpg?t=${Date.now()}`;
  }
  $("#stream-empty").classList.toggle("hidden", !!img.getAttribute("src"));
}
$("#stream").addEventListener("error", () => { $("#stream").removeAttribute("src"); streamOn = false; $("#stream-empty").classList.remove("hidden"); });

let lastState = null;
function applyRun(r) {
  S.run = r;
  const st = r.state;
  const badge = $("#run-state");
  badge.textContent = st.toUpperCase();
  badge.className = "badge " + ({ running: "run", starting: "run", paused: "warn", error: "err", completed: "ok" }[st] || "");
  $("#sb-engine").textContent = st;
  $("#run-source").textContent = r.source ? `${r.source}  ${r.resolution[0]}x${r.resolution[1]}  src ${r.src_fps}fps  model ${r.model || "-"}` : "-";

  const active = ACTIVE.has(st);
  $("#btn-start").disabled = active;
  $("#btn-pause").disabled = !active;
  $("#btn-pause").textContent = st === "paused" ? "RESUME" : "PAUSE";
  $("#btn-stop").disabled = !active;
  $("#run-video").disabled = active;

  $("#m-fps").textContent = r.fps.toFixed(1);
  $("#m-infer").textContent = `${r.infer_ms.toFixed(1)} ms`;
  $("#m-frame").textContent = r.total_frames ? `${r.frame}/${r.total_frames}` : r.frame;
  $("#m-tracks").textContent = r.active_tracks;
  $("#m-elapsed").textContent = `${r.elapsed_s.toFixed(0)}s`;
  $("#m-events").textContent = r.event_count;
  $("#t-in").textContent = r.totals.in;
  $("#t-out").textContent = r.totals.out;
  $("#t-total").textContent = r.totals.total;
  $("#run-progress > i").style.width = r.progress != null ? `${Math.min(100, r.progress * 100)}%` : "0";

  $("#tbl-classes").innerHTML = r.classes.length
    ? r.classes.map((c) => `<tr><td>${c.id}</td><td>${esc(c.name)}</td><td>${c.in}</td><td>${c.out}</td><td>${c.total}</td></tr>`).join("")
    : `<tr><td colspan="5" class="dim">-</td></tr>`;
  $("#tbl-lines").innerHTML = r.lines.length
    ? r.lines.map((l) => `<tr><td>${esc(l.name)}</td><td>${l.in}</td><td>${l.out}</td></tr>`).join("")
    : `<tr><td colspan="3" class="dim">-</td></tr>`;
  $("#event-log").innerHTML = r.recent_events.map((e) =>
    `<div>${e.timestamp.slice(11, 23)}  f${String(e.frame).padStart(6, " ")}  #${String(e.track_id).padEnd(4, " ")} ` +
    `<span class="dir-${e.direction}">${e.direction.toUpperCase().padEnd(3, " ")}</span> ${esc(e.line_name)}  ${esc(e.class_name)}</div>`
  ).join("") || `<div class="dim">no crossings yet</div>`;

  const err = $("#run-error");
  err.classList.toggle("hidden", !r.error);
  err.textContent = r.error || "";
  if (st !== lastState) {
    if (st === "completed") status(`run completed: ${r.totals.total} crossings, ${r.event_count} events`, "ok");
    if (st === "error") status(r.error, "err");
    lastState = st;
  }
  syncStream();
}

// ------------------------------------------------------------------ telemetry socket
function connectWS() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/telemetry`);
  const dot = $("#sb-ws");
  ws.onopen = () => { dot.className = "dot on"; };
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    applyRun(m.run);
    const mi = m.model;
    if (!S.modelInfo || mi.generation !== S.modelInfo.generation || mi.state !== S.modelInfo.state) {
      applyModelInfo(mi);
      if (mi.state === "ready" && mi.active && $("#model-select").value !== mi.active) $("#model-select").value = mi.active;
    }
  };
  ws.onclose = () => { dot.className = "dot off"; setTimeout(connectWS, 1500); };
  ws.onerror = () => ws.close();
}

// ------------------------------------------------------------------ live (WIP)
async function loadLiveStatus() {
  try {
    const s = await api("/api/live/status");
    $("#live-status").innerHTML =
      `enabled  ${s.enabled}\nstate    <span class="warn">${esc(s.state)}</span>\ninputs   ${esc(s.supported_inputs.join(", "))}\n\n${esc(s.detail)}`;
  } catch (e) { $("#live-status").textContent = e.message; }
}
$("#live-uri").addEventListener("input", (e) => {
  const v = e.target.value.trim();
  const b = $("#live-validate");
  if (!v) { b.textContent = "-"; b.className = "badge"; return; }
  const ok = /^\d+$/.test(v) || /^(rtsp|rtsps|rtmp|https?):\/\//i.test(v);
  b.textContent = ok ? (/^\d+$/.test(v) ? "WEBCAM" : "URL OK") : "INVALID";
  b.className = "badge " + (ok ? "" : "err");
});

// ------------------------------------------------------------------ boot
(async function boot() {
  try {
    const [h, c] = await Promise.all([api("/api/health"), api("/api/config")]);
    $("#ver").textContent = `v${h.version}`;
    S.cfg = c.config;
    bindParams();
    renderZones();
    await Promise.all([loadModels(), loadVideos()]);
    resizeCanvas();
    connectWS();
    status("ready", "ok");
  } catch (e) { status(`boot failed: ${e.message}`, "err"); }
  const h = location.hash.replace("#", "");
  if (["studio", "execute", "live"].includes(h)) setTab(h);
})();
