"use strict";
// All model and tool output is inserted with textContent / DOM nodes, never innerHTML.

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
};
let mapOrigin = "";
let busy = false;
let traceN = 0;
let webMapThisTurn = false;

// POST bodies carry their SHA-256: in AWS, CloudFront signs requests to the chat Lambda (origin
// access control) and needs the payload hash from the browser. Harmless locally.
async function sha256Hex(text) {
  if (!(window.crypto && crypto.subtle)) return null;
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return Array.from(new Uint8Array(buf), (b) => b.toString(16).padStart(2, "0")).join("");
}
async function rawPost(path, body) {
  const text = JSON.stringify(body);
  const headers = { "Content-Type": "application/json" };
  const h = await sha256Hex(text);
  if (h) headers["x-amz-content-sha256"] = h;
  return fetch(path, { method: "POST", headers, body: text, credentials: "same-origin" });
}
async function api(path, body) {
  const r = body === undefined
    ? await fetch(path, { credentials: "same-origin" })
    : await rawPost(path, body);
  if (r.status === 401) { showLogin(); throw new Error("signed out"); }
  return r;
}

/* ---------- sign-in ---------- */
function showLogin(msg) {
  $("app").hidden = true; $("who").hidden = true; $("login").hidden = false;
  $("loginerror").textContent = msg || "";
  $("username").focus();
}
function signedIn(me) {
  mapOrigin = me.map_origin;
  $("user").textContent = me.user;
  const roles = $("roles"); roles.replaceChildren();
  (me.roles.length ? me.roles : ["no roles"]).forEach((r) => roles.append(el("span", "role", r)));
  $("login").hidden = true; $("app").hidden = false; $("who").hidden = false;
  $("q").focus();
}
$("loginform").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("loginerror").textContent = "";
  try {
    const r = await rawPost("/api/login", { username: $("username").value, password: $("password").value });
    if (!r.ok) { $("loginerror").textContent = "Sign-in failed. Check the username and password."; return; }
    $("password").value = "";
    signedIn(await r.json());
  } catch { $("loginerror").textContent = "Cannot reach the sign-in service."; }
});
$("signout").addEventListener("click", async () => {
  await rawPost("/api/logout", {});
  clearAll(); showLogin();
});
$("newchat").addEventListener("click", async () => { await api("/api/reset", {}); clearAll(); });

function clearAll() {
  $("log").replaceChildren($("empty")); $("empty").hidden = false;
  $("data").replaceChildren(); $("dataempty").hidden = false;
  $("trace").replaceChildren(); $("traceempty").hidden = false; traceN = 0; $("tracecount").textContent = "0";
  $("map").hidden = true; $("map").removeAttribute("src"); $("mapopen").hidden = true; $("mapempty").hidden = false;
  $("propng").hidden = true; $("propdf").hidden = true; webMapThisTurn = false;
}

/* ---------- tabs ---------- */
function showTab(name) {
  document.querySelectorAll(".tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === name)));
  document.querySelectorAll(".tabpane").forEach((p) => { p.hidden = p.dataset.pane !== name; });
}
document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));

/* ---------- chat ---------- */
function link(label, url) {
  const a = el("a", "", label);
  const isMap = /^\/maps\/[\w.\-\/]+\.html$/.test(url) || (mapOrigin && url.startsWith(mapOrigin + "/maps/"));
  a.href = url; a.rel = "noopener noreferrer";
  if (isMap) a.addEventListener("click", (e) => { e.preventDefault(); showMap(url); });
  else a.target = "_blank";
  return a;
}
function addMsg(cls, text) {
  $("empty").hidden = true;
  const m = el("div", "msg " + cls);
  // light formatting, built as nodes: **bold**, [label](url) and bare links. Only same-site /maps/
  // paths and https URLs become links; map links open in the Map tab.
  const TOKEN = /(\*\*[^*]+\*\*|\[[^\]\n]{1,200}\]\((?:\/maps\/[\w.\-\/]+|https:\/\/[^\s)]+)\)|(?:^|(?<=\s))\/maps\/[\w.\-\/]+\.html|https:\/\/[^\s)\]]*[^\s)\].,;:!?'"])/g;
  String(text).split(TOKEN).forEach((part) => {
    if (!part) return;
    if (/^\*\*[^*]+\*\*$/.test(part)) { m.append(el("strong", "", part.slice(2, -2))); return; }
    const md = part.match(/^\[([^\]]+)\]\((.+)\)$/);
    const url = md ? md[2] : part;
    if (md || /^(\/maps\/|https:\/\/)/.test(part)) m.append(link(md ? md[1] : part, url));
    else m.append(document.createTextNode(part));
  });
  $("log").append(m); m.scrollIntoView({ block: "end" });
  return m;
}
document.querySelectorAll("#suggest button").forEach((b) => b.addEventListener("click", () => { $("q").value = b.dataset.q; send(); }));
$("composer").addEventListener("submit", (e) => { e.preventDefault(); send(); });
$("q").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } });

async function send() {
  const q = $("q").value.trim();
  if (!q || busy) return;
  busy = true; $("send").disabled = true; $("q").value = "";
  addMsg("user", q);
  $("propng").hidden = true; $("propdf").hidden = true; webMapThisTurn = false;
  const steps = el("div", "steps"); $("log").append(steps);
  const thinking = el("div", "thinking", "Thinking"); $("log").append(thinking);
  const chips = {};
  try {
    const r = await api("/api/chat", { message: q });
    if (!r.ok) throw new Error("HTTP " + r.status);
    const reader = r.body.getReader(); const dec = new TextDecoder(); let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, i); buf = buf.slice(i + 1);
        if (line.trim()) handle(JSON.parse(line), steps, thinking, chips);
      }
    }
  } catch (err) {
    if (String(err.message) !== "signed out") addMsg("err", "The request failed. Is the model running?");
  } finally {
    thinking.remove(); if (!steps.childElementCount) steps.remove();
    busy = false; $("send").disabled = false; $("q").focus();
  }
}

function handle(ev, steps, thinking, chips) {
  if (ev.type === "start") {
    thinking.textContent = "Connected: " + ev.servers.join(" + ") + " (" + ev.tools + " tools). Thinking";
    return;
  }
  if (ev.type === "tool_call") {
    const c = el("span", "step", ev.tool); steps.append(c);
    (chips[ev.tool] = chips[ev.tool] || []).push(c);
    thinking.textContent = "Running " + ev.tool;
    addTrace(ev);
  } else if (ev.type === "tool_result") {
    const c = (chips[ev.tool] || []).shift();
    if (c) c.classList.add(ev.is_error ? "bad" : "ok");
    finishTrace(ev);
    thinking.textContent = "Writing the answer";
    if (!ev.is_error) showResult(ev);
  } else if (ev.type === "answer") {
    thinking.remove(); addMsg("bot", ev.content || "(no answer)");
  } else if (ev.type === "error") {
    addMsg("err", ev.message);
  }
}

/* ---------- trace ---------- */
const pending = [];
function addTrace(ev) {
  $("traceempty").hidden = true;
  const li = el("li"); const row = el("div", "row");
  row.append(el("span", "name", ev.tool), el("span", "st run", "running"));
  li.append(row, el("pre", "", JSON.stringify(ev.args, null, 2)));
  $("trace").append(li); pending.push(li);
  $("tracecount").textContent = String(++traceN);
}
function finishTrace(ev) {
  const li = pending.shift(); if (!li) return;
  const st = li.querySelector(".st");
  st.className = "st " + (ev.is_error ? "bad" : "ok");
  st.textContent = ev.is_error ? "refused / error" : "ok";
  if (ev.is_error && ev.text) li.append(el("pre", "", ev.text));
}

/* ---------- results: map + tables ---------- */
function showMap(src, openUrl) {
  $("map").src = src; $("map").hidden = false; $("mapempty").hidden = true;
  $("mapopen").href = openUrl || src; $("mapopen").hidden = false;
  showTab("map");
}
// ArcGIS Pro results: the Pro server copies the PNG/PDF into maps/pro on the Mac (PRO_COPY_TO),
// which the map viewer serves. Only a bare file name is taken from the result, never a path.
function proFile(d, ext) {
  const hit = (d.copied_to_host || []).find((p) => typeof p === "string" && p.toLowerCase().endsWith(ext));
  if (!hit) return null;
  const name = hit.split(/[\\/]/).pop();
  return /^[\w .()+-]+$/.test(name) ? mapOrigin + "/maps/pro/" + encodeURIComponent(name) : null;
}
function showResult(ev) {
  const d = ev.data;
  // Maps come from the map viewer origin, or (AWS) from this same origin as a relative /maps/ path.
  const isMapUrl = (u) => typeof u === "string" &&
    ((mapOrigin && u.startsWith(mapOrigin + "/")) || /^\/maps\/[\w.\-\/]+$/.test(u));
  if (d && typeof d === "object" && isMapUrl(d.url)) {
    showMap(d.url); webMapThisTurn = true;
  } else if (d && typeof d === "object" && Array.isArray(d.copied_to_host)) {
    // Keep the interactive web map in the tab; the Pro layout (static PNG/PDF) goes in the links.
    const png = proFile(d, ".png"), pdf = proFile(d, ".pdf");
    if (png) { $("propng").href = png; $("propng").hidden = false; }
    if (pdf) { $("propdf").href = pdf; $("propdf").hidden = false; }
    if (!webMapThisTurn && (png || pdf)) showMap(png || pdf, pdf || png);
    else showTab("map");
  }
  const card = el("article", "result"); const head = el("header");
  head.append(el("h3", "", ev.tool), el("span", "meta", new Date().toLocaleTimeString()));
  card.append(head);
  let rows = null;
  if (Array.isArray(d)) rows = d;
  else if (d && typeof d === "object" && !Array.isArray(d.impacted)) {
    // Generic: show each list of records as its own table (e.g. hotspots, by_year).
    const lists = Object.entries(d).filter(([, v]) => Array.isArray(v) && v.length && typeof v[0] === "object");
    if (lists.length) {
      const scalars = Object.entries(d).filter(([, v]) => v !== null && typeof v !== "object");
      if (scalars.length) card.append(el("div", "summary", scalars.map(([k, v]) => k + ": " + v).join(" · ")));
      const totals = d.totals && typeof d.totals === "object" ? d.totals : null;
      if (totals) card.append(table([totals]));
      lists.forEach(([k, v]) => { card.append(el("h3", "subhead", k.replace(/_/g, " "))); card.append(table(v)); });
      $("dataempty").hidden = true; $("data").prepend(card);
      if (!(d && (d.url || d.copied_to_host))) showTab("data");
      return;
    }
  }
  else if (d && Array.isArray(d.impacted)) {
    const s = el("div", "summary");
    s.append(el("span", "pill neutral", "Failed: " + d.failed_asset));
    if (d.summary) {
      s.append(el("span", "pill lost", d.summary.lost + " lost"));
      s.append(el("span", "pill degraded", d.summary.degraded + " degraded"));
    }
    card.append(s);
    rows = d.impacted.map((r) => ({ id: r.id, name: r.name, type: r.asset_type, status: r.status, order: r.order, why: (r.reasons || []).join("; ") }));
  }
  if (rows && rows.length && typeof rows[0] === "object") card.append(table(rows));
  else if (d !== null && d !== undefined) card.append(el("pre", "json", JSON.stringify(d, null, 2)));
  else card.append(el("pre", "json", ev.text || ""));
  $("dataempty").hidden = true;
  $("data").prepend(card);
  if (!(d && (d.url || (d.copied_to_host || []).length))) showTab("data");
}
function table(rows) {
  const wrap = el("div", "tablewrap"); const t = el("table");
  const cols = Object.keys(rows[0]).filter((k) => k !== "geom");
  const thead = el("thead"); const hr = el("tr");
  cols.forEach((c) => hr.append(el("th", "", c.replace(/_/g, " "))));
  thead.append(hr); t.append(thead);
  const tb = el("tbody");
  rows.slice(0, 500).forEach((r) => {
    const tr = el("tr", r.status === "lost" ? "lost" : r.status === "degraded" ? "degraded" : "");
    cols.forEach((c) => {
      const v = r[c];
      tr.append(el("td", typeof v === "number" ? "num" : "", v === null || v === undefined ? "" : typeof v === "object" ? JSON.stringify(v) : String(v)));
    });
    tb.append(tr);
  });
  t.append(tb); wrap.append(t); return wrap;
}

/* ---------- start ---------- */
(async () => {
  try {
    const r = await fetch("/api/me", { credentials: "same-origin" });
    if (r.ok) signedIn(await r.json()); else showLogin();
  } catch { showLogin("Cannot reach the server."); }
})();
