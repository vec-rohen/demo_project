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
  me: null,            // signed-in user
  site: null,          // client / site / device / assets on this gateway
  assetId: null,       // the asset shown on every tab
  longHistory: {},     // trend data from the device database for windows > 15 min
};
// Every asset-specific API call and the live feed are scoped to the selected asset.
const A = () => `/api/assets/${encodeURIComponent(S.assetId)}`;
const hasRole = (role) => !!S.me && ROLE_ORDER.indexOf(S.me.role) >= ROLE_ORDER.indexOf(role);
const ROLE_ORDER = ["viewer", "operator", "engineer", "integrator"];
const HISTORY_SECONDS = 900;
const PEN_COLORS = ["#4e9be6", "#e8875a", "#5fbf8f", "#c77dd6", "#d9b44a", "#6cc7d9", "#e46a8a", "#9aa0a8"];
const DEMO_KPIS = ["AI_TankLevel", "AI_TankTemp", "AI_InletFlow", "AI_OutletFlow", "AI_PumpPressure", "AI_MotorCurrent"];
const isDemoScreen = () => S.config?.asset?.screen === "tank-demo";
// The demo tank has a hand-drawn mimic; any other asset gets an automatic overview.
const kpiTags = () => (isDemoScreen() ? DEMO_KPIS : Object.values(S.tags).filter((t) => t.kind === "AI").map((t) => t.name))
  .filter((n) => S.tags[n]);

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

/* Mirrors the server's checks so controls are greyed out with a reason; the server still enforces them. */
const can = (role) => !!S.me && !S.me.must_change_password &&
  ROLE_ORDER.indexOf(S.me.role) >= ROLE_ORDER.indexOf(role);
function lockReason(tag) {
  const need = tag.role || "operator";
  if (S.me?.must_change_password) return "Change your password to make changes";
  if (!can(need)) return `${roleName(need)} role required`;
  if (tag.manual_only && isAuto()) return "Controlled by PLC in Auto — switch to Manual to adjust";
  return null;
}

function tagAlarm(name) {
  // Highest-priority *active* alarm on a tag, or null.
  return S.alarms.find((a) => a.tag === name && a.active) || null;
}
function alarmLimit(name, cond) {
  const spec = S.tags[name]?.alarm?.[cond];
  if (spec === undefined) return null;
  return typeof spec === "string" ? S.values[spec] : spec;
}

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
    const res = await api("POST", `${A()}/tags/${encodeURIComponent(name)}`, { value });
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
  const ws = new WebSocket(`${proto}://${location.host}/ws/${encodeURIComponent(S.assetId)}`);
  let retry = 1000;
  ws.onopen = () => { retry = 1000; };
  ws.onmessage = (msg) => applyUpdate(JSON.parse(msg.data));
  ws.onclose = (ev) => {
    if (ev.code === 4401) { location.replace("/login?expired=1"); return; }
    if (ev.code === 4404) { location.replace("/"); return; } // asset no longer on this device
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
  if (S.tab === "users") renderUsers();
  if (S.tab === "commission") renderCommission();
}

function renderStatus() {
  const chip = $("#conn-chip");
  chip.className = "chip " + (S.connected ? "ok" : "bad");
  $("#conn-text").textContent = S.connected === null ? "HMI server offline" : S.connected ? "PLC online" : "PLC comms lost";
  const mode = $("#mode-chip");
  mode.hidden = !S.tags.SP_AutoMode; // only assets with an auto/manual mode show it
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
  $("#mimic-panel").hidden = !isDemoScreen();
  $("#status-panel").hidden = isDemoScreen();
  if (!isDemoScreen()) renderStatusPanel();
  else renderMimic(v);
  renderKpisAndAlarms();
}

// Automatic overview for any asset: every digital point as a status lamp.
function renderStatusPanel() {
  const grid = $("#status-grid");
  const digital = Object.values(S.tags).filter((t) => t.type === "bool");
  if (grid.childElementCount !== digital.length) {
    grid.replaceChildren(...digital.map((t) => el("div", { class: "status-item", "data-status": t.name },
      el("span", { class: "lamp" }),
      el("span", {}, el("div", { class: "io-desc", text: t.description || t.name }),
        el("div", { class: "io-name", text: t.name })),
      el("span", { class: "io-val" }))));
    if (!digital.length) grid.append(el("p", { class: "muted", text: "This asset has no digital points." }));
  }
  for (const item of $$("[data-status]", grid)) {
    const name = item.dataset.status, alarm = tagAlarm(name);
    $(".lamp", item).className = "lamp" + (alarm ? " alarm" : S.values[name] ? " on" : "");
    $(".io-val", item).textContent = fmt(name);
  }
}

function renderMimic(v) {
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
}

function renderKpisAndAlarms() {
  // KPI cards
  const kpis = $("#kpis");
  if (!kpis.childElementCount) {
    for (const name of kpiTags()) {
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
      // Disabled buttons swallow taps and touch screens can't show tooltips,
      // so a tap on a locked control explains why it's locked.
      const action = el("td", { class: "io-act", onclick: () => {
        const btn = $("button", action);
        if (btn?.disabled && btn.title) toast(btn.title, "error");
      } });
      if (kind === "DI" && S.config.simulated_inputs) {
        action.append(el("button", { class: "btn small sim-btn", text: "Sim", disabled: !can("engineer"),
          title: can("engineer") ? "Toggle this input in the simulator" : "Engineer role required",
          onclick: () => simulateInput(tag.name) }));
      } else if (tag.writable && tag.type === "bool") {
        action.append(el("button", { class: "switch-btn", role: "switch", "aria-label": tag.description,
          "data-switch": tag.name, onclick: () => writeTag(tag.name, !S.values[tag.name]) }));
      } else if (tag.writable) {
        action.append(el("button", { class: "btn small", text: "Set…", "data-set": tag.name,
          onclick: () => openValueDialog(tag.name) }));
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
  const bar = el("div", { class: "bar io-bar" }, el("div", { class: "bar-fill" }));
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
      const reason = lockReason(tag);
      sw.disabled = !!reason;
      sw.title = reason || "";
    }
    const set = $("[data-set]", row);
    if (set) { const reason = lockReason(tag); set.disabled = !!reason; set.title = reason || ""; }
  }
}

// Edit one analog value in a dialog (used by the I/O page's "Set…" buttons).
function openValueDialog(name) {
  const tag = S.tags[name];
  const dlg = $("#value-dialog");
  const num = $("#vd-number"), range = $("#vd-range");
  const step = 10 ** -(tag.decimals ?? 1);
  for (const input of [num, range]) { input.min = tag.min; input.max = tag.max; input.step = step; }
  const current = Number(S.values[name] ?? tag.min).toFixed(tag.decimals ?? 1);
  num.value = range.value = current;
  dlg.dataset.tag = name;
  $("#vd-title").textContent = tag.description;
  $("#vd-tag").textContent = name;
  $("#vd-range-text").textContent = `Range ${tag.min} – ${tag.max} ${tag.unit}`;
  $("#vd-unit").textContent = tag.unit;
  $("#vd-error").hidden = true;
  const preview = () => { $("#vd-change").textContent = `${fmt(name)} → ${fmt(name, Number(num.value))}`; };
  num.oninput = () => { range.value = num.value; preview(); };
  range.oninput = () => { num.value = range.value; preview(); };
  preview();
  dlg.showModal();
}

function initValueDialog() {
  const dlg = $("#value-dialog");
  $("#vd-cancel").onclick = () => dlg.close();
  $("#vd-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const name = dlg.dataset.tag, tag = S.tags[name];
    const value = Number($("#vd-number").value);
    if ($("#vd-number").value === "" || !Number.isFinite(value) || value < tag.min || value > tag.max) {
      $("#vd-error").textContent = `Enter a number between ${tag.min} and ${tag.max}.`;
      $("#vd-error").hidden = false;
      return;
    }
    if (await writeTag(name, value, { confirm: false })) dlg.close();
  });
}

async function simulateInput(name) {
  try {
    await api("POST", `${A()}/simulate/${encodeURIComponent(name)}`, { value: !S.values[name] });
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
  const num = el("input", { type: "number", inputmode: "decimal", min: tag.min, max: tag.max, step: 10 ** -(tag.decimals ?? 1),
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
    el("div", { class: "sp-lock", hidden: true }));

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
    el("div", { class: "sp-lock", hidden: true }));
}

function syncInputs(card, tag) {
  const v = S.values[tag.name];
  if (v == null) return;
  const text = Number(v).toFixed(tag.decimals ?? 1);
  for (const input of $$("input", card)) input.value = text;
}

function renderSetpoints() {
  $(".mode-panel").hidden = !S.tags.SP_AutoMode;
  const modeReason = lockReason(S.tags.SP_AutoMode || {});
  for (const btn of $$("#mode-switch button")) {
    btn.setAttribute("aria-checked", String((btn.dataset.mode === "true") === isAuto()));
    btn.disabled = !!modeReason;
    btn.title = modeReason || "";
  }
  $("#manual-note").textContent = isAuto() ? "— locked items are driven by the PLC in Auto" : "";
  for (const card of $$("[data-sp]")) {
    const name = card.dataset.sp, tag = S.tags[name];
    const reason = lockReason(tag);
    const locked = !!reason;
    card.classList.toggle("locked", locked);
    // Viewers already see a read-only banner; don't repeat it on every card.
    $(".sp-lock", card).hidden = !locked || S.me.role === "viewer";
    $(".sp-lock", card).textContent = reason || "";
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
      loadLongHistory();
      renderTrend();
    };
    picker.append(btn);
  });
  for (const b of $$("#trend-window button")) {
    b.onclick = () => {
      S.trendWindow = Number(b.dataset.window);
      $$("#trend-window button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
      loadLongHistory();
      renderTrend();
    };
  }
  const canvas = $("#trend");
  // Pointer events cover mouse, pen and touch: hover with a mouse, or tap and
  // drag sideways with a finger to scrub (vertical swipes still scroll the page).
  const scrub = (e) => { S.hoverX = e.offsetX; renderTrend(); };
  canvas.addEventListener("pointermove", scrub);
  canvas.addEventListener("pointerdown", scrub);
  canvas.addEventListener("pointerleave", (e) => { if (e.pointerType === "mouse") { S.hoverX = null; renderTrend(); } });
  document.addEventListener("pointerdown", (e) => {
    if (e.target !== canvas && S.hoverX !== null) { S.hoverX = null; renderTrend(); }
  });
}

const LIVE_WINDOW = 900; // seconds kept in the browser; longer windows come from the device database
function trendData(name) {
  const live = S.history[name] || [];
  if (S.trendWindow <= LIVE_WINDOW) return live;
  // Long windows: averaged points from the device database, then the live 1 s data for the newest part.
  const cutoff = live.length ? live[0][0] : Infinity;
  return [...(S.longHistory[name] || []).filter((p) => p[0] < cutoff), ...live];
}

async function loadLongHistory() {
  if (S.trendWindow <= LIVE_WINDOW || !S.pens.size) return;
  try {
    const tags = [...S.pens].map(encodeURIComponent).join(",");
    S.longHistory = await api("GET", `${A()}/history?tags=${tags}&seconds=${S.trendWindow}`);
    S.longHistory.loadedAt = Date.now();
    renderTrend();
  } catch (err) { toast(err.message, "error"); }
}

function timeAxis(window) {
  // [grid step in seconds, label formatter]
  if (window <= 60) return [10, (t) => timeStr(t)];
  if (window <= 900) return [window <= 300 ? 60 : 180, (t) => timeStr(t).slice(0, 5)];
  if (window <= 3600) return [600, (t) => timeStr(t).slice(0, 5)];
  if (window <= 86400) return [3 * 3600, (t) => timeStr(t).slice(0, 5)];
  return [86400, (t) => new Date(t * 1000).toLocaleDateString([], { day: "numeric", month: "short" })];
}

function renderTrend() {
  if (S.trendWindow > LIVE_WINDOW && S.tab === "trends" && Date.now() - (S.longHistory.loadedAt || 0) > 30000) {
    S.longHistory.loadedAt = Date.now(); // refresh long trends every 30 s while visible
    loadLongHistory();
  }
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
  const tEnd = Math.max(S.lastHistoryT || 0, Date.now() / 1000 - 1), tStart = tEnd - S.trendWindow;
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
  const [step, label] = timeAxis(S.trendWindow);
  const tz = new Date().getTimezoneOffset() * 60; // align day/hour grid lines to local time
  ctx.textAlign = "center";
  for (let t = Math.ceil((tStart - tz) / step) * step + tz; t <= tEnd; t += step) {
    const x = Math.round(xFor(t)) + 0.5;
    ctx.beginPath(); ctx.moveTo(x, pad.t); ctx.lineTo(x, pad.t + ph); ctx.stroke();
    ctx.fillText(label(t), x, h - 8);
  }

  ctx.save();
  ctx.beginPath(); ctx.rect(pad.l, pad.t, pw, ph); ctx.clip();
  for (const name of S.pens) {
    const data = trendData(name);
    if (!data.length) continue;
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
    const near = nearest(trendData(name), tHover, S.trendWindow / 100);
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

function nearest(data, t, tolerance = 5) {
  let lo = 0, hi = data.length - 1;
  if (hi < 0) return null;
  while (lo < hi) { const mid = (lo + hi) >> 1; data[mid][0] < t ? (lo = mid + 1) : (hi = mid); }
  const cand = [data[lo], data[lo - 1]].filter(Boolean);
  const best = cand.sort((a, b) => Math.abs(a[0] - t) - Math.abs(b[0] - t))[0];
  return Math.abs(best[0] - t) < Math.max(5, tolerance) ? best : null;
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
        el("button", { class: "btn small", text: "Ack", disabled: !can("operator"),
          title: can("operator") ? "" : "Operator role required", onclick: () => ack(a.id) })));
  }));
  $("#alarm-empty").hidden = S.alarms.length > 0;
  $("#ack-all").disabled = !can("operator") || !S.alarms.some((a) => !a.acked);

  const filter = S.eventFilter;
  const kinds = { alarm: ["alarm", "clear", "ack"], write: ["write", "sim"], config: ["config", "commission"],
    auth: ["auth"], system: ["system"] };
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
  try { await api("POST", `${A()}/alarms/ack`, { id }); } catch (err) { toast(err.message, "error"); }
}

/* ------------------------------------------------------------- account */
function initAccount() {
  const me = S.me;
  $("#user-name").textContent = me.full_name || me.username;
  $("#user-avatar").textContent = (me.full_name || me.username).split(/\s+/).map((w) => w[0]).join("").slice(0, 2);
  const role = $("#user-role");
  role.textContent = me.role;
  role.className = `role-tag ${me.role}`;
  $("#menu-head").replaceChildren(el("b", { text: me.full_name || me.username }),
    el("span", { text: `${me.username} · ${roleName(me.role)}` }));
  $("#users-tab").hidden = !hasRole("engineer");
  $("#commission-tab").hidden = !hasRole("engineer");
  const strip = $("#readonly-strip");
  strip.hidden = me.role !== "viewer";
  strip.textContent = "You're signed in as a viewer: you can monitor everything but can't make changes.";

  const btn = $("#user-btn"), menu = $("#user-menu");
  const close = () => { menu.hidden = true; btn.setAttribute("aria-expanded", "false"); };
  btn.onclick = (e) => {
    e.stopPropagation();
    menu.hidden = !menu.hidden;
    btn.setAttribute("aria-expanded", String(!menu.hidden));
    if (!menu.hidden) $("button", menu).focus();
  };
  document.addEventListener("click", (e) => { if (!menu.contains(e.target)) close(); });
  menu.addEventListener("keydown", (e) => { if (e.key === "Escape") { close(); btn.focus(); } });
  $("#menu-password").onclick = () => { close(); openPasswordDialog(false); };
  $("#menu-signout").onclick = signOut;

  initPasswordDialog();
  if (me.must_change_password) openPasswordDialog(true);
  trackActivity();
}

async function signOut() {
  try { await api("POST", "/api/logout", {}); } catch { /* already signed out */ }
  location.replace("/login?signedout=1");
}

function initPasswordDialog() {
  const dlg = $("#pw-dialog");
  $("#pw-cancel").onclick = () => dlg.close();
  $("#pw-signout").onclick = signOut;
  dlg.addEventListener("cancel", (e) => { if (dlg.dataset.forced) e.preventDefault(); });
  $("#pw-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const err = $("#pw-error");
    const current = $("#pw-current").value, next = $("#pw-new").value;
    const show = (msg) => { err.textContent = msg; err.hidden = false; };
    if (next.length < 8) return show("The new password must be at least 8 characters.");
    if (next !== $("#pw-repeat").value) return show("The new passwords don't match.");
    try {
      await api("POST", "/api/me/password", { current, new: next });
    } catch (ex) {
      return show(ex.message);
    }
    S.me.must_change_password = false;
    delete dlg.dataset.forced;
    dlg.close();
    toast("Password changed");
    renderAll();
    if (S.config) { buildIO(); renderAll(); }
  });
}

function openPasswordDialog(forced) {
  const dlg = $("#pw-dialog");
  $("#pw-form").reset();
  $("#pw-error").hidden = true;
  $("#pw-form [name=username]").value = S.me.username;
  $("#pw-title").textContent = forced ? "Choose a new password" : "Change password";
  $("#pw-intro").textContent = forced
    ? "Your password was set by an administrator. Choose your own before you continue."
    : "";
  $("#pw-intro").hidden = !forced;
  $("#pw-cancel").hidden = forced;
  $("#pw-signout").hidden = !forced;
  if (forced) dlg.dataset.forced = "1"; else delete dlg.dataset.forced;
  dlg.showModal();
  $("#pw-current").focus();
}

// The server signs a session out after a period without interaction. Tell it
// when the operator is active, and warn shortly before the timeout.
function trackActivity() {
  const timeout = S.me.idle_timeout;
  if (!timeout) return;
  let last = Date.now(), lastPing = Date.now(), warned = false;
  const onActivity = () => {
    last = Date.now();
    warned = false;
    if (Date.now() - lastPing > 60_000) {
      lastPing = Date.now();
      api("POST", "/api/me/ping", {}).catch(() => {});
    }
  };
  for (const ev of ["pointerdown", "keydown", "wheel", "touchstart"]) {
    window.addEventListener(ev, onActivity, { passive: true });
  }
  setInterval(() => {
    const left = timeout * 1000 - (Date.now() - last);
    if (left < 60_000 && !warned) {
      warned = true;
      toast("You'll be signed out in about a minute due to inactivity. Click anywhere to stay signed in.", "error");
    }
  }, 5000);
}

/* --- user administration (engineers) */
let usersCache = [];

async function renderUsers() {
  if (renderUsers.busy) return;
  // Only refetch occasionally: this runs on every live-data update.
  if (renderUsers.fetched && Date.now() - renderUsers.fetched < 15000) return;
  renderUsers.busy = true;
  try {
    usersCache = await api("GET", "/api/users");
    renderUsers.fetched = Date.now();
    drawUsers();
  } catch (err) {
    toast(err.message, "error");
  } finally {
    renderUsers.busy = false;
  }
}
const refreshUsers = () => { renderUsers.fetched = 0; return renderUsers(); };

function drawUsers() {
  const body = $("#users-table tbody");
  body.replaceChildren(...usersCache.map((u) => {
    const self = u.username === S.me.username;
    // You can only manage accounts at or below your own role, and only grant those roles.
    const locked = !hasRole(u.role);
    const lockTitle = locked ? `Only an ${u.role} can change this account` : "";
    const roleSel = el("select", { "aria-label": `Role for ${u.username}`, disabled: self || locked,
      title: self ? "You can't change your own role" : lockTitle },
    ...ROLE_ORDER.filter((r) => hasRole(r) || r === u.role)
      .map((r) => el("option", { value: r, text: roleName(r), selected: r === u.role })));
    roleSel.onchange = async () => {
      const ok = await confirmChange(`Change role for ${u.username}?`,
        `<span class="change">${esc(roleName(u.role))} → ${esc(roleName(roleSel.value))}</span><br>` +
        `<span class="muted">They will be signed out so the change takes effect.</span>`);
      if (!ok) { roleSel.value = u.role; return; }
      await userAction("PATCH", u.username, { role: roleSel.value }, `${u.username} is now ${roleName(roleSel.value)}`);
    };
    const status = u.disabled ? el("span", { class: "status-tag off", text: "Disabled" })
      : u.must_change_password ? el("span", { class: "status-tag pending", text: "Password change pending" })
        : el("span", { class: "status-tag", text: "Active" });
    const actions = el("td", { class: "actions" },
      el("button", { class: "btn small", text: "Reset password", disabled: locked, title: lockTitle,
        onclick: () => openUserDialog(u) }),
      el("button", { class: "btn small", text: u.disabled ? "Enable" : "Disable", disabled: self || locked,
        onclick: async () => {
          const verb = u.disabled ? "Enable" : "Disable";
          if (!(await confirmChange(`${verb} ${u.username}?`, u.disabled
            ? "They will be able to sign in again."
            : "They will be signed out immediately and won't be able to sign in."))) return;
          await userAction("PATCH", u.username, { disabled: !u.disabled }, `${u.username} ${verb.toLowerCase()}d`);
        } }),
      el("button", { class: "btn small danger", text: "Delete", disabled: self || locked,
        onclick: async () => {
          if (!(await confirmChange(`Delete ${u.username}?`,
            "This removes the account permanently. Their past actions stay in the event journal."))) return;
          await userAction("DELETE", u.username, undefined, `${u.username} deleted`);
        } }));
    return el("tr", {},
      el("td", {}, el("div", {}, el("b", { text: u.username }), self ? el("span", { class: "you", text: "(you)" }) : null),
        el("div", { class: "muted", text: u.full_name || "" })),
      el("td", {}, roleSel),
      el("td", {}, status),
      el("td", { class: "time", text: u.last_login ? new Date(u.last_login * 1000).toLocaleString([], { hour12: false }) : "Never" }),
      actions);
  }));
}

async function userAction(method, username, body, message) {
  try {
    await api(method, `/api/users/${encodeURIComponent(username)}`, body ?? {});
    toast(message);
  } catch (err) {
    toast(err.message, "error");
  }
  await refreshUsers();
}


function openUserDialog(existing) {
  const dlg = $("#user-dialog");
  $("#user-form").reset();
  $("#ud-error").hidden = true;
  dlg.dataset.username = existing ? existing.username : "";
  $("#ud-title").textContent = existing ? `Reset password for ${existing.username}` : "Add user";
  $("#ud-submit").textContent = existing ? "Reset password" : "Create user";
  $("#ud-new-fields").hidden = !!existing;
  $("#ud-password").value = randomPassword();
  dlg.showModal();
  (existing ? $("#ud-password") : $("#ud-username")).focus();
}

function initUserAdmin() {
  const dlg = $("#user-dialog");
  if (hasRole("integrator")) {
    $("#ud-role").append(el("option", { value: "integrator", text: "Integrator – commissioning & configuration" }));
  }
  $("#add-user-btn").onclick = () => openUserDialog(null);
  $("#ud-cancel").onclick = () => dlg.close();
  $("#ud-generate").onclick = () => { $("#ud-password").value = randomPassword(); };
  $("#user-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const err = $("#ud-error");
    const password = $("#ud-password").value;
    const existing = dlg.dataset.username;
    const show = (msg) => { err.textContent = msg; err.hidden = false; };
    if (password.length < 8) return show("The password must be at least 8 characters.");
    try {
      if (existing) {
        await api("PATCH", `/api/users/${encodeURIComponent(existing)}`, { password });
        toast(`Password reset for ${existing}. They'll choose a new one at next sign-in.`);
      } else {
        const username = $("#ud-username").value.trim().toLowerCase();
        if (!/^[a-z0-9_.-]{2,32}$/.test(username)) return show("Usernames are 2–32 lowercase letters, digits, dot, dash or underscore.");
        await api("POST", "/api/users", { username, password, role: $("#ud-role").value, full_name: $("#ud-name").value });
        toast(`User ${username} created`);
      }
    } catch (ex) {
      return show(ex.message);
    }
    dlg.close();
    refreshUsers();
  });
}

/* -------------------------------------------------------- site & assets */
function pickAsset() {
  const ids = S.site.assets.map((a) => a.id);
  const fromUrl = new URLSearchParams(location.search).get("asset");
  let saved = null;
  try { saved = localStorage.getItem("hmi-asset"); } catch { /* ignore */ }
  const id = [fromUrl, saved, ids[0]].find((x) => x && ids.includes(x)) || null;
  try { if (id) localStorage.setItem("hmi-asset", id); } catch { /* ignore */ }
  return id;
}

function renderSiteHeader() {
  const asset = S.site.assets.find((a) => a.id === S.assetId);
  const where = [S.site.client?.name, S.site.site?.name].filter(Boolean).join(" · ");
  $("#plant-name").textContent = asset?.name || "Plant HMI";
  $("#plant-desc").textContent = where || asset?.description || "";
  document.title = `${asset?.name || "Plant"} · HMI`;
  const picker = $("#asset-picker");
  picker.hidden = S.site.assets.length < 2;
  picker.replaceChildren(...S.site.assets.map((a) => el("option", {
    value: a.id, selected: a.id === S.assetId,
    text: `${a.name}${a.alarms_unacked ? `  (${a.alarms_unacked} alarm${a.alarms_unacked > 1 ? "s" : ""})` : ""}`,
  })));
  picker.onchange = () => {
    const url = new URL(location.href);
    url.searchParams.set("asset", picker.value);
    location.assign(url); // a fresh load keeps every view consistent with the new asset
  };
}

// Other assets' alarm counts in the picker stay current.
async function refreshSite() {
  try {
    S.site = await api("GET", "/api/site");
    if (S.site.assets.length > 1) renderSiteHeader();
  } catch { /* transient */ }
}

/* ------------------------------------------------------------- startup */
function selectTab(tab) {
  if (!$(`.tabs [data-tab="${tab}"]`) || $(`.tabs [data-tab="${tab}"]`).hidden) tab = "overview";
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
  S.me = await api("GET", "/api/me");
  initAccount();
  S.site = await api("GET", "/api/site");
  S.assetId = pickAsset();
  if (!S.assetId) { toast("No assets are configured on this device yet.", "error"); return; }
  S.config = await api("GET", `${A()}/config`);
  for (const t of S.config.tags) S.tags[t.name] = t;
  renderSiteHeader();
  if (!isDemoScreen()) S.pens = new Set(kpiTags().slice(0, 4));
  S.pens = new Set([...S.pens].filter((n) => S.tags[n]));

  const hist = await api("GET", `${A()}/history`);
  for (const [name, points] of Object.entries(hist)) S.history[name] = points;
  S.lastHistoryT = Math.max(0, ...Object.values(hist).map((p) => (p.length ? p[p.length - 1][0] : 0)));

  buildIO();
  buildSetpoints();
  buildTrendPicker();
  initValueDialog();
  if (hasRole("engineer")) { initUserAdmin(); initCommissioning(); }

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
  setInterval(refreshSite, 15000);
  let flash = null;
  try { flash = sessionStorage.getItem("hmi-flash"); sessionStorage.removeItem("hmi-flash"); } catch { /* ignore */ }
  if (flash) toast(flash);
}

init().catch((err) => toast(`Failed to load HMI: ${err.message}`, "error"));
