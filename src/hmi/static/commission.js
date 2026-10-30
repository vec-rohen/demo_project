"use strict";
/* Commissioning tab: device & package, PLC connection test, I/O checkout and
   on-site I/O edits. Uses the helpers and state defined in app.js. */

const C = { plc: null, tags: [], checks: {}, filter: "", built: false };
const CHECK_LABEL = { pass: "Pass", fail: "Fail", na: "N/A" };

function reloadWithMessage(message) {
  try { sessionStorage.setItem("hmi-flash", message); } catch { /* ignore */ }
  location.reload();
}

function initCommissioning() {
  for (const node of $$(".integrator-only")) node.hidden = !hasRole("integrator");
  $("#plc-protocol").onchange = syncPlcFields;
  $("#plc-test").onclick = testPlc;
  $("#plc-save").onclick = savePlc;
  $("#io-report").onclick = exportReport;
  $("#add-tag").onclick = () => openTagDialog(null);
  $("#changes-btn").onclick = openChanges;
  $("#cd-close").onclick = () => $("#changes-dialog").close();
  $("#download-package").onclick = () => { location.href = "/api/package"; };
  $("#package-file").onchange = loadPackageFile;
  for (const b of $$("#io-filter button")) {
    b.onclick = () => {
      C.filter = b.dataset.iofilter;
      $$("#io-filter button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
      buildIOTable();
    };
  }
  initTagDialog();
}

async function loadCommission() {
  C.built = true;
  try {
    const [site, plc, io] = await Promise.all([
      api("GET", "/api/site"), api("GET", `${A()}/plc`), api("GET", `${A()}/io`)]);
    S.site = site;
    C.plc = plc;
    C.tags = io.tags;
    C.checks = io.checks;
  } catch (err) {
    C.built = false;
    toast(err.message, "error");
    return;
  }
  renderDeviceFacts();
  fillPlcForm(C.plc);
  buildIOTable();
}

function renderCommission() {
  if (!C.built) { loadCommission(); return; }
  $("#plc-live").textContent = S.connected ? "● PLC online" : "● PLC offline";
  $("#plc-live").className = S.connected ? "live-ok" : "live-bad";
  for (const cell of $$("[data-live]")) cell.textContent = fmt(cell.dataset.live);
}

/* --- device & package */
function renderDeviceFacts() {
  const { client, site, device, package: pkg } = S.site;
  const when = (t) => (t ? new Date(t * 1000).toLocaleString([], { hour12: false }) : "—");
  const facts = [
    ["Client", client ? `${client.name} (${client.id})` : "—"],
    ["Site", site ? `${site.name} (${site.id})` : "—"],
    ["Device", device ? `${device.name} (${device.id})` : "—"],
    ["Assets", S.site.assets.map((a) => a.name).join(", ")],
    ["Package", pkg ? `v${pkg.version} · loaded ${when(pkg.applied_at)} by ${pkg.applied_by} (${pkg.source})` : "—"],
  ];
  $("#device-facts").replaceChildren(...facts.flatMap(([k, v]) => [el("dt", { text: k }), el("dd", { text: v })]));
  const n = S.site.pending_changes;
  $("#changes-count").textContent = n;
  $("#changes-count").title = `${n} change${n === 1 ? "" : "s"} made here not yet synced to the cloud`;
}

async function loadPackageFile(e) {
  const file = e.target.files[0];
  e.target.value = "";
  if (!file) return;
  let pkg;
  try {
    pkg = JSON.parse(await file.text());
  } catch {
    toast("That file isn't a valid configuration package (not JSON).", "error");
    return;
  }
  const assets = (pkg.assets || []).map((a) => a.name).join(", ") || "none";
  const ok = await confirmChange("Load this configuration package?",
    `<b>${esc(pkg.client?.name ?? "?")} · ${esc(pkg.site?.name ?? "?")}</b><br>` +
    `Package v${esc(pkg.package_version ?? "?")} · assets: ${esc(assets)}<br><br>` +
    `<span class="muted">This replaces the device's assets, PLC settings, I/O${pkg.users ? " and user accounts" : ""}. ` +
    "Everyone is signed out. Download the current package first if you might need it back.</span>");
  if (!ok) return;
  try {
    const res = await api("POST", "/api/package", pkg);
    try { sessionStorage.setItem("hmi-flash", `Package v${res.package.version} loaded`); } catch { /* ignore */ }
    location.replace("/login?signedout=1");
  } catch (err) { toast(err.message, "error"); }
}

async function openChanges() {
  let changes;
  try { changes = await api("GET", "/api/changes"); } catch (err) { toast(err.message, "error"); return; }
  const describe = (c) => {
    if (c.kind === "plc") return `PLC → ${c.after.protocol === "modbus" ? `${c.after.host}:${c.after.port} unit ${c.after.unit}` : c.after.protocol}, poll ${c.after.poll_ms} ms`;
    if (c.kind === "tag" && !c.after) return "Deleted";
    if (c.kind === "tag" && !c.before) return "Added";
    if (c.kind === "tag") {
      const keys = new Set([...Object.keys(c.before), ...Object.keys(c.after)]);
      const changed = [...keys].filter((k) => JSON.stringify(c.before[k]) !== JSON.stringify(c.after[k]));
      return changed.map((k) => `${k}: ${JSON.stringify(c.before[k] ?? "—")} → ${JSON.stringify(c.after[k] ?? "—")}`).join("; ");
    }
    if (c.kind === "user.password") return "Password changed";
    if (c.kind === "user") return !c.after ? "Account deleted" : !c.before ? `Account created (${c.after.role})` : "Account updated";
    return c.kind;
  };
  const asset = (id) => S.site.assets.find((a) => a.id === id)?.name || id || "";
  $("#changes-table tbody").replaceChildren(...changes.slice().reverse().map((c) => el("tr", {},
    el("td", { class: "time", text: new Date(c.t * 1000).toLocaleString([], { hour12: false }) }),
    el("td", { text: c.user }),
    el("td", {}, el("div", { text: c.target }), el("div", { class: "muted", text: asset(c.asset_id) })),
    el("td", { class: "change-desc", text: describe(c) }),
    el("td", { text: c.synced ? "Yes" : "Pending" }))));
  if (!changes.length) $("#changes-table tbody").append(el("tr", {}, el("td", { colspan: 5, class: "muted", text: "No changes made on this device." })));
  $("#changes-dialog").showModal();
}

/* --- PLC connection */
function fillPlcForm(plc) {
  $("#plc-protocol").value = plc.protocol;
  $("#plc-host").value = plc.host ?? "";
  $("#plc-port").value = plc.port ?? 502;
  $("#plc-unit").value = plc.unit ?? 1;
  $("#plc-poll").value = plc.poll_ms ?? 500;
  const editable = hasRole("integrator");
  for (const input of $$("#plc-form input, #plc-form select")) input.disabled = !editable;
  syncPlcFields();
}

function syncPlcFields() {
  const modbus = $("#plc-protocol").value === "modbus";
  for (const f of $$("#plc-form .modbus-only")) f.hidden = !modbus;
}

function readPlcForm() {
  const plc = { protocol: $("#plc-protocol").value, poll_ms: Number($("#plc-poll").value) };
  if (plc.protocol === "modbus") {
    plc.host = $("#plc-host").value.trim();
    plc.port = Number($("#plc-port").value);
    plc.unit = Number($("#plc-unit").value);
  }
  return plc;
}

async function testPlc() {
  const box = $("#plc-result"), btn = $("#plc-test");
  btn.disabled = true;
  btn.textContent = "Testing…";
  box.hidden = false;
  box.className = "test-result";
  box.textContent = "Connecting to the PLC…";
  try {
    const r = await api("POST", `${A()}/plc/test`, readPlcForm());
    box.className = `test-result ${r.ok ? "ok" : "bad"}`;
    const head = r.ok
      ? `✓ Connected · read ${r.tags_read} of ${r.tags_total} points in ${r.latency_ms} ms`
      : `✗ Connection failed${r.tags_read ? ` · read ${r.tags_read} of ${r.tags_total} points` : ""}`;
    box.replaceChildren(el("b", { text: head }),
      ...(r.errors.length ? [el("ul", {}, ...r.errors.map((e) => el("li", { text: e })))] : []));
  } catch (err) {
    box.className = "test-result bad";
    box.textContent = err.message;
  } finally {
    btn.disabled = false;
    btn.textContent = "Test connection";
  }
}

async function savePlc() {
  const plc = readPlcForm();
  const target = plc.protocol === "modbus" ? `${plc.host}:${plc.port} (unit ${plc.unit})` : "the simulator";
  const ok = await confirmChange("Apply new PLC connection?",
    `Connect to <b>${esc(target)}</b>, polling every ${esc(plc.poll_ms)} ms.<br>` +
    "<span class=\"muted\">The asset restarts its connection straight away. The change is logged for cloud sync.</span>");
  if (!ok) return;
  try {
    await api("PUT", `${A()}/plc`, plc);
    toast("PLC connection saved");
    C.built = false;
    loadCommission();
  } catch (err) { toast(err.message, "error"); }
}

/* --- I/O checkout */
function formatAddress(tag) {
  const m = tag.modbus;
  if (!m) return "—";
  const prefix = { coil: "0x", discrete: "1x", input: "3x", holding: "4x" }[m.table] || m.table;
  return `${prefix} ${m.address}${m.scale && m.scale !== 1 ? ` ÷${m.scale}` : ""}`;
}

function checkCounts() {
  const counts = { pass: 0, fail: 0, na: 0, open: 0 };
  for (const t of C.tags) counts[C.checks[t.name]?.status || "open"]++;
  return counts;
}

function renderProgress() {
  const c = checkCounts(), total = C.tags.length || 1;
  const seg = (cls, n) => el("span", { class: `seg ${cls}`, style: `width:${(n / total) * 100}%` });
  $("#io-progress").replaceChildren(
    el("div", { class: "bar-track" }, seg("pass", c.pass), seg("fail", c.fail), seg("na", c.na)),
    el("div", { class: "progress-text",
      text: `${c.pass + c.fail + c.na} of ${C.tags.length} checked · ${c.pass} passed · ${c.fail} failed · ${c.na} N/A` }));
}

function buildIOTable() {
  renderProgress();
  const integrator = hasRole("integrator");
  const rows = C.tags.filter((t) => {
    const status = C.checks[t.name]?.status;
    return !C.filter || (C.filter === "open" ? !status : status === C.filter);
  });
  $("#io-check-table tbody").replaceChildren(...rows.map((t) => {
    const check = C.checks[t.name];
    const statusBtns = el("div", { class: "segmented small check-btns", role: "group", "aria-label": `Check ${t.name}` },
      ...Object.entries(CHECK_LABEL).map(([status, label]) => el("button", {
        class: `chk-${status}`, text: label, "aria-pressed": String(check?.status === status),
        onclick: () => markCheck(t, check?.status === status ? null : status),
      })));
    const by = check
      ? el("div", {}, el("div", { text: `${check.user} · ${new Date(check.t * 1000).toLocaleString([], { hour12: false })}` }),
        check.note ? el("div", { class: "muted", text: check.note }) : null)
      : el("span", { class: "muted", text: "—" });
    return el("tr", { class: check ? `checked-${check.status}` : "" },
      el("td", { class: "c-point" }, el("div", { class: "io-desc", text: t.description || t.name }),
        el("div", { class: "io-name", text: t.name })),
      el("td", { class: "c-type mono", text: `${t.kind}${t.writable ? " · R/W" : ""}` }),
      el("td", { class: "c-addr mono", text: formatAddress(t) }),
      el("td", { class: "c-live io-val", "data-live": t.name, text: fmt(t.name) }),
      el("td", { class: "c-check" }, statusBtns),
      el("td", { class: "c-by small-cell" }, by),
      el("td", { class: "c-edit io-act" }, integrator ? el("button", { class: "btn small", text: "Edit", onclick: () => openTagDialog(t) }) : null));
  }));
  if (!rows.length) {
    $("#io-check-table tbody").append(el("tr", {}, el("td", { colspan: 7, class: "muted",
      text: C.filter ? "Nothing in this filter." : "No I/O points defined for this asset yet." })));
  }
}

async function markCheck(tag, status) {
  let note = C.checks[tag.name]?.note || "";
  if (status === "fail" || status === "na") {
    const answer = await askText(status === "fail" ? `What's wrong with ${tag.name}?` : `Why is ${tag.name} not applicable?`,
      status === "fail" ? "e.g. reads double, wired to wrong terminal" : "e.g. spare, not installed", note);
    if (answer === null) return;
    note = answer;
  }
  try {
    const res = await api("PUT", `${A()}/io/${encodeURIComponent(tag.name)}/check`, { status, note });
    if (status) C.checks[tag.name] = res; else delete C.checks[tag.name];
    buildIOTable();
  } catch (err) { toast(err.message, "error"); }
}

// Small text-entry dialog (built once) – used for check notes.
function askText(title, placeholder, initial = "") {
  let dlg = $("#ask-dialog");
  if (!dlg) {
    dlg = el("dialog", { id: "ask-dialog" },
      el("form", { method: "dialog" },
        el("h3", { id: "ask-title" }),
        el("input", { id: "ask-input", autocomplete: "off", maxlength: 500 }),
        el("div", { class: "dialog-actions" },
          el("button", { class: "btn", value: "cancel", type: "submit", formnovalidate: true, text: "Cancel" }),
          el("button", { class: "btn primary", value: "ok", type: "submit", text: "Save" }))));
    document.body.append(dlg);
  }
  $("#ask-title").textContent = title;
  $("#ask-input").placeholder = placeholder;
  $("#ask-input").value = initial;
  dlg.returnValue = "";
  dlg.showModal();
  $("#ask-input").focus();
  return new Promise((resolve) => dlg.addEventListener("close",
    () => resolve(dlg.returnValue === "ok" ? $("#ask-input").value.trim() : null), { once: true }));
}

function exportReport() {
  const { client, site, device } = S.site;
  const asset = S.site.assets.find((a) => a.id === S.assetId);
  const q = (v) => `"${String(v ?? "").replace(/"/g, '""')}"`;
  const lines = [
    ["I/O commissioning report"], ["Client", client?.name], ["Site", site?.name], ["Device", device?.id],
    ["Asset", asset?.name], ["Generated", new Date().toLocaleString()], ["By", S.me.username], [],
    ["Tag", "Description", "Kind", "Address", "Value at export", "Result", "Note", "Checked by", "Checked at"],
    ...C.tags.map((t) => {
      const c = C.checks[t.name];
      return [t.name, t.description, t.kind, formatAddress(t), fmt(t.name), c ? CHECK_LABEL[c.status] : "Not checked",
        c?.note, c?.user, c ? new Date(c.t * 1000).toLocaleString() : ""];
    }),
  ];
  const csv = lines.map((row) => row.map(q).join(",")).join("\r\n");
  const link = el("a", {
    href: URL.createObjectURL(new Blob(["﻿" + csv], { type: "text/csv" })),
    download: `IO-check_${site?.id || "site"}_${S.assetId}_${new Date().toISOString().slice(0, 10)}.csv`,
  });
  document.body.append(link);
  link.click();
  link.remove();
}

/* --- edit / add an I/O point (integrators) */
let editingTag = null;

function initTagDialog() {
  const dlg = $("#tag-dialog");
  $("#td-cancel").onclick = () => dlg.close();
  $("#td-type").onchange = syncTagFields;
  $("#td-kind").onchange = () => {
    // Default the value type from the kind; setpoints can be either.
    const k = $("#td-kind").value;
    if (k === "DI" || k === "DO") $("#td-type").value = "bool";
    if (k === "AI" || k === "AO") $("#td-type").value = "float";
    syncTagFields();
  };
  $("#td-delete").onclick = deleteTag;
  $("#tag-form").addEventListener("submit", (e) => { e.preventDefault(); saveTag(); });
}

function syncTagFields() {
  const analog = $("#td-type").value === "float";
  for (const f of $$("#tag-dialog .analog-only")) f.hidden = !analog;
  for (const f of $$("#tag-dialog .digital-only")) f.hidden = analog;
}

function openTagDialog(tag) {
  editingTag = tag;
  const t = tag || { name: "", kind: "AI", type: "float", decimals: 1, role: "operator" };
  const set = (id, v) => { $(id).value = v ?? ""; };
  $("#td-title").textContent = tag ? `Edit ${tag.name}` : "Add I/O point";
  set("#td-name", t.name);
  $("#td-name").readOnly = !!tag;
  set("#td-description", t.description);
  set("#td-kind", t.kind);
  set("#td-type", t.type);
  set("#td-unit", t.unit);
  set("#td-decimals", t.decimals ?? 1);
  set("#td-min", t.min);
  set("#td-max", t.max);
  set("#td-role", t.role || "operator");
  $("#td-writable").checked = !!t.writable;
  set("#td-table", t.modbus?.table);
  set("#td-address", t.modbus?.address);
  set("#td-scale", t.modbus?.scale ?? 1);
  const al = t.alarm || {};
  set("#td-alarm-state", t.type === "bool" && "state" in al ? String(al.state) : "");
  set("#td-alarm-priority", al.priority || "medium");
  set("#td-alarm-message", al.message);
  for (const k of ["hihi", "hi", "lo", "lolo"]) set(`#td-alarm-${k}`, t.type === "float" ? al[k] : "");
  set("#td-alarm-deadband", al.deadband);
  $("#td-delete").hidden = !tag;
  $("#td-error").hidden = true;
  syncTagFields();
  $("#tag-dialog").showModal();
  (tag ? $("#td-description") : $("#td-name")).focus();
}

function readTagForm() {
  const num = (id) => ($(id).value.trim() === "" ? undefined : Number($(id).value));
  const type = $("#td-type").value;
  const def = { ...(editingTag || {}) };
  Object.assign(def, {
    name: $("#td-name").value.trim(),
    description: $("#td-description").value.trim(),
    kind: $("#td-kind").value,
    type,
    role: $("#td-role").value,
    writable: $("#td-writable").checked,
  });
  for (const key of ["unit", "min", "max", "decimals", "modbus", "alarm"]) delete def[key];
  if (type === "float") {
    if ($("#td-unit").value.trim()) def.unit = $("#td-unit").value.trim();
    def.min = num("#td-min");
    def.max = num("#td-max");
    def.decimals = num("#td-decimals") ?? 1;
  }
  const table = $("#td-table").value;
  if (table) {
    def.modbus = { table, address: num("#td-address") };
    if (type === "float") def.modbus.scale = num("#td-scale") ?? 1;
  }
  if (type === "bool") {
    const state = $("#td-alarm-state").value;
    if (state) {
      def.alarm = { state: state === "true", priority: $("#td-alarm-priority").value,
        message: $("#td-alarm-message").value.trim() || `${def.description || def.name} alarm` };
    }
  } else {
    const alarm = {};
    for (const k of ["hihi", "hi", "lo", "lolo"]) {
      const raw = $(`#td-alarm-${k}`).value.trim();
      if (raw) alarm[k] = Number.isFinite(Number(raw)) ? Number(raw) : raw;
    }
    if (Object.keys(alarm).length) {
      const db = num("#td-alarm-deadband");
      if (db !== undefined) alarm.deadband = db;
      def.alarm = alarm;
    }
  }
  for (const [k, v] of Object.entries(def)) if (v === undefined) delete def[k];
  return def;
}

async function saveTag() {
  const def = readTagForm();
  const err = $("#td-error");
  if (!def.name) { err.textContent = "Give the point a tag name."; err.hidden = false; return; }
  if (def.modbus && !Number.isInteger(def.modbus.address)) {
    err.textContent = "Enter the Modbus address, or set the register type to “Not mapped”.";
    err.hidden = false;
    return;
  }
  if (!editingTag && C.tags.some((t) => t.name === def.name)) {
    err.textContent = `A point called ${def.name} already exists.`;
    err.hidden = false;
    return;
  }
  try {
    await api("PUT", `${A()}/tags/${encodeURIComponent(def.name)}/definition`, def);
  } catch (ex) {
    err.textContent = ex.message;
    err.hidden = false;
    return;
  }
  $("#tag-dialog").close();
  reloadWithMessage(`${def.name} ${editingTag ? "updated" : "added"} – re-check it in the I/O list`);
}

async function deleteTag() {
  const name = editingTag.name;
  $("#tag-dialog").close();
  const ok = await confirmChange(`Delete ${name}?`,
    "The point is removed from this asset's I/O, screens and trends. The change is logged for cloud sync.");
  if (!ok) { $("#tag-dialog").showModal(); return; }
  try {
    await api("DELETE", `${A()}/tags/${encodeURIComponent(name)}`, {});
    reloadWithMessage(`${name} deleted`);
  } catch (err) { toast(err.message, "error"); }
}
