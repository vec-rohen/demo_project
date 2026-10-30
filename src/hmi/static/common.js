"use strict";
/* Helpers shared by the edge HMI and the cloud portal. */

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
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const roleName = (r) => r.charAt(0).toUpperCase() + r.slice(1);

function toast(message, kind = "ok") {
  let box = $("#toasts");
  if (!box) { box = el("div", { class: "toasts", id: "toasts", "aria-live": "polite" }); document.body.append(box); }
  const node = el("div", { class: `toast ${kind}`, role: kind === "error" ? "alert" : "status", text: message });
  box.append(node);
  setTimeout(() => node.remove(), kind === "error" ? 6000 : 3000);
}

// JSON API call. Pass { raw, type } to send a non-JSON body (e.g. a CSV file).
async function api(method, path, body, { raw, type } = {}) {
  const res = await fetch(path, {
    method,
    headers: { "Content-Type": type || "application/json" },
    body: raw !== undefined ? raw : body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401 && path !== "/api/me/password") { location.replace("/login?expired=1"); throw new Error("Signed out"); }
  if (!res.ok) {
    const detail = Array.isArray(data.detail) // FastAPI validation errors arrive as a list
      ? data.detail.map((d) => `${(d.loc || []).slice(1).join(".")}: ${d.msg}`).join("; ")
      : data.detail;
    throw new Error(detail || `${res.status} ${res.statusText}`);
  }
  return data;
}

// Modal yes/no confirmation; resolves true when the user presses the OK button.
function confirmChange(title, html, okLabel = "Apply") {
  let dlg = $("#confirm");
  if (!dlg) {
    dlg = el("dialog", { id: "confirm" }, el("form", { method: "dialog" },
      el("h3", { id: "confirm-title" }), el("p", { id: "confirm-body" }),
      el("div", { class: "dialog-actions" },
        el("button", { value: "cancel", class: "btn", text: "Cancel" }),
        el("button", { value: "ok", class: "btn primary", id: "confirm-ok", text: "Apply" }))));
    document.body.append(dlg);
  }
  $("#confirm-title").textContent = title;
  $("#confirm-body").innerHTML = html;
  $("#confirm-ok").textContent = okLabel;
  dlg.returnValue = "";
  dlg.showModal();
  $("#confirm-ok").focus();
  return new Promise((resolve) => dlg.addEventListener("close", () => resolve(dlg.returnValue === "ok"), { once: true }));
}

function randomPassword() {
  const alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789";
  const bytes = crypto.getRandomValues(new Uint8Array(12));
  return [...bytes].map((b) => alphabet[b % alphabet.length]).join("");
}

function downloadText(filename, text, type = "text/csv") {
  const link = el("a", { href: URL.createObjectURL(new Blob([text], { type })), download: filename });
  document.body.append(link);
  link.click();
  link.remove();
}
