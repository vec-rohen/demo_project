"use strict";

/* ------------------------------------------------------------------ state */
const S = {
  config: null,
  tags: {},            // name -> tag metadata
  values: {},          // name -> live value
  alarms: [],          // visible alarms from the server
  events: [],          // journal, oldest first
  lastSeq: 0,
  connected: false,
  history: {},         // name -> [[t, v], ...]
  lastHistoryT: 0,
  tab: "overview",
  pens: new Set(["AI_TankLevel", "AI_TankTemp", "SP_LevelTarget", "AO_PumpSpeed"]),
  trendWindow: 300,
  eventFilter: "",
  hoverX: null,
};
const HISTORY_SECONDS = 900;
const PEN_COLORS = ["#4e9be6", "#e8875a", "#5fbf8f", "#c77dd6", "#d9b44a", "#6cc7d9", "#e46a8a", "#9aa0a8"];
const KPI_TAGS = ["AI_TankLevel", "AI_TankTemp", "AI_InletFlow", "AI_OutletFlow", "AI_PumpPressure", "AI_MotorCurrent"];

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const el = (tag, attrs = {}, ...children) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null && v !== false) node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children) if (c != null) node.append(c);
  return node;
};

/* -------------------------------------------------------------- helpers */
function fmt(name, value = S.values[name], withUnit = true) {
  const tag = S.tags[name];
  if (value === undefined || value === null || !tag) return "—";
  if (tag.type === "bool") return value ? "ON" : "OFF";
  const text = Number(value).toFixed(tag.decimals ?? 1);
  return withUnit && tag.unit ? `${text} ${tag.unit}` : text;
}
const pct = (name, v = S.values[name]) => {
  const t = S.tags[name];
  if (!t || t.min == null || t.max == null || v == null) return 0;
  return Math.max(0, Math.min(100, ((v - t.min) / (t.max - t.min)) * 100));
};
const timeStr = (t) => new Date(t * 1000).toLocaleTimeString([], { hour12: false });
const isAuto = () => !!S.values.SP_AutoMode;

function tagAlarm(name) {
  // Highest-priority *active* alarm on a tag, or null.
  return S.alarms.find((a) => a.tag === name && a.active) || null;
}
function alarmLimit(name, cond) {
  const spec = S.tags[name]?.alarm?.[cond];
  if (spec === undefined) return null;
  return typeof spec === "string" ? S.values[spec] : spec;
}

function toast(message, kind = "ok") {
  const node = el("div", { class: `toast ${kind}`, role: kind === "error" ? "alert" : "status", text: message });
  $("#toasts").append(node);
  setTimeout(() => node.remove(), kind === "error" ? 6000 : 3000);
}

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `${res.status} ${res.statusText}`);
  return data;
}

function confirmChange(title, html) {
  const dlg = $("#confirm");
  $("#confirm-title").textContent = title;
  $("#confirm-body").innerHTML = html;
  dlg.returnValue = "";
  dlg.showModal();
  $("#confirm-ok").focus();
  return new Promise((resolve) => dlg.addEventListener("close", () => resolve(dlg.returnValue === "ok"), { once: true }));
}
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

async function writeTag(name, value, { confirm = true } = {}) {
  const tag = S.tags[name];
  if (confirm) {
    const ok = await confirmChange(
      `Change ${tag.description}?`,
      `<span class="sp-tag">${esc(name)}</span><br><span class="change">${esc(fmt(name))} → ${esc(fmt(name, value))}</span>`,
    );
    if (!ok) return false;
  }
  try {
    const res = await api("POST", `/api/tags/${encodeURIComponent(name)}`, { value });
    S.values[name] = res.value;
    toast(`${tag.description} set to ${fmt(name, res.value)}`);
    renderAll();
    return true;
  } catch (err) {
    toast(err.message, "error");
    return false;
  }
}

/* ---------------------------------------------------------- live data */
function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  let retry = 1000;
  ws.onopen = () => { retry = 1000; };
  ws.onmessage = (msg) => applyUpdate(JSON.parse(msg.data));
  ws.onclose = () => {
    S.connected = null; // web link down (distinct from PLC link down)
    renderStatus();
    setTimeout(connect, retry);
    retry = Math.min(retry * 2, 10000);
  };
}

function applyUpdate(msg) {
  S.values = msg.values;
  S.alarms = msg.alarms;
  S.connected = msg.connected;
  for (const ev of msg.events) {
    if (ev.seq > S.lastSeq) { S.events.push(ev); S.lastSeq = ev.seq; }
  }
  if (S.events.length > 500) S.events.splice(0, S.events.length - 500);
  if (msg.t - S.lastHistoryT >= 1) {
    S.lastHistoryT = msg.t;
    for (const [name, buf] of Object.entries(S.history)) {
      if (msg.values[name] !== undefined) buf.push([msg.t, Number(msg.values[name])]);
      while (buf.length && buf[0][0] < msg.t - HISTORY_SECONDS) buf.shift();
    }
  }
  renderAll();
}

/* ------------------------------------------------------------ rendering */
function renderAll() {
  renderStatus();
  renderBoundValues();
  if (S.tab === "overview") renderOverview();
  if (S.tab === "io") renderIO();
  if (S.tab === "setpoints") renderSetpoints();
  if (S.tab === "trends") renderTrend();
  if (S.tab === "alarms") renderAlarms();
}

function renderStatus() {
  const chip = $("#conn-chip");
  chip.className = "chip " + (S.connected ? "ok" : "bad");
  $("#conn-text").textContent = S.connected === null ? "HMI server offline" : S.connected ? "PLC online" : "PLC comms lost";
  const mode = $("#mode-chip");
  mode.textContent = isAuto() ? "AUTO" : "MANUAL";
  mode.className = "chip " + (isAuto() ? "" : "manual");

  const unacked = S.alarms.filter((a) => !a.acked);
  const active = S.alarms.filter((a) => a.active);
  const banner = $("#alarm-banner");
  banner.hidden = S.alarms.length === 0;
  if (S.alarms.length) {
    const top = S.alarms[0];
    banner.className = `alarm-banner ${top.priority} ${unacked.length ? "unacked" : ""}`;
    $("#alarm-banner-count").textContent = S.alarms.length;
    $("#alarm-banner-text").textContent =
      `${top.message}${top.active ? "" : " (cleared)"} · ${active.length} active, ${unacked.length} unacknowledged`;
  }
  const badge = $("#alarm-tab-badge");
  badge.hidden = unacked.length === 0;
  badge.textContent = unacked.length;
}

function renderBoundValues() {
  for (const node of $$("[data-value]")) node.textContent = fmt(node.dataset.value);
}

/* --- overview */
function renderOverview() {
  const v = S.values;
  const toggle = (sel, cls, on) => $(sel).classList.toggle(cls, !!on);
  toggle("#pump", "on", v.DI_PumpRunning);
  toggle("#pump", "alarm", !v.DI_EStopOK); // interlocked off
  toggle("#pipe-in", "flowing", v.AI_InletFlow > 0.5);
  toggle("#pipe-out", "flowing", v.AI_OutletFlow > 0.5 && v.AO_OutletValve > 0);
  toggle("#pipe-drain", "flowing", v.DO_DrainValve && v.AI_TankLevel > 0.5);
  toggle("#valve", "on", v.AO_OutletValve > 0);
  toggle("#drain", "on", v.DO_DrainValve);
  toggle("#heater", "on", v.DO_HeaterOn && v.AI_TankLevel > 15);
  toggle("#lsh", "on", v.DI_LevelHighSw);
  toggle("#lsl", "on", v.DI_LevelLowSw);

  const top = 70, height = 230, yFor = (p) => top + height * (1 - p / 100);
  const fill = $("#tank-fill");
  fill.setAttribute("y", yFor(pct("AI_TankLevel")));
  fill.setAttribute("height", height * pct("AI_TankLevel") / 100);
  for (const [id, val] of [["#tank-sp", v.SP_LevelTarget], ["#tank-hi", alarmLimit("AI_TankLevel", "hi")]]) {
    const line = $(id);
    line.style.display = val == null ? "none" : "";
    if (val != null) { line.setAttribute("y1", yFor(val)); line.setAttribute("y2", yFor(val)); }
  }
  for (const r of $$(".readout")) {
    const name = $("[data-value]", r)?.dataset.value;
    r.classList.toggle("alarm", !!tagAlarm(name));
  }
  $("#mimic-mode").textContent = isAuto()
    ? `Auto · level SP ${fmt("SP_LevelTarget")} · temp SP ${fmt("SP_TempTarget")}`
    : "Manual control";

  // KPI cards
  const kpis = $("#kpis");
  if (!kpis.childElementCount) {
    for (const name of KPI_TAGS.filter((n) => S.tags[n])) {
      kpis.append(el("div", { class: "kpi", "data-kpi": name },
        el("span", { class: "kpi-name", text: S.tags[name].description }),
        el("span", { class: "kpi-val" }),
        el("canvas", { height: 26 })));
    }
  }
  for (const card of $$(".kpi", kpis)) {
    const name = card.dataset.kpi;
    const alarm = tagAlarm(name);
    card.className = "kpi" + (alarm ? ` alarm-${alarm.priority}` : "");
    const val = $(".kpi-val", card);
    val.textContent = fmt(name, undefined, false);
    val.append(el("small", { text: S.tags[name].unit }));
    sparkline($("canvas", card), name);
  }

  // Alarm list
  const list = $("#mini-alarms");
  list.replaceChildren();
  const shown = S.alarms.slice(0, 6);
  if (!shown.length) list.append(el("li", { class: "empty-li", text: "No active alarms" }));
  for (const a of shown) {
    list.append(el("li", {},
      prioIcon(a.priority),
      el("span", { text: `${a.message}${a.active ? "" : " — cleared"}`, style: a.acked ? "" : "font-weight:600" }),
      el("span", { class: "muted mono", style: "margin-left:auto;font-size:12px", text: timeStr(a.raised_at) })));
  }
}

function prioIcon(priority) {
  const letter = { high: "1", medium: "2", low: "3" }[priority] || "?";
  return el("span", { class: `prio ${priority}`, title: `${priority} priority`, text: letter });
}

function sparkline(canvas, name) {
  const w = canvas.clientWidth || 200, h = 26, dpr = window.devicePixelRatio || 1;
  if (canvas.width !== Math.round(w * dpr)) { canvas.width = Math.round(w * dpr); canvas.height = h * dpr; }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  const data = (S.history[name] || []).slice(-120);
  if (data.length < 2) return;
  const vals = data.map((d) => d[1]);
  let lo = Math.min(...vals), hi = Math.max(...vals);
  if (hi - lo < 1e-6) { lo -= 1; hi += 1; }
  ctx.strokeStyle = cssVar("--equip-stroke");
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  data.forEach(([, v], i) => {
    const x = (i / (data.length - 1)) * w, y = h - 3 - ((v - lo) / (hi - lo)) * (h - 6);
    i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
  });
  ctx.stroke();
}
const cssVar = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();

/* --- I/O */
function buildIO() {
  $("#sim-note").textContent = S.config.simulated_inputs ? "Simulator: use “Sim” to force inputs" : "";
  for (const kind of ["DI", "DO", "AI", "AO"]) {
    const table = $(`#tbl-${kind}`);
    table.replaceChildren();
    for (const tag of Object.values(S.tags).filter((t) => t.kind === kind)) {
      const action = el("td", { class: "io-act" });
      if (kind === "DI" && S.config.simulated_inputs) {
        action.append(el("button", { class: "btn small sim-btn", text: "Sim", title: "Toggle this input in the simulator",
          onclick: () => simulateInput(tag.name) }));
      } else if (tag.writable && tag.type === "bool") {
        action.append(el("button", { class: "switch-btn", role: "switch", "aria-label": tag.description,
          "data-switch": tag.name, onclick: () => writeTag(tag.name, !S.values[tag.name]) }));
      } else if (tag.writable) {
        action.append(el("button", { class: "btn small", text: "Set…", "data-set": tag.name,
          onclick: () => promptAnalog(tag.name) }));
      }
      const indicator = tag.type === "bool" ? el("span", { class: "lamp" }) : analogBar(tag.name);
      table.append(el("tr", { "data-row": tag.name },
        el("td", { style: "width:1%" }, indicator),
        el("td", {}, el("div", { class: "io-desc", text: tag.description }), el("div", { class: "io-name", text: tag.name })),
        el("td", { class: "io-val" }),
        action));
    }
  }
}

function analogBar(name) {
  const bar = el("div", { class: "bar", style: "width:110px" }, el("div", { class: "bar-fill" }));
  for (const cond of ["lo", "hi", "hihi"]) {
    if (S.tags[name].alarm?.[cond] !== undefined) bar.append(el("div", { class: "bar-mark", "data-limit": cond }));
  }
  return bar;
}

function renderIO() {
  for (const row of $$("[data-row]")) {
    const name = row.dataset.row, tag = S.tags[name], v = S.values[name];
    const alarm = tagAlarm(name);
    row.classList.toggle("in-alarm", !!alarm);
    $(".io-val", row).textContent = fmt(name);
    const lamp = $(".lamp", row);
    if (lamp) lamp.className = "lamp" + (alarm ? " alarm" : v ? " on" : "");
    const bar = $(".bar", row);
    if (bar) {
      $(".bar-fill", bar).style.width = pct(name) + "%";
      for (const mark of $$("[data-limit]", bar)) {
        const lim = alarmLimit(name, mark.dataset.limit);
        mark.style.left = `calc(${pct(name, lim)}% - 1px)`;
      }
    }
    const sw = $("[data-switch]", row);
    if (sw) {
      sw.setAttribute("aria-checked", String(!!v));
      const locked = tag.manual_only && isAuto();
      sw.disabled = locked;
      sw.title = locked ? "Controlled by PLC in Auto — switch to Manual" : "";
    }
    const set = $("[data-set]", row);
    if (set) set.disabled = tag.manual_only && isAuto();
  }
}

async function promptAnalog(name) {
  const tag = S.tags[name];
  const raw = prompt(`${tag.description} (${tag.min}–${tag.max} ${tag.unit})`, fmt(name, undefined, false));
  if (raw === null) return;
  const value = Number(raw);
  if (!Number.isFinite(value) || value < tag.min || value > tag.max) {
    toast(`Enter a number between ${tag.min} and ${tag.max}`, "error");
    return;
  }
  await writeTag(name, value);
}

async function simulateInput(name) {
  try {
    await api("POST", `/api/simulate/${encodeURIComponent(name)}`, { value: !S.values[name] });
  } catch (err) { toast(err.message, "error"); }
}

/* --- setpoints & manual control */
const MANUAL_ORDER = ["DO_PumpRun", "AO_PumpSpeed", "DO_HeaterOn", "AO_OutletValve", "DO_DrainValve"];

function buildSetpoints() {
  const spGrid = $("#sp-grid");
  spGrid.replaceChildren(...Object.values(S.tags)
    .filter((t) => t.kind === "SP" && t.type === "float")
    .map((t) => spCard(t)));
  const manual = Object.values(S.tags).filter((t) => t.writable && (t.kind === "DO" || t.kind === "AO"));
  const rank = (t) => (MANUAL_ORDER.includes(t.name) ? MANUAL_ORDER.indexOf(t.name) : MANUAL_ORDER.length);
  manual.sort((a, b) => rank(a) - rank(b));
  $("#manual-grid").replaceChildren(...manual.map((t) => (t.type === "bool" ? boolCard(t) : spCard(t))));
  for (const btn of $$("#mode-switch button")) {
    btn.onclick = () => {
      const wantAuto = btn.dataset.mode === "true";
      if (wantAuto !== isAuto()) writeTag("SP_AutoMode", wantAuto);
    };
  }
}

// Which process value a setpoint relates to, so the operator sees both side by side.
const RELATED_PV = { SP_LevelTarget: "AI_TankLevel", SP_LevelHiAlarm: "AI_TankLevel",
  SP_TempTarget: "AI_TankTemp", SP_TempHiAlarm: "AI_TankTemp", AO_PumpSpeed: "AI_InletFlow", AO_OutletValve: "AI_OutletFlow" };

function spCard(tag) {
  const num = el("input", { type: "number", min: tag.min, max: tag.max, step: 10 ** -(tag.decimals ?? 1),
    "aria-label": `New ${tag.description}` });
  const range = el("input", { type: "range", min: tag.min, max: tag.max, step: 10 ** -(tag.decimals ?? 1),
    "aria-label": `${tag.description} slider` });
  const apply = el("button", { class: "btn primary small", text: "Apply" });
  const reset = el("button", { class: "btn small", text: "Reset", title: "Discard the pending value" });
  const card = el("div", { class: "sp-card", "data-sp": tag.name },
    el("div", { class: "sp-top" }, el("span", { class: "sp-title", text: tag.description }), el("span", { class: "sp-tag", text: tag.name })),
    el("div", { class: "sp-top" }, el("span", { class: "sp-live" }), el("span", { class: "sp-pv" })),
    el("div", { class: "sp-edit" }, num, range),
    el("div", { class: "sp-top" },
      el("span", { class: "sp-range", text: `Range ${tag.min} – ${tag.max} ${tag.unit}` }),
      el("span", { class: "sp-edit" }, reset, apply)),
    el("div", { class: "sp-lock", hidden: true, text: "Controlled by PLC in Auto — switch to Manual to adjust" }));

  const markDirty = (v) => { card.dataset.dirty = "1"; card.classList.add("pending"); num.value = v; range.value = v; };
  const clean = () => { delete card.dataset.dirty; card.classList.remove("pending"); syncInputs(card, tag); };
  num.addEventListener("input", () => markDirty(num.value));
  range.addEventListener("input", () => markDirty(range.value));
  reset.onclick = clean;
  const submit = async () => {
    if (!num.checkValidity() || num.value === "") { toast(`Enter a value between ${tag.min} and ${tag.max}`, "error"); return; }
    if (await writeTag(tag.name, Number(num.value))) clean();
  };
  apply.onclick = submit;
  num.addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); if (e.key === "Escape") clean(); });
  return card;
}

function boolCard(tag) {
  const sw = el("button", { class: "switch-btn", role: "switch", "aria-label": tag.description,
    onclick: () => writeTag(tag.name, !S.values[tag.name]) });
  return el("div", { class: "sp-card", "data-sp": tag.name, "data-bool": "1" },
    el("div", { class: "sp-top" }, el("span", { class: "sp-title", text: tag.description }), el("span", { class: "sp-tag", text: tag.name })),
    el("div", { class: "sp-top" }, el("span", { class: "sp-live" }), sw),
    el("div", { class: "sp-pv" }),
    el("div", { class: "sp-lock", hidden: true, text: "Controlled by PLC in Auto — switch to Manual to operate" }));
}

function syncInputs(card, tag) {
  const v = S.values[tag.name];
  if (v == null) return;
  const text = Number(v).toFixed(tag.decimals ?? 1);
  for (const input of $$("input", card)) input.value = text;
}

function renderSetpoints() {
  for (const btn of $$("#mode-switch button")) btn.setAttribute("aria-checked", String((btn.dataset.mode === "true") === isAuto()));
  $("#manual-note").textContent = isAuto() ? "— locked items are driven by the PLC in Auto" : "";
  for (const card of $$("[data-sp]")) {
    const name = card.dataset.sp, tag = S.tags[name];
    const locked = tag.manual_only && isAuto();
    card.classList.toggle("locked", locked);
    $(".sp-lock", card).hidden = !locked;
    const live = $(".sp-live", card);
    live.textContent = fmt(name, undefined, false);
    if (tag.type !== "bool") live.append(el("small", { text: tag.unit }));
    if (card.dataset.bool) {
      const sw = $(".switch-btn", card);
      sw.setAttribute("aria-checked", String(!!S.values[name]));
      sw.disabled = locked;
      const fb = name === "DO_PumpRun" ? `Feedback: ${S.values.DI_PumpRunning ? "running" : "stopped"}` : "";
      $(".sp-pv", card).textContent = fb;
      continue;
    }
    const pv = RELATED_PV[name];
    $(".sp-pv", card).textContent = pv ? `${S.tags[pv].description}: ${fmt(pv)}` : "";
    for (const c of $$("input, button", card)) c.disabled = locked;
    if (!card.dataset.dirty && !card.contains(document.activeElement)) syncInputs(card, tag);
  }
}

/* --- trends */
function buildTrendPicker() {
  const picker = $("#trend-picker");
  picker.replaceChildren();
  const candidates = Object.values(S.tags).filter((t) => t.type === "float");
  candidates.forEach((t, i) => {
    t.color = PEN_COLORS[i % PEN_COLORS.length];
    const btn = el("button", { class: "pen", "data-pen": t.name, style: `--pen:${t.color}`,
      "aria-pressed": String(S.pens.has(t.name)), title: t.name },
    el("i"), el("span", { text: t.description }), el("span", { class: "pen-val" }));
    btn.onclick = () => {
      S.pens.has(t.name) ? S.pens.delete(t.name) : S.pens.add(t.name);
      btn.setAttribute("aria-pressed", String(S.pens.has(t.name)));
      renderTrend();
    };
    picker.append(btn);
  });
  for (const b of $$("#trend-window button")) {
    b.onclick = () => {
      S.trendWindow = Number(b.dataset.window);
      $$("#trend-window button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
      renderTrend();
    };
  }
  const canvas = $("#trend");
  canvas.addEventListener("mousemove", (e) => { S.hoverX = e.offsetX; renderTrend(); });
  canvas.addEventListener("mouseleave", () => { S.hoverX = null; renderTrend(); });
}

function renderTrend() {
  for (const pen of $$("[data-pen]")) $(".pen-val", pen).textContent = fmt(pen.dataset.pen);
  const canvas = $("#trend");
  const w = canvas.clientWidth, h = 380, dpr = window.devicePixelRatio || 1;
  if (!w) return;
  if (canvas.width !== Math.round(w * dpr)) { canvas.width = Math.round(w * dpr); canvas.height = h * dpr; }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);

  const pad = { l: 44, r: 12, t: 12, b: 26 };
  const pw = w - pad.l - pad.r, ph = h - pad.t - pad.b;
  const tEnd = S.lastHistoryT || Date.now() / 1000, tStart = tEnd - S.trendWindow;
  const xFor = (t) => pad.l + ((t - tStart) / S.trendWindow) * pw;
  const yFor = (p) => pad.t + ph * (1 - p / 100);

  ctx.font = `11px ${cssVar("--mono") || "monospace"}`;
  ctx.fillStyle = cssVar("--muted");
  ctx.strokeStyle = cssVar("--line");
  ctx.lineWidth = 1;
  for (let p = 0; p <= 100; p += 25) {
    const y = Math.round(yFor(p)) + 0.5;
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(w - pad.r, y); ctx.stroke();
    ctx.textAlign = "right"; ctx.fillText(`${p}%`, pad.l - 6, y + 4);
  }
  const step = S.trendWindow <= 60 ? 10 : S.trendWindow <= 300 ? 60 : 180;
  ctx.textAlign = "center";
  for (let t = Math.ceil(tStart / step) * step; t <= tEnd; t += step) {
    const x = Math.round(xFor(t)) + 0.5;
    ctx.beginPath(); ctx.moveTo(x, pad.t); ctx.lineTo(x, pad.t + ph); ctx.stroke();
    ctx.fillText(timeStr(t).slice(0, 5 + (step < 60 ? 3 : 0)), x, h - 8);
  }

  ctx.save();
  ctx.beginPath(); ctx.rect(pad.l, pad.t, pw, ph); ctx.clip();
  for (const name of S.pens) {
    const data = S.history[name];
    if (!data?.length) continue;
    ctx.strokeStyle = S.tags[name].color;
    ctx.lineWidth = 2;
    ctx.setLineDash(S.tags[name].kind === "SP" ? [6, 4] : []);
    ctx.beginPath();
    let started = false;
    for (const [t, v] of data) {
      if (t < tStart - 2) continue;
      const x = xFor(t), y = yFor(pct(name, v));
      started ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
      started = true;
    }
    ctx.stroke();
  }
  ctx.restore();
  ctx.setLineDash([]);

  // Hover cursor + tooltip.
  const tip = $("#trend-tip");
  if (S.hoverX == null || S.hoverX < pad.l || S.hoverX > w - pad.r) { tip.hidden = true; return; }
  const tHover = tStart + ((S.hoverX - pad.l) / pw) * S.trendWindow;
  ctx.strokeStyle = cssVar("--muted");
  ctx.beginPath(); ctx.moveTo(S.hoverX + 0.5, pad.t); ctx.lineTo(S.hoverX + 0.5, pad.t + ph); ctx.stroke();
  const rows = [el("div", {}, el("span", { class: "muted", text: timeStr(tHover) }))];
  for (const name of S.pens) {
    const near = nearest(S.history[name] || [], tHover);
    if (!near) continue;
    ctx.fillStyle = S.tags[name].color;
    ctx.beginPath(); ctx.arc(xFor(near[0]), yFor(pct(name, near[1])), 3.5, 0, Math.PI * 2); ctx.fill();
    rows.push(el("div", {}, el("span", { style: `color:${S.tags[name].color}`, text: S.tags[name].description }),
      el("b", { text: fmt(name, near[1]) })));
  }
  tip.replaceChildren(...rows);
  tip.hidden = false;
  const left = S.hoverX + 14 + tip.offsetWidth > w ? S.hoverX - tip.offsetWidth - 14 : S.hoverX + 14;
  tip.style.left = left + "px";
}

function nearest(data, t) {
  let lo = 0, hi = data.length - 1;
  if (hi < 0) return null;
  while (lo < hi) { const mid = (lo + hi) >> 1; data[mid][0] < t ? (lo = mid + 1) : (hi = mid); }
  const cand = [data[lo], data[lo - 1]].filter(Boolean);
  const best = cand.sort((a, b) => Math.abs(a[0] - t) - Math.abs(b[0] - t))[0];
  return Math.abs(best[0] - t) < 5 ? best : null;
}

/* --- alarms & events */
function renderAlarms() {
  const body = $("#alarm-table tbody");
  body.replaceChildren(...S.alarms.map((a) => {
    const tag = S.tags[a.tag];
    const state = a.active ? (a.acked ? "Active · acknowledged" : "Active · unacknowledged") : "Cleared · unacknowledged";
    return el("tr", { class: a.acked ? "" : "unacked" },
      el("td", {}, prioIcon(a.priority)),
      el("td", { class: "time", text: timeStr(a.raised_at) }),
      el("td", {}, el("div", { text: a.message }), el("div", { class: "io-name", text: a.tag })),
      el("td", { class: "mono", text: fmt(a.tag, a.value) }),
      el("td", { class: "mono", text: a.limit == null ? "—" : fmt(a.tag, a.limit) }),
      el("td", { class: "state", text: state }),
      el("td", { class: "io-act" }, a.acked ? null :
        el("button", { class: "btn small", text: "Ack", onclick: () => ack(a.id) })));
  }));
  $("#alarm-empty").hidden = S.alarms.length > 0;
  $("#ack-all").disabled = !S.alarms.some((a) => !a.acked);

  const filter = S.eventFilter;
  const kinds = { alarm: ["alarm", "clear", "ack"], write: ["write", "sim"], system: ["system"] };
  const events = S.events.filter((e) => !filter || kinds[filter].includes(e.kind)).slice(-300).reverse();
  const tbody = $("#event-table tbody");
  if (tbody.dataset.sig === `${filter}:${S.lastSeq}`) return; // nothing new
  tbody.dataset.sig = `${filter}:${S.lastSeq}`;
  tbody.replaceChildren(...events.map((e) => el("tr", {},
    el("td", { class: "time", text: new Date(e.t * 1000).toLocaleString([], { hour12: false }) }),
    el("td", {}, el("span", { class: `ev-kind ${e.kind}`, text: e.kind })),
    el("td", { text: e.message }),
    el("td", { class: "muted", text: e.user || "" }))));
}

async function ack(id) {
  try { await api("POST", "/api/alarms/ack", { id }); } catch (err) { toast(err.message, "error"); }
}

/* ------------------------------------------------------------- startup */
function selectTab(tab) {
  S.tab = tab;
  for (const b of $$(".tabs button")) b.setAttribute("aria-selected", String(b.dataset.tab === tab));
  for (const v of $$(".view")) v.hidden = v.id !== `view-${tab}`;
  history.replaceState(null, "", `#${tab}`);
  renderAll();
}

function initTheme() {
  let theme = null;
  try { theme = localStorage.getItem("hmi-theme"); } catch { /* storage unavailable */ }
  if (theme) document.documentElement.dataset.theme = theme;
  $("#theme-btn").onclick = () => {
    const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("hmi-theme", next); } catch { /* ignore */ }
    renderAll();
  };
}

async function init() {
  initTheme();
  setInterval(() => { $("#clock").textContent = new Date().toLocaleTimeString([], { hour12: false }); }, 1000);
  S.config = await api("GET", "/api/config");
  for (const t of S.config.tags) S.tags[t.name] = t;
  $("#plant-name").textContent = S.config.plant.name || "Plant HMI";
  $("#plant-desc").textContent = S.config.plant.description || "";
  document.title = `${S.config.plant.name || "Plant"} · HMI`;
  S.pens = new Set([...S.pens].filter((n) => S.tags[n]));

  const hist = await api("GET", "/api/history");
  for (const [name, points] of Object.entries(hist)) S.history[name] = points;
  S.lastHistoryT = Math.max(0, ...Object.values(hist).map((p) => (p.length ? p[p.length - 1][0] : 0)));

  buildIO();
  buildSetpoints();
  buildTrendPicker();

  for (const b of $$(".tabs button")) b.onclick = () => selectTab(b.dataset.tab);
  for (const b of $$("[data-goto]")) b.onclick = () => selectTab(b.dataset.goto);
  $("#alarm-banner").onclick = () => selectTab("alarms");
  $("#ack-all").onclick = () => ack(null);
  for (const b of $$("#event-filter button")) {
    b.onclick = () => {
      S.eventFilter = b.dataset.filter;
      $$("#event-filter button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
      renderAlarms();
    };
  }
  window.addEventListener("resize", () => renderAll());
  const fromHash = location.hash.slice(1);
  selectTab($(`.tabs [data-tab="${fromHash}"]`) ? fromHash : "overview");
  connect();
}

init().catch((err) => toast(`Failed to load HMI: ${err.message}`, "error"));
