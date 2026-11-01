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
  const when = (t) => (t ? fmtDateTime(t) : "—");
  const facts = [
    ["Client", client ? `${client.name} (${client.id})` : "—"],
    ["Site", site ? `${site.name} (${site.id})` : "—"],
    ["Device", device ? `${device.name} (${device.id})` : "—"],
    ["Assets", S.site.assets.map((a) => a.name).join(", ")],
    ["Package", pkg ? `v${pkg.version} · loaded ${when(pkg.applied_at)} by ${pkg.applied_by} (${pkg.source})` : "—"],
    ["Clock", `${fmtDateTime(S.site.server_time)} ${tzName()} · ` + (S.site.clock_synced === true ? "synchronised (NTP)"
      : S.site.clock_synced === false ? "⚠ NOT synchronised – enable NTP on the device" : "sync status unknown")],
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
    if (c.kind === "plc") return `PLC → ${c.after.protocol === "sim" ? "simulator" : `${c.after.protocol} ${c.after.host}:${c.after.port}`}, poll ${c.after.poll_ms} ms`;
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
    el("td", { class: "time", text: fmtDateTime(c.t) }),
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
  $("#plc-port").value = plc.port ?? (plc.protocol === "s7" ? 102 : 502);
  $("#plc-unit").value = plc.unit ?? 1;
  $("#plc-rack").value = plc.rack ?? 0;
  $("#plc-slot").value = plc.slot ?? 1;
  $("#plc-poll").value = plc.poll_ms ?? 500;
  const editable = hasRole("integrator");
  for (const input of $$("#plc-form input, #plc-form select")) input.disabled = !editable;
  syncPlcFields();
}

function syncPlcFields() {
  const protocol = $("#plc-protocol").value;
  for (const f of $$("#plc-form .modbus-only")) f.hidden = protocol !== "modbus";
  for (const f of $$("#plc-form .s7-only")) f.hidden = protocol !== "s7";
  for (const f of $$("#plc-form .net-only")) f.hidden = protocol === "sim";
  // Suggest the protocol's standard port when switching.
  const port = $("#plc-port");
  if (protocol === "s7" && port.value === "502") port.value = 102;
  if (protocol === "modbus" && port.value === "102") port.value = 502;
}

function readPlcForm() {
  const plc = { protocol: $("#plc-protocol").value, poll_ms: Number($("#plc-poll").value) };
  if (plc.protocol !== "sim") {
    plc.host = $("#plc-host").value.trim();
    plc.port = Number($("#plc-port").value);
  }
  if (plc.protocol === "modbus") plc.unit = Number($("#plc-unit").value);
  if (plc.protocol === "s7") Object.assign(plc, { rack: Number($("#plc-rack").value), slot: Number($("#plc-slot").value) });
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
  const target = plc.protocol === "modbus" ? `${plc.host}:${plc.port} (unit ${plc.unit})`
    : plc.protocol === "s7" ? `S7 PLC ${plc.host} (rack ${plc.rack}, slot ${plc.slot})` : "the simulator";
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
const formatAddress = (tag) => TagForm.address(tag, C.plc?.protocol);

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
      ? el("div", {}, el("div", { text: `${check.user} · ${fmtDateTime(check.t)}` }),
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
    ["Asset", asset?.name], ["Generated", `${fmtDateTime(Date.now() / 1000)} (${tzName()})`], ["By", S.me.username], [],
    ["Tag", "Description", "Kind", "Address", "Value at export", "Result", "Note", "Checked by", "Checked at"],
    ...C.tags.map((t) => {
      const c = C.checks[t.name];
      return [t.name, t.description, t.kind, formatAddress(t), fmt(t.name), c ? CHECK_LABEL[c.status] : "Not checked",
        c?.note, c?.user, c ? fmtDateTime(c.t) : ""];
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
function openTagDialog(tag) {
  TagForm.open(tag, {
    protocol: C.plc?.protocol === "s7" ? "s7" : "modbus",
    exists: (name) => C.tags.some((t) => t.name === name),
    save: async (def, isNew) => {
      await api("PUT", `${A()}/tags/${encodeURIComponent(def.name)}/definition`, def);
      reloadWithMessage(`${def.name} ${isNew ? "added" : "updated"} – re-check it in the I/O list`);
    },
    remove: async (name) => {
      await api("DELETE", `${A()}/tags/${encodeURIComponent(name)}`, {});
      reloadWithMessage(`${name} deleted`);
    },
  });
}
