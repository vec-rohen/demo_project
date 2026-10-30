"use strict";
/* Cloud portal: set up clients, sites, IoT2050 devices, assets, I/O and users,
   then publish a configuration package per device. Helpers come from common.js. */

const P = { me: null };
const fmtTime = (t) => (t ? new Date(t * 1000).toLocaleString([], { hour12: false }) : "—");
const enc = encodeURIComponent;
const link = (href, text) => el("a", { href, text });
const isAdmin = () => P.me?.role === "admin";
const ROUTES = { "CL": "clients", "ST": "sites", "GW": "devices", "AS": "assets" };
const SITE_ROLES = [["", "No access"], ["viewer", "Viewer – read only"], ["operator", "Operator – run the plant"],
  ["engineer", "Engineer – limits, tuning, users"]];

/* ----------------------------------------------------------- page helpers */
function page(crumbs, title, { id, sub, actions = [] } = {}) {
  const main = $("#page");
  main.replaceChildren(
    crumbs.length ? el("div", { class: "crumbs" }, ...crumbs.flatMap(([href, text], i) =>
      [i ? el("span", { text: "›" }) : null, link(href, text)])) : null,
    el("div", { class: "page-head" },
      el("div", {}, el("h1", {}, title, id ? el("span", { class: "id-tag", text: id }) : null),
        sub ? el("div", { class: "sub", text: sub }) : null),
      el("div", { class: "btn-row tight" }, ...actions)));
  return main;
}

function panel(title, actions, ...content) {
  return el("section", { class: "panel" },
    el("div", { class: "panel-head" }, el("h2", { text: title }), el("div", { class: "btn-row tight" }, ...(actions || []))),
    ...content);
}

function table(headers, rows, empty = "Nothing here yet.") {
  return el("div", { class: "table-scroll" }, el("table", { class: "data-table" },
    el("thead", {}, el("tr", {}, ...headers.map((h) => el("th", { text: h })))),
    el("tbody", {}, ...(rows.length ? rows : [el("tr", { class: "empty-row" }, el("td", { colspan: headers.length, text: empty }))]))));
}

const button = (text, onclick, cls = "btn small") => el("button", { class: cls, text, onclick });
const row = (...cells) => el("tr", {}, ...cells.map((c) => (c instanceof Node && c.tagName === "TD" ? c : el("td", {}, c ?? ""))));

function deviceStatus(d) {
  if (!d.published) return el("span", { class: "pill warn", text: "Not published" });
  if (d.unpublished_changes) return el("span", { class: "pill warn", text: `v${d.published.version} · changes to publish` });
  return el("span", { class: "pill ok", text: `v${d.published.version} · up to date` });
}

function plcSummary(plc) {
  return plc.protocol === "modbus" ? `Modbus ${plc.host}:${plc.port} #${plc.unit}` : "Simulator";
}

async function act(fn, success) {
  try {
    const result = await fn();
    if (success) toast(success);
    return result;
  } catch (err) {
    toast(err.message, "error");
    return undefined;
  }
}

/* Generic form dialog. fields: {name, label, type, options, required, hint, value} */
function formDialog(title, fields, { submit = "Save", intro } = {}) {
  const dlg = el("dialog", { class: "form-dialog" });
  const inputs = {};
  const form = el("form", { novalidate: true }, el("h3", { text: title }), intro ? el("p", { class: "muted", text: intro }) : null);
  for (const f of fields) {
    const id = `fd-${f.name}`;
    let input;
    if (f.type === "select") {
      input = el("select", { id }, ...f.options.map(([v, label]) => el("option", { value: v, text: label, selected: String(f.value ?? "") === v })));
    } else if (f.type === "textarea") {
      input = el("textarea", { id, text: f.value ?? "" });
    } else {
      input = el("input", { id, type: f.type === "password" ? "text" : f.type || "text", value: f.value ?? "",
        autocomplete: "off", class: f.type === "password" ? "mono" : undefined,
        inputmode: f.type === "number" ? "numeric" : undefined });
    }
    inputs[f.name] = input;
    const control = f.type === "password" && f.generate
      ? el("div", { class: "pw-field" }, input, el("button", { type: "button", class: "pw-toggle", text: "Generate",
        onclick: () => { input.value = randomPassword(); } }))
      : input;
    const optional = !f.required && f.type !== "select";
    form.append(el("label", { for: id, text: f.label + (optional ? " (optional)" : "") }), control,
      f.hint ? el("p", { class: "hint", text: f.hint }) : null);
  }
  const error = el("p", { class: "form-error", role: "alert", hidden: true });
  form.append(error, el("div", { class: "dialog-actions" },
    el("button", { type: "button", class: "btn", text: "Cancel", onclick: () => dlg.close() }),
    el("button", { class: "btn primary", text: submit })));
  dlg.append(form);
  document.body.append(dlg);
  return new Promise((resolve) => {
    let result = null;
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const values = {};
      for (const f of fields) {
        let v = inputs[f.name].value.trim();
        if (f.required && !v) { error.textContent = `${f.label} is required.`; error.hidden = false; inputs[f.name].focus(); return; }
        if (f.type === "number") v = v === "" ? null : Number(v);
        values[f.name] = v;
      }
      result = values;
      dlg.close();
    });
    dlg.addEventListener("close", () => { dlg.remove(); resolve(result); }, { once: true });
    dlg.showModal();
    form.querySelector("input, select, textarea")?.focus();
  });
}

/* --------------------------------------------------------------- routing */
async function route() {
  const parts = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean).map(decodeURIComponent);
  const [section, id] = parts;
  const nav = section === "sites" || section === "assets" || section === "devices" ? "clients" : section || "dashboard";
  for (const a of $$("[data-nav]")) a.setAttribute("aria-current", a.dataset.nav === nav ? "page" : "false");
  $("#page").replaceChildren(el("p", { class: "muted", text: "Loading…" }));
  try {
    if (!section) await dashboardPage();
    else if (section === "clients" && id) await clientPage(id);
    else if (section === "clients") await clientsPage();
    else if (section === "sites") await sitePage(id);
    else if (section === "assets") await assetPage(id);
    else if (section === "devices") await devicePage(id);
    else if (section === "staff") await staffPage();
    else if (section === "audit") await auditPage();
    else $("#page").replaceChildren(el("p", { text: "Page not found." }));
  } catch (err) {
    $("#page").replaceChildren(el("p", { class: "form-error", text: err.message }));
  }
  $("#page").focus({ preventScroll: true });
}
const go = (hash) => { if (location.hash === hash) route(); else location.hash = hash; };

function targetLink(target) {
  const id = String(target || "").split("/")[0];
  const kind = ROUTES[id.slice(0, 2)];
  return kind ? link(`#/${kind}/${enc(id)}`, target) : target;
}

/* ------------------------------------------------------------- dashboard */
async function dashboardPage() {
  const o = await api("GET", "/api/overview");
  const main = page([], "Dashboard", { sub: `Signed in as ${P.me.full_name || P.me.username}` });
  main.append(
    el("div", { class: "cards" }, ...Object.entries(o.counts).map(([k, n]) =>
      el("a", { class: "stat", href: "#/clients", style: "text-decoration:none;color:inherit" }, el("b", { text: n }), el("span", { text: roleName(k) })))),
    el("div", { class: "two-col" },
      panel("Devices waiting to be published", [],
        o.pending.length ? el("ul", { class: "checklist" }, ...o.pending.map((d) =>
          el("li", { class: "todo" }, link(`#/devices/${enc(d.id)}`, `${d.name} (${d.id})`))))
          : el("p", { class: "muted", text: "Every device is up to date." })),
      panel("Recent activity", [link("#/audit", "All activity")], auditTable(o.recent, true))));
}

function auditTable(rows, compact = false) {
  return table(compact ? ["When", "Who", "What"] : ["When", "Who", "Action", "Target", "Detail"], rows.map((a) => compact
    ? row(el("td", { class: "time", text: fmtTime(a.t) }), a.user, el("span", {}, `${a.action} `, targetLink(a.target)))
    : row(el("td", { class: "time", text: fmtTime(a.t) }), a.user, a.action, targetLink(a.target), a.detail)), "No activity yet.");
}

async function auditPage() {
  const rows = await api("GET", "/api/audit");
  page([], "Activity log", { sub: "Every change made in the portal, newest first." }).append(panel("Activity", [], auditTable(rows)));
}

/* --------------------------------------------------------------- clients */
const CLIENT_FIELDS = (c = {}) => [
  { name: "name", label: "Client name", required: true, value: c.name },
  { name: "contact", label: "Contact person", value: c.contact },
  { name: "phone", label: "Phone", value: c.phone },
  { name: "email", label: "Email", type: "email", value: c.email },
  { name: "notes", label: "Notes", type: "textarea", value: c.notes },
];

async function clientsPage() {
  const clients = await api("GET", "/api/clients");
  page([], "Clients", { actions: [button("New client", newClient, "btn primary")] }).append(panel("All clients", [],
    table(["Client", "Sites", "Devices", "Assets", "Contact"], clients.map((c) => row(
      el("td", {}, link(`#/clients/${enc(c.id)}`, c.name), el("span", { class: "id-tag", text: c.id })),
      el("td", { class: "num", text: c.sites }), el("td", { class: "num", text: c.devices }),
      el("td", { class: "num", text: c.assets }), [c.contact, c.phone].filter(Boolean).join(" · "))),
    "No clients yet – start with “New client”.")));
}

async function newClient() {
  const values = await formDialog("New client", CLIENT_FIELDS(), { submit: "Create client" });
  if (!values) return;
  const c = await act(() => api("POST", "/api/clients", values), `Client ${values.name} created`);
  if (c) go(`#/clients/${enc(c.id)}`);
}

async function clientPage(id) {
  const c = await api("GET", `/api/clients/${enc(id)}`);
  const main = page([["#/clients", "Clients"]], c.name, {
    id: c.id, sub: [c.contact, c.phone, c.email].filter(Boolean).join(" · ") || "No contact details",
    actions: [
      button("Edit", async () => {
        const v = await formDialog(`Edit ${c.name}`, CLIENT_FIELDS(c));
        if (v && await act(() => api("PATCH", `/api/clients/${enc(id)}`, v), "Client updated")) route();
      }),
      button("Delete", async () => {
        if (await confirmChange(`Delete ${c.name}?`, "Only possible once it has no sites, devices or users.", "Delete")
            && await act(() => api("DELETE", `/api/clients/${enc(id)}`), "Client deleted")) go("#/clients");
      }, "btn small danger"),
    ],
  });
  const sites = panel("Sites", [button("Add site", () => newSite(c))],
    table(["Site", "Assets", "Devices", "Address"], c.sites.map((s) => row(
      el("td", {}, link(`#/sites/${enc(s.id)}`, s.name), el("span", { class: "id-tag", text: s.id })),
      el("td", { class: "num", text: s.assets }), el("td", { class: "num", text: s.devices }), s.address)), "No sites yet."));
  const devices = panel("Devices (IoT2050)", [button("Add device", () => newDevice(c))],
    table(["Device", "Site", "Assets", "Status"], c.devices.map((d) => row(
      el("td", {}, link(`#/devices/${enc(d.id)}`, d.name), el("span", { class: "id-tag", text: d.id })),
      c.sites.find((s) => s.id === d.site_id)?.name || el("span", { class: "muted", text: "Not assigned" }),
      el("td", { class: "num", text: d.assets.length }), deviceStatus(d))), "No devices yet."));
  main.append(el("div", { class: "two-col" }, sites, devices), el("div", { class: "stack" }, el("div", { style: "height:16px" }),
    panel("Client users", [button("Add user", () => newClientUser(c))], usersTable(c.users, c),
      el("p", { class: "muted small-print", text: "Client users sign in to the HMI on their sites' devices. Your staff are integrators on every device automatically." }))));
}

async function newSite(c) {
  const v = await formDialog(`New site for ${c.name}`, [
    { name: "name", label: "Site name", required: true },
    { name: "address", label: "Address" },
    { name: "timezone", label: "Time zone", value: "Australia/Brisbane" },
    { name: "history_days", label: "Trend history kept on the device (days)", type: "number", value: 7,
      hint: "Older data lives in the cloud." },
  ], { submit: "Create site" });
  if (!v) return;
  const s = await act(() => api("POST", `/api/clients/${enc(c.id)}/sites`, v), `Site ${v.name} created`);
  if (s) go(`#/sites/${enc(s.id)}`);
}

async function newDevice(c, siteId = "") {
  const v = await formDialog(`New device for ${c.name}`, [
    { name: "name", label: "Device name", required: true, hint: "e.g. IoT2050 – MCC panel" },
    { name: "serial", label: "Serial number" },
    { name: "site_id", label: "Site", type: "select", value: siteId,
      options: [["", "Not assigned yet"], ...c.sites.map((s) => [s.id, s.name])] },
    { name: "notes", label: "Notes", type: "textarea" },
  ], { submit: "Create device" });
  if (!v) return;
  const d = await act(() => api("POST", `/api/clients/${enc(c.id)}/devices`, v), `Device ${v.name} created`);
  if (d) go(`#/devices/${enc(d.id)}`);
}

/* ----------------------------------------------------------------- users */
function usersTable(users, client) {
  return table(["User", "Email", client ? "Sites & roles" : "Role", "Status", ""], users.map((u) => {
    const status = u.disabled ? el("span", { class: "pill bad", text: "Disabled" })
      : u.must_change_password ? el("span", { class: "pill warn", text: "Temporary password" })
        : el("span", { class: "pill ok", text: "Active" });
    const roles = client
      ? (u.access.length ? u.access.map((a) => `${a.site_name}: ${roleName(a.role)}`).join(", ") : el("span", { class: "muted", text: "No sites" }))
      : roleName(u.portal_role);
    const editable = client || isAdmin();
    const actions = el("td", { class: "io-act" },
      client ? button("Site access", () => editAccess(u, client)) : null,
      !client && isAdmin() && u.username !== P.me.username
        ? button("Role", () => changeStaffRole(u)) : null,
      editable ? button("Reset password", () => resetPassword(u)) : null,
      editable && u.username !== P.me.username ? button(u.disabled ? "Enable" : "Disable", async () => {
        if (await act(() => api("PATCH", `/api/users/${enc(u.username)}`, { disabled: !u.disabled }),
          `${u.username} ${u.disabled ? "enabled" : "disabled"}`)) route();
      }) : null,
      editable && u.username !== P.me.username ? button("Delete", async () => {
        if (await confirmChange(`Delete ${u.username}?`, "The account is removed from every device at the next publish.", "Delete")
            && await act(() => api("DELETE", `/api/users/${enc(u.username)}`), `${u.username} deleted`)) route();
      }, "btn small danger") : null);
    return row(el("td", {}, el("b", { text: u.username }), el("div", { class: "muted", text: u.full_name })),
      u.email, roles, status, actions);
  }), "No users yet.");
}

async function newClientUser(c) {
  const v = await formDialog(`New user for ${c.name}`, [
    { name: "username", label: "Username", required: true, hint: "Lowercase letters, digits, dot, dash or underscore." },
    { name: "full_name", label: "Full name" },
    { name: "email", label: "Email", type: "email" },
    { name: "password", label: "Temporary password", type: "password", generate: true, required: true, value: randomPassword(),
      hint: "Give this to the user. They choose their own the first time they sign in on site." },
  ], { submit: "Create user" });
  if (!v) return;
  const u = await act(() => api("POST", "/api/users", { ...v, kind: "client", client_id: c.id }), `User ${v.username} created`);
  if (u) { await editAccess(u, c); route(); }
}

async function editAccess(u, client) {
  const current = Object.fromEntries(u.access.map((a) => [a.site_id, a.role]));
  if (!client.sites.length) { toast("Add a site to this client first.", "error"); return; }
  const v = await formDialog(`Site access for ${u.username}`, client.sites.map((s) => (
    { name: s.id, label: s.name, type: "select", value: current[s.id] || "", options: SITE_ROLES, required: false })),
  { submit: "Save access", intro: "Choose what this person can do on each site's HMI. Changes reach the devices at the next publish." });
  if (!v) return;
  const access = Object.entries(v).filter(([, role]) => role).map(([site_id, role]) => ({ site_id, role }));
  if (await act(() => api("PUT", `/api/users/${enc(u.username)}/access`, access), "Access saved")) route();
}

async function resetPassword(u) {
  const v = await formDialog(`Reset password for ${u.username}`, [
    { name: "password", label: "Temporary password", type: "password", generate: true, required: true, value: randomPassword(),
      hint: "They'll be asked to choose their own at next sign-in." }], { submit: "Reset password" });
  if (v && await act(() => api("PATCH", `/api/users/${enc(u.username)}`, v), `Password reset for ${u.username}`)) route();
}

async function changeStaffRole(u) {
  const v = await formDialog(`Role for ${u.username}`, [{ name: "portal_role", label: "Role", type: "select", value: u.portal_role,
    options: [["technician", "Technician – set up clients and publish"], ["admin", "Admin – also manages staff"]] }]);
  if (v && await act(() => api("PATCH", `/api/users/${enc(u.username)}`, v), "Role updated")) route();
}

async function staffPage() {
  const users = await api("GET", "/api/users?kind=staff");
  const main = page([], "Staff", {
    sub: "Your team. Staff sign in to this portal and are integrators on every device (commissioning and recovery access).",
    actions: isAdmin() ? [button("Add staff member", newStaff, "btn primary")] : [] });
  main.append(panel("Staff accounts", [], usersTable(users, null)));
}

async function newStaff() {
  const v = await formDialog("New staff member", [
    { name: "username", label: "Username", required: true },
    { name: "full_name", label: "Full name" },
    { name: "email", label: "Email", type: "email" },
    { name: "portal_role", label: "Role", type: "select", value: "technician",
      options: [["technician", "Technician – set up clients and publish"], ["admin", "Admin – also manages staff"]] },
    { name: "password", label: "Temporary password", type: "password", generate: true, required: true, value: randomPassword() },
  ], { submit: "Create" });
  if (v && await act(() => api("POST", "/api/users", { ...v, kind: "staff" }), `${v.username} created`)) route();
}

/* ----------------------------------------------------------------- sites */
async function sitePage(id) {
  const s = await api("GET", `/api/sites/${enc(id)}`);
  const main = page([["#/clients", "Clients"], [`#/clients/${enc(s.client.id)}`, s.client.name]], s.name, {
    id: s.id, sub: [s.address, s.timezone, `${s.history_days} days of trends on device`].filter(Boolean).join(" · "),
    actions: [
      button("Edit", async () => {
        const v = await formDialog(`Edit ${s.name}`, [
          { name: "name", label: "Site name", required: true, value: s.name },
          { name: "address", label: "Address", value: s.address },
          { name: "timezone", label: "Time zone", value: s.timezone },
          { name: "history_days", label: "Trend history kept on the device (days)", type: "number", value: s.history_days },
          { name: "notes", label: "Notes", type: "textarea", value: s.notes }]);
        if (v && await act(() => api("PATCH", `/api/sites/${enc(id)}`, v), "Site updated")) route();
      }),
      button("Delete", async () => {
        if (await confirmChange(`Delete ${s.name}?`, "Only possible once its assets and devices are removed or moved.", "Delete")
            && await act(() => api("DELETE", `/api/sites/${enc(id)}`), "Site deleted")) go(`#/clients/${enc(s.client.id)}`);
      }, "btn small danger")] });
  const deviceName = (did) => s.devices.find((d) => d.id === did)?.name;
  main.append(
    panel("Assets", [button("Add asset", () => newAsset(s))],
      table(["Asset", "Device", "I/O", "PLC", ""], s.assets.map((a) => row(
        el("td", {}, link(`#/assets/${enc(a.id)}`, a.name), el("span", { class: "id-tag", text: a.id }),
          a.description ? el("div", { class: "muted", text: a.description }) : null),
        a.device_id ? link(`#/devices/${enc(a.device_id)}`, deviceName(a.device_id) || a.device_id) : el("span", { class: "pill warn", text: "No device" }),
        el("td", { class: "num", text: a.tags }), plcSummary(a.plc),
        el("td", { class: "io-act" }, button("Duplicate", () => duplicateAsset(a, s))))), "No assets yet.")),
    el("div", { style: "height:16px" }),
    el("div", { class: "two-col" },
      panel("Devices at this site", [button("Add device", async () => newDevice(await api("GET", `/api/clients/${enc(s.client.id)}`), s.id))],
        table(["Device", "Assets", "Status"], s.devices.map((d) => row(
          el("td", {}, link(`#/devices/${enc(d.id)}`, d.name), el("span", { class: "id-tag", text: d.id })),
          el("td", { class: "num", text: d.assets.length }), deviceStatus(d))), "No devices at this site.")),
      panel("Who can use this site's HMI", [link(`#/clients/${enc(s.client.id)}`, "Manage users")],
        table(["User", "Role"], s.access.map((u) => row(
          el("span", {}, el("b", { text: u.username }), u.disabled ? el("span", { class: "pill bad", text: "Disabled", style: "margin-left:6px" }) : null),
          roleName(u.role))), "No client users yet."),
        el("p", { class: "muted small-print", text: "Plus all your staff, as integrators." }))));
}

async function newAsset(s) {
  const v = await formDialog(`New asset at ${s.name}`, [
    { name: "name", label: "Asset name", required: true, hint: "e.g. Pump Station 2, Process Skid 01" },
    { name: "description", label: "Description" },
    { name: "device_id", label: "Runs on device", type: "select", value: s.devices[0]?.id || "",
      options: [["", "Not assigned yet"], ...s.devices.map((d) => [d.id, d.name])] },
  ], { submit: "Create asset" });
  if (!v) return;
  const a = await act(() => api("POST", `/api/sites/${enc(s.id)}/assets`, v), `Asset ${v.name} created`);
  if (a) go(`#/assets/${enc(a.id)}`);
}

async function duplicateAsset(a, s) {
  const v = await formDialog(`Duplicate ${a.name}`, [
    { name: "name", label: "New asset name", required: true, value: `${a.name} (copy)` }],
  { submit: "Duplicate", intro: "Copies the PLC settings and all I/O. Assign it to a device afterwards." });
  if (!v) return;
  const copy = await act(() => api("POST", `/api/assets/${enc(a.id)}/duplicate`, { ...v, site_id: s.id }), "Asset duplicated");
  if (copy) go(`#/assets/${enc(copy.id)}`);
}

/* ---------------------------------------------------------------- assets */
function addressText(t) {
  const m = t.modbus;
  if (!m) return "—";
  return `${{ coil: "0x", discrete: "1x", input: "3x", holding: "4x" }[m.table]} ${m.address}${m.scale && m.scale !== 1 ? ` ÷${m.scale}` : ""}`;
}

function alarmText(t) {
  const a = t.alarm;
  if (!a) return "";
  if ("state" in a) return `${roleName(a.priority || "medium")} when ${a.state ? "ON" : "OFF"}`;
  return ["hihi", "hi", "lo", "lolo"].filter((k) => a[k] !== undefined).map((k) => `${k.toUpperCase()} ${a[k]}`).join(" · ");
}

async function assetPage(id) {
  const a = await api("GET", `/api/assets/${enc(id)}`);
  const main = page([["#/clients", "Clients"], [`#/clients/${enc(a.client.id)}`, a.client.name], [`#/sites/${enc(a.site.id)}`, a.site.name]],
    a.name, { id: a.id, sub: a.description || "", actions: [
      button("Duplicate", () => duplicateAsset(a, a.site)),
      button("Delete", async () => {
        if (await confirmChange(`Delete ${a.name}?`, "Removes the asset and all its I/O. It disappears from its device at the next publish.", "Delete")
            && await act(() => api("DELETE", `/api/assets/${enc(id)}`), "Asset deleted")) go(`#/sites/${enc(a.site.id)}`);
      }, "btn small danger")] });

  // Details
  const name = el("input", { value: a.name }), desc = el("input", { value: a.description });
  const screen = el("select", {}, el("option", { value: "auto", text: "Automatic overview", selected: a.screen === "auto" }),
    el("option", { value: "tank-demo", text: "Heated tank demo mimic", selected: a.screen === "tank-demo" }));
  const device = el("select", {}, el("option", { value: "", text: "Not assigned" }),
    ...a.site_devices.map((d) => el("option", { value: d.id, text: d.name, selected: d.id === a.device_id })));
  const details = panel("Asset details", [], el("div", { class: "form-grid" },
    el("div", { class: "field" }, el("label", { text: "Name" }), name),
    el("div", { class: "field span2" }, el("label", { text: "Description" }), desc),
    el("div", { class: "field" }, el("label", { text: "Runs on device" }), device),
    el("div", { class: "field" }, el("label", { text: "Overview screen" }), screen)),
  el("div", { class: "btn-row" }, button("Save details", async () => {
    if (await act(() => api("PATCH", `/api/assets/${enc(id)}`, { name: name.value, description: desc.value, screen: screen.value,
      device_id: device.value || null }), "Asset saved")) route();
  }, "btn primary")));

  // PLC connection
  const plc = a.plc;
  const proto = el("select", {}, el("option", { value: "modbus", text: "Modbus TCP", selected: plc.protocol === "modbus" }),
    el("option", { value: "sim", text: "Simulator (no PLC)", selected: plc.protocol === "sim" }));
  const host = el("input", { class: "mono", value: plc.host ?? "", placeholder: "192.168.1.10" });
  const port = el("input", { type: "number", value: plc.port ?? 502 });
  const unit = el("input", { type: "number", value: plc.unit ?? 1 });
  const poll = el("input", { type: "number", value: plc.poll_ms ?? 500, step: 50 });
  const modbusFields = [el("div", { class: "field" }, el("label", { text: "PLC IP address" }), host),
    el("div", { class: "field" }, el("label", { text: "Port" }), port),
    el("div", { class: "field" }, el("label", { text: "Unit ID" }), unit)];
  const syncProto = () => modbusFields.forEach((f) => { f.hidden = proto.value !== "modbus"; });
  proto.onchange = syncProto;
  syncProto();
  const plcPanel = panel("PLC connection", [], el("div", { class: "form-grid" },
    el("div", { class: "field" }, el("label", { text: "Protocol" }), proto), ...modbusFields,
    el("div", { class: "field" }, el("label", { text: "Poll (ms)" }), poll)),
  el("p", { class: "muted small-print", text: "The technician tests this connection on site from the device's Commissioning tab." }),
  el("div", { class: "btn-row" }, button("Save PLC settings", async () => {
    const body = { protocol: proto.value, poll_ms: Number(poll.value) };
    if (proto.value === "modbus") Object.assign(body, { host: host.value.trim(), port: Number(port.value), unit: Number(unit.value) });
    if (await act(() => api("PATCH", `/api/assets/${enc(id)}`, { plc: body }), "PLC settings saved")) route();
  }, "btn primary")));

  // I/O
  const fileInput = el("input", { type: "file", accept: ".csv,text/csv", hidden: true, onchange: (e) => importCsv(e, a) });
  const ioPanel = panel(`I/O points (${a.tags.length})`, [
    button("Add point", () => editTag(a, null), "btn small primary"),
    el("label", { class: "btn small file-btn" }, "Import CSV…", fileInput),
    el("a", { class: "btn small", href: `/api/assets/${enc(id)}/io.csv`, text: "Export CSV" }),
    el("a", { class: "btn small", href: "/api/io-template.csv", text: "CSV template" })],
  table(["Point", "Kind", "Address", "Range", "Alarm", "Access", ""], a.tags.map((t) => row(
    el("td", {}, el("div", { class: "io-desc", text: t.description || t.name }), el("div", { class: "io-name", text: t.name })),
    el("td", { class: "mono", text: `${t.kind} · ${t.type === "bool" ? "digital" : "analog"}` }),
    el("td", { class: "mono", text: addressText(t) }),
    t.type === "float" ? `${t.min ?? "—"} – ${t.max ?? "—"} ${t.unit || ""}` : "",
    alarmText(t),
    t.writable ? `Write: ${roleName(t.role || "operator")}` : "Read only",
    el("td", { class: "io-act" }, button("Edit", () => editTag(a, t))))),
  "No I/O yet – add points one by one, or import a CSV (download the template to see the columns)."));

  main.append(el("div", { class: "two-col" }, details, plcPanel), el("div", { style: "height:16px" }), ioPanel);
}

function editTag(a, tag) {
  TagForm.open(tag, {
    exists: (name) => a.tags.some((t) => t.name === name),
    save: async (def, isNew) => {
      await api("PUT", `/api/assets/${enc(a.id)}/tags/${enc(def.name)}`, def);
      toast(`${def.name} ${isNew ? "added" : "saved"}`);
      route();
    },
    remove: async (name) => {
      await api("DELETE", `/api/assets/${enc(a.id)}/tags/${enc(name)}`);
      toast(`${name} deleted`);
      route();
    },
  });
}

async function importCsv(e, a) {
  const file = e.target.files[0];
  e.target.value = "";
  if (!file) return;
  const v = await formDialog(`Import ${file.name}`, [{ name: "mode", label: "How to import", type: "select", value: "merge",
    options: [["merge", "Merge – add new points, update matching names, keep the rest"],
      ["replace", "Replace – the CSV becomes this asset's complete I/O list"]] }],
  { submit: "Import", intro: "The whole file is checked first; if any row is wrong, nothing is changed." });
  if (!v) return;
  const raw = await file.arrayBuffer();
  const r = await act(() => api("POST", `/api/assets/${enc(a.id)}/import?mode=${v.mode}`, undefined, { raw, type: "text/csv" }));
  if (r) { toast(`Imported: ${r.added} added, ${r.updated} updated${r.removed ? `, ${r.removed} removed` : ""}`); route(); }
}

/* --------------------------------------------------------------- devices */
async function devicePage(id) {
  const d = await api("GET", `/api/devices/${enc(id)}`);
  const crumbs = [["#/clients", "Clients"], [`#/clients/${enc(d.client.id)}`, d.client.name]];
  if (d.site) crumbs.push([`#/sites/${enc(d.site.id)}`, d.site.name]);
  const main = page(crumbs, d.name, { id: d.id, sub: d.serial ? `Serial ${d.serial}` : "IoT2050 gateway", actions: [
    button("Edit", async () => {
      const v = await formDialog(`Edit ${d.name}`, [
        { name: "name", label: "Device name", required: true, value: d.name },
        { name: "serial", label: "Serial number", value: d.serial },
        { name: "notes", label: "Notes", type: "textarea", value: d.notes }]);
      if (v && await act(() => api("PATCH", `/api/devices/${enc(id)}`, v), "Device updated")) route();
    }),
    button("Delete", async () => {
      if (await confirmChange(`Delete ${d.name}?`, "Its assets stay at the site, unassigned. Published package history is removed.", "Delete")
          && await act(() => api("DELETE", `/api/devices/${enc(id)}`), "Device deleted")) go(`#/clients/${enc(d.client.id)}`);
    }, "btn small danger")] });

  // Site & assets
  const siteSel = el("select", {}, el("option", { value: "", text: "Not assigned" }),
    ...(await api("GET", `/api/clients/${enc(d.client.id)}`)).sites.map((s) =>
      el("option", { value: s.id, text: s.name, selected: d.site?.id === s.id })));
  siteSel.onchange = async () => {
    const ok = !d.assets.length || await confirmChange("Move this device?",
      `Its ${d.assets.length} asset(s) stay at ${esc(d.site?.name)} and are unassigned from the device.`, "Move");
    if (ok && await act(() => api("PATCH", `/api/devices/${enc(id)}`, { site_id: siteSel.value || null }), "Device moved")) route();
    else siteSel.value = d.site?.id || "";
  };
  const assign = el("select", {}, el("option", { value: "", text: "Assign an asset…" }),
    ...d.unassigned_assets.map((x) => el("option", { value: x.id, text: x.name })));
  assign.onchange = async () => {
    if (assign.value && await act(() => api("PATCH", `/api/assets/${enc(assign.value)}`, { device_id: id }), "Asset assigned")) route();
  };
  const assetsPanel = panel("Site & assets", [],
    el("div", { class: "field" }, el("label", { text: "Installed at site" }), siteSel),
    el("h4", { text: "Assets this device runs", style: "margin:16px 0 6px;font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em" }),
    table(["Asset", ""], d.assets.map((x) => row(link(`#/assets/${enc(x.id)}`, x.name),
      el("td", { class: "io-act" }, button("Remove", async () => {
        if (await act(() => api("PATCH", `/api/assets/${enc(x.id)}`, { device_id: null }), "Asset removed from device")) route();
      })))), d.site ? "No assets assigned yet." : "Assign the device to a site first."),
    d.site && d.unassigned_assets.length ? el("div", { class: "btn-row" }, assign) : null);

  // Publish
  const checks = [
    [!!d.site, "Installed at a site"],
    [d.assets.length > 0, "Runs at least one asset"],
    [d.problems.length === 0, d.problems.length ? d.problems.join("; ") : "Configuration valid for the device"],
  ];
  const publishBtn = button("Publish new version", async () => {
    const v = await formDialog(`Publish to ${d.name}`, [{ name: "notes", label: "What changed", type: "textarea",
      hint: "Shown in the version history, e.g. “Added pump 2 I/O”." }], { submit: "Publish" });
    if (!v) return;
    const r = await act(() => api("POST", `/api/devices/${enc(id)}/publish`, v));
    if (r) { toast(`Published v${r.version}`); route(); }
  }, "btn primary");
  publishBtn.disabled = d.problems.length > 0;
  const status = d.published
    ? `Latest: v${d.published.version}, ${fmtTime(d.published.created_at)} by ${d.published.created_by}.`
    : "Nothing published yet.";
  const publishPanel = panel("Configuration package", [deviceStatus(d)],
    el("ul", { class: "checklist" }, ...checks.map(([ok, text]) => el("li", { class: ok ? "" : "todo", text }))),
    el("p", { class: "muted", text: `${status} ${d.unpublished_changes ? "There are changes that haven't been published." : ""}` }),
    el("div", { class: "btn-row" }, publishBtn),
    el("p", { class: "muted small-print", text: "Until the device links to the cloud (next step), download the package and load it on the IoT2050 from its Commissioning tab." }));

  const versions = panel("Version history", [], table(["Version", "Published", "By", "Notes", ""], d.versions.map((v) => row(
    el("td", { class: "mono", text: `v${v.version}` }), el("td", { class: "time", text: fmtTime(v.created_at) }), v.created_by, v.notes,
    el("td", { class: "io-act" }, el("a", { class: "btn small", href: `/api/devices/${enc(id)}/packages/${v.version}`, text: "Download" })))),
  "No versions yet."));

  main.append(el("div", { class: "two-col" }, assetsPanel, publishPanel), el("div", { style: "height:16px" }), versions);
}

/* --------------------------------------------------------------- account */
function initAccount() {
  const me = P.me;
  $("#user-name").textContent = me.full_name || me.username;
  $("#user-avatar").textContent = (me.full_name || me.username).split(/\s+/).map((w) => w[0]).join("").slice(0, 2);
  $("#user-role").textContent = me.role;
  $("#user-role").className = `role-tag ${me.role === "admin" ? "integrator" : "engineer"}`;
  $("#menu-head").replaceChildren(el("b", { text: me.full_name || me.username }), el("span", { text: `${me.username} · ${roleName(me.role)}` }));
  const btn = $("#user-btn"), menu = $("#user-menu");
  const close = () => { menu.hidden = true; btn.setAttribute("aria-expanded", "false"); };
  btn.onclick = (e) => { e.stopPropagation(); menu.hidden = !menu.hidden; btn.setAttribute("aria-expanded", String(!menu.hidden)); };
  document.addEventListener("click", (e) => { if (!menu.contains(e.target)) close(); });
  $("#menu-password").onclick = () => { close(); changePassword(false); };
  $("#menu-signout").onclick = signOut;
  $("#theme-btn").onclick = () => {
    const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("hmi-theme", next); } catch { /* ignore */ }
  };
  let lastPing = Date.now();
  for (const ev of ["pointerdown", "keydown"]) {
    window.addEventListener(ev, () => {
      if (Date.now() - lastPing > 60_000) { lastPing = Date.now(); api("POST", "/api/me/ping", {}).catch(() => {}); }
    }, { passive: true });
  }
}

async function signOut() {
  try { await api("POST", "/api/logout", {}); } catch { /* already signed out */ }
  location.replace("/login?signedout=1");
}

async function changePassword(forced) {
  for (;;) {
    const v = await formDialog(forced ? "Choose a new password" : "Change password", [
      { name: "current", label: "Current password", type: "password", required: true },
      { name: "new", label: "New password", type: "password", required: true, hint: "At least 8 characters." },
      { name: "repeat", label: "Repeat new password", type: "password", required: true }],
    { submit: "Change password", intro: forced ? "Your password was set by an admin. Choose your own before you continue." : undefined });
    if (!v) { if (forced) await signOut(); return; }
    if (v.new !== v.repeat) { toast("The new passwords don't match.", "error"); continue; }
    if (await act(() => api("POST", "/api/me/password", { current: v.current, new: v.new }), "Password changed")) {
      P.me.must_change_password = false;
      return;
    }
  }
}

async function init() {
  P.me = await api("GET", "/api/me");
  initAccount();
  if (P.me.must_change_password) await changePassword(true);
  window.addEventListener("hashchange", route);
  route();
}

init().catch((err) => toast(`Failed to load the portal: ${err.message}`, "error"));
