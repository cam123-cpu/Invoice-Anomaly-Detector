"use strict";
/* Invoice Review web app. Plain JavaScript, no external libraries.
 * Safety rule: text from the server (vendor names, file contents) is only ever inserted
 * with textContent, never as HTML, so an uploaded "<script>" is shown, not run. */

const RULES = {
  duplicate: ["Duplicate", "Same vendor, amount and date as an earlier invoice", "high"],
  bank_account_change: ["New bank account", "First payment to a different bank account for an established vendor", "high"],
  shared_bank_account: ["Shared bank account", "The same bank account is used by another vendor", "high"],
  amount_outlier: ["Unusual amount", "Far above this vendor's normal invoice amount", "mid"],
  split_invoice: ["Possible split", "Several invoices in a few days that together pass the approval limit", "mid"],
  near_duplicate: ["Near-duplicate", "Same vendor and amount a few days apart", "mid"],
  similar_vendor_name: ["Look-alike vendor", "Vendor name is almost identical to another vendor's", "mid"],
  new_vendor_large: ["Large first invoice", "Large invoice from a vendor with almost no history", "mid"],
  ml_outlier: ["Unusual pattern", "The statistical model found an unusual mix of amount, timing and vendor history", "mid"],
  below_threshold: ["Just under limit", "Amount is just below the approval threshold", "low"],
  round_amount: ["Round amount", "Suspiciously round figure", "low"],
  weekend_invoice: ["Weekend date", "Invoice is dated on a weekend", "low"],
};
const ACTIONS = {
  login: "Signed in", login_failed: "Failed sign-in", file_imported: "Imported a file",
  invoices_imported: "Imported invoices", detection_run: "Ran detection", invoice_reviewed: "Reviewed an invoice",
  bank_account_viewed: "Viewed a bank account", user_created: "Added a user", settings_changed: "Changed settings",
};
const CAN_EDIT = ["admin", "accountant"];

/* ------------------------------------------------------------ helpers */
function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "value") el.value = v;
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  const add = (c) => {
    if (c === null || c === undefined || c === false) return;
    if (Array.isArray(c)) c.forEach(add);
    else el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  };
  kids.forEach(add);
  return el;
}
const $ = (sel) => document.querySelector(sel);
/* Replace an element's contents, skipping null/false the same way h() does
 * (the DOM's own replaceChildren would print them as the text "null"). */
function put(el, ...kids) {
  el.textContent = "";
  const add = (c) => {
    if (c === null || c === undefined || c === false) return;
    if (Array.isArray(c)) c.forEach(add);
    else el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  };
  kids.forEach(add);
}
const money = new Intl.NumberFormat(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const whole = new Intl.NumberFormat();
const fmtAmount = (n) => (n === null || n === undefined ? "" : money.format(n));
const fmtDate = (iso) => {
  if (!iso) return "";
  const d = new Date(iso.length === 10 ? iso + "T00:00:00" : iso);
  return isNaN(d) ? iso : iso.length === 10
    ? d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" })
    : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
};
const ruleLabel = (r) => (RULES[r] || [r])[0];
const ruleLevel = (r) => (RULES[r] || [, , "low"])[2];
const riskClass = (n) => (n >= 70 ? "high" : n >= 40 ? "mid" : "low");

const memory = {};
const store = {
  get(k, persistent) {
    try { return (persistent ? localStorage : sessionStorage).getItem(k); } catch { return memory[k] ?? null; }
  },
  set(k, v, persistent) {
    try { (persistent ? localStorage : sessionStorage).setItem(k, v); } catch { memory[k] = v; }
  },
  del(k) {
    try { sessionStorage.removeItem(k); } catch { /* ignore */ }
    delete memory[k];
  },
};

function toast(message, isError) {
  const t = h("div", { class: "toast" + (isError ? " err" : ""), role: "status" }, message);
  $("#toasts").append(t);
  setTimeout(() => t.remove(), isError ? 6000 : 3200);
}

/* ------------------------------------------------------------ API */
let session = null; // { token, username, role, company }

class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

async function api(method, path, body) {
  const headers = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (session) headers.Authorization = "Bearer " + session.token;
  let res;
  try {
    res = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  } catch {
    throw new ApiError(0, "Can't reach the server. Is it still running?");
  }
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (res.status === 401 && session && path !== "/login") {
    signOut("Your session expired. Please sign in again.");
    throw new ApiError(401, "Signed out");
  }
  if (!res.ok) {
    let msg = data && data.detail;
    if (Array.isArray(msg)) msg = msg.map((d) => `${(d.loc || []).slice(1).join(".")}: ${d.msg}`).join("; ");
    throw new ApiError(res.status, msg || `Request failed (${res.status})`);
  }
  return data;
}

function signOut(message) {
  session = null;
  store.del("session");
  if (message) toast(message, true);
  location.hash = "#/login";
  render();
}

/* ------------------------------------------------------------ shell */
const PAGES = [
  { id: "dashboard", label: "Dashboard", view: dashboardView },
  { id: "review", label: "Review queue", view: reviewView },
  { id: "upload", label: "Upload invoices", view: uploadView, roles: CAN_EDIT },
  { id: "invoices", label: "Invoices", view: invoicesView },
  { id: "audit", label: "Activity log", view: auditView, roles: ["admin", "auditor"] },
  { id: "settings", label: "Settings", view: settingsView },
];
let openCount = null;

function shell(active, content) {
  const nav = h("nav", { class: "nav", "aria-label": "Main" },
    PAGES.filter((p) => !p.roles || p.roles.includes(session.role)).map((p) =>
      h("a", { href: "#/" + p.id, class: p.id === active ? "active" : null,
               "aria-current": p.id === active ? "page" : null },
        p.label, p.id === "review" && openCount ? h("span", { class: "count", "aria-label": `${openCount} open` }, whole.format(openCount)) : null)));
  return h("div", { class: "shell" },
    h("aside", { class: "side" },
      h("div", { class: "brand" }, h("div", { class: "logo", "aria-hidden": "true" }, "IR"),
        h("div", { class: "meta" }, h("div", { class: "brand-name" }, "Invoice Review"),
          h("div", { class: "brand-org" }, session.company))),
      nav,
      h("div", { class: "who" },
        h("div", { class: "meta" }, h("div", { class: "name" }, session.username),
          h("div", { class: "muted small" }, session.role)),
        h("button", { class: "btn link small", onclick: () => signOut() }, "Sign out"))),
    h("main", { class: "main", id: "main" }, h("div", { class: "page" }, content)));
}

function pageHead(title, subtitle, ...actions) {
  return h("div", { class: "page-head" },
    h("div", {}, h("h1", {}, title), subtitle ? h("p", { class: "muted" }, subtitle) : null),
    h("div", { class: "row" }, actions));
}

function loading() { return h("p", { class: "muted" }, "Loading…"); }

function failure(err) {
  return h("div", { class: "error-box", role: "alert" }, err.message || String(err));
}

let renderToken = 0;
async function render() {
  const mine = ++renderToken;
  const root = $("#app");
  const route = (location.hash.replace(/^#\/?/, "") || "dashboard").split("?")[0];
  if (!session) {
    put(root, loginView());
    return;
  }
  const page = PAGES.find((p) => p.id === route && (!p.roles || p.roles.includes(session.role)));
  if (!page) { location.hash = "#/dashboard"; return; }
  const holder = h("div", {}, loading());
  put(root, shell(page.id, holder));
  document.title = `${page.label} · Invoice Review`;
  try {
    const content = await page.view();
    if (mine === renderToken) put(holder, content);
  } catch (err) {
    if (mine === renderToken && err.status !== 401) put(holder, failure(err));
  }
}

async function refreshCount() {
  try {
    const s = await api("GET", "/summary");
    openCount = s.open;
  } catch { /* the badge is optional */ }
}

/* ------------------------------------------------------------ login */
function loginView() {
  document.title = "Sign in · Invoice Review";
  const err = h("div", { class: "error-box hidden", role: "alert" });
  const user = h("input", { id: "username", name: "username", autocomplete: "username", required: true, autofocus: true });
  const pass = h("input", { id: "password", name: "password", type: "password", autocomplete: "current-password", required: true });
  const submit = h("button", { class: "btn primary", type: "submit" }, "Sign in");
  const form = h("form", { class: "form", onsubmit: async (e) => {
    e.preventDefault();
    submit.disabled = true;
    err.classList.add("hidden");
    try {
      const tok = await api("POST", "/login", { username: user.value.trim(), password: pass.value });
      session = { token: tok.access_token };
      const me = await api("GET", "/me");
      session = { token: tok.access_token, username: me.username, role: me.role, company: me.company };
      store.set("session", JSON.stringify(session));
      await refreshCount();
      if (!location.hash || location.hash === "#/login") location.hash = "#/dashboard";
      else render();
    } catch (ex) {
      session = null;
      err.textContent = ex.status === 401 ? "Wrong username or password." : ex.message;
      err.classList.remove("hidden");
      submit.disabled = false;
    }
  } },
    h("label", { class: "field", for: "username" }, "Username", user),
    h("label", { class: "field", for: "password" }, "Password", pass),
    err, submit);
  return h("div", { class: "login-wrap" },
    h("div", { class: "card login" },
      h("div", { class: "brand" }, h("div", { class: "logo", "aria-hidden": "true" }, "IR"),
        h("div", {}, h("div", { class: "brand-name" }, "Invoice Review"),
          h("div", { class: "brand-org" }, "Sign in to your company"))),
      form,
      h("p", { class: "muted small" }, "Flags are signals for review, not proof of fraud.")));
}

/* ------------------------------------------------------------ dashboard */
async function dashboardView() {
  const [s, m] = await Promise.all([api("GET", "/summary"), api("GET", "/metrics")]);
  openCount = s.open;
  const canEdit = CAN_EDIT.includes(session.role);
  const head = pageHead("Dashboard", s.company,
    canEdit ? h("a", { class: "btn", href: "#/upload" }, "Upload invoices") : null,
    h("a", { class: "btn primary", href: "#/review" }, "Open review queue"));

  if (!s.invoices) {
    return h("div", {}, head, h("div", { class: "card empty" },
      h("div", { class: "big" }, "No invoices yet"),
      h("p", { class: "muted" }, "Upload an Excel or CSV export from your accounting system to get started."),
      canEdit ? h("a", { class: "btn primary", href: "#/upload" }, "Upload your first file") : null));
  }

  const kpi = (label, value, sub, alert) => h("div", { class: "card kpi" + (alert ? " alert" : "") },
    h("div", { class: "label" }, label), h("div", { class: "value" }, value), sub ? h("div", { class: "sub" }, sub) : null);
  const kpis = h("div", { class: "kpis" },
    kpi("Invoices", whole.format(s.invoices), s.last_import ? `Last import ${fmtDate(s.last_import.at)}` : null),
    kpi("Need review", whole.format(s.open), `${whole.format(s.flagged)} flagged in total`),
    kpi("High risk, open", whole.format(s.high_risk_open), "Risk score 70 or more", s.high_risk_open > 0),
    kpi("Confirmed problems", whole.format(s.confirmed)),
    kpi("False alarms", whole.format(s.dismissed)),
    kpi("Flag accuracy", m.precision === null ? "—" : Math.round(m.precision * 100) + "%",
      m.reviewed ? `from ${whole.format(m.reviewed)} reviews` : "after your first reviews"));

  const maxRule = Math.max(1, ...Object.values(s.open_by_rule));
  const reasons = h("div", { class: "card" }, h("h2", {}, "Why open invoices were flagged"),
    Object.keys(s.open_by_rule).length
      ? Object.entries(s.open_by_rule).map(([rule, n]) => {
        const fill = h("div", { class: "fill" });
        fill.style.width = (100 * n / maxRule) + "%";
        return h("div", { class: "hbar", title: (RULES[rule] || [, ""])[1] },
          h("span", {}, ruleLabel(rule)), h("div", { class: "track" }, fill), h("span", { class: "n" }, n));
      })
      : h("p", { class: "muted" }, "Nothing open. Run detection after your next upload."));

  const reviewed = Object.entries(m.rules).filter(([, r]) => r.confirmed + r.dismissed > 0);
  const perf = h("div", { class: "card" }, h("h2", {}, "How each check is performing"),
    reviewed.length
      ? h("div", { class: "table-wrap" }, h("table", {},
        h("thead", {}, h("tr", {}, h("th", {}, "Check"), h("th", { class: "num" }, "Confirmed"),
          h("th", { class: "num" }, "False alarms"), h("th", { class: "num" }, "Weight now"))),
        h("tbody", {}, reviewed.map(([rule, r]) => h("tr", {},
          h("td", {}, ruleLabel(rule)), h("td", { class: "num" }, r.confirmed), h("td", { class: "num" }, r.dismissed),
          h("td", { class: "num", title: `Started at ${r.base_weight}` },
            r.weight.toFixed(2), r.weight < r.base_weight ? h("span", { class: "muted small" }, " ↓") : null))))))
      : h("p", { class: "muted" }, "As your team confirms or dismisses flags, this shows which checks are useful for your company. Checks that keep raising false alarms automatically count for less."));

  const last = s.last_detection
    ? h("p", { class: "muted small" }, `Detection last ran ${fmtDate(s.last_detection.at)} by ${s.last_detection.username}: ${s.last_detection.detail}.`)
    : h("p", { class: "muted small" }, "Detection hasn't run yet.");
  return h("div", {}, head, kpis, h("div", { class: "grid grid-2" }, reasons, perf), spacer(12), last);
}

/* ------------------------------------------------------------ review queue */
async function reviewView() {
  let reviewFilter = "open";
  const items = await api("GET", "/review-queue?include_dismissed=true&limit=500");
  const canEdit = CAN_EDIT.includes(session.role);
  const counts = { open: 0, confirmed: 0, dismissed: 0, all: items.length };
  items.forEach((i) => { counts[i.status] += 1; });
  openCount = counts.open;
  let query = "";

  const body = h("tbody");
  const wrap = h("div", { class: "table-wrap" });
  const emptyBox = h("div", { class: "card empty hidden" });

  function drawRows() {
    const q = query.toLowerCase();
    const shown = items.filter((i) => (reviewFilter === "all" || i.status === reviewFilter) &&
      (!q || (i.invoice_id + " " + (i.vendor || "")).toLowerCase().includes(q)));
    put(body);
    for (const item of shown) addRow(item);
    const none = shown.length === 0;
    wrap.classList.toggle("hidden", none);
    emptyBox.classList.toggle("hidden", !none);
    put(emptyBox, h("div", { class: "big" },
      q ? "No matches" : reviewFilter === "open" ? "Nothing waiting for review" : "Nothing here"),
      h("p", { class: "muted" }, q ? "Try a different search." : reviewFilter === "open"
        ? "New flags appear here after an upload or a detection run." : ""));
  }

  function actionsFor(item) {
    if (!canEdit) return h("span", { class: "pill " + item.status }, item.status === "dismissed" ? "false alarm" : item.status);
    const decide = (verdict) => async (e) => {
      e.stopPropagation();
      e.currentTarget.disabled = true;
      try {
        await api("POST", "/review/" + encodeURIComponent(item.invoice_id), { verdict });
        counts[item.status] -= 1;
        item.status = verdict;
        counts[verdict] += 1;
        openCount = counts.open;
        toast(verdict === "confirmed" ? `${item.invoice_id} marked as a real problem` : `${item.invoice_id} dismissed as a false alarm`);
        updateTabs();
        drawRows();
        updateNavCount();
      } catch (err) {
        toast(err.message, true);
        e.currentTarget.disabled = false;
      }
    };
    if (item.status === "open") {
      return h("div", { class: "row" },
        h("button", { class: "btn sm danger", onclick: decide("confirmed"), "aria-label": `Confirm ${item.invoice_id} is a problem` }, "Confirm"),
        h("button", { class: "btn sm", onclick: decide("dismissed"), "aria-label": `Dismiss ${item.invoice_id} as a false alarm` }, "Dismiss"));
    }
    const other = item.status === "confirmed" ? "dismissed" : "confirmed";
    return h("div", { class: "row" },
      h("span", { class: "pill " + item.status }, item.status === "dismissed" ? "false alarm" : "confirmed"),
      h("button", { class: "btn link small", onclick: decide(other) }, other === "confirmed" ? "Confirm instead" : "Dismiss instead"));
  }

  function addRow(item) {
    const level = riskClass(item.risk);
    const fill = h("div", { class: "fill" });
    fill.style.width = item.risk + "%";
    const details = h("tr", { class: "details hidden" }, h("td", { colspan: 7 },
      h("ul", {}, item.details.map((d, i) => h("li", {}, h("strong", {}, ruleLabel(item.rules[i]) + ": "), d)))));
    const row = h("tr", { class: "clickable", tabindex: 0, "aria-expanded": "false",
      onclick: () => toggle(), onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); } } },
      h("td", { class: "c-risk" }, h("span", { class: "risk " + level, title: `Risk ${item.risk} of 100` },
        h("span", { class: "n" }, item.risk), h("span", { class: "bar" }, fill))),
      h("td", { class: "mono c-id" }, item.invoice_id),
      h("td", { class: "c-vendor" }, item.vendor),
      h("td", { class: "num c-amount" }, fmtAmount(item.amount)),
      h("td", { class: "c-date" }, fmtDate(item.date)),
      h("td", { class: "c-why" }, h("div", { class: "chips" }, item.rules.map((r) =>
        h("span", { class: "chip " + ruleLevel(r), title: (RULES[r] || [, ""])[1] }, ruleLabel(r))))),
      h("td", { class: "c-act", onclick: (e) => e.stopPropagation() }, actionsFor(item)));
    function toggle() {
      const open = details.classList.toggle("hidden") === false;
      row.setAttribute("aria-expanded", String(open));
    }
    body.append(row, details);
  }

  const tabs = h("div", { class: "tabs", role: "tablist" });
  function updateTabs() {
    put(tabs, ...[["open", "Open"], ["confirmed", "Confirmed"], ["dismissed", "False alarms"], ["all", "All"]]
      .map(([key, label]) => h("button", { class: key === reviewFilter ? "on" : null, role: "tab",
        "aria-selected": String(key === reviewFilter),
        onclick: () => { reviewFilter = key; updateTabs(); drawRows(); } }, `${label} (${counts[key]})`)));
  }
  updateTabs();

  const search = h("input", { class: "search", type: "search", placeholder: "Search invoice or vendor",
    "aria-label": "Search invoice or vendor", oninput: (e) => { query = e.target.value; drawRows(); } });
  const runBtn = canEdit ? h("button", { class: "btn primary", onclick: async () => {
    runBtn.disabled = true;
    try {
      const r = await api("POST", "/detect");
      toast(`Checked ${whole.format(r.invoices_checked)} invoices, ${whole.format(r.invoices_flagged)} flagged`);
      await refreshCount();
      render();
    } catch (err) { toast(err.message, true); runBtn.disabled = false; }
  } }, "Run detection") : null;

  wrap.append(h("table", { class: "queue" },
    h("thead", {}, h("tr", {}, h("th", {}, "Risk"), h("th", {}, "Invoice"), h("th", {}, "Vendor"),
      h("th", { class: "num" }, "Amount"), h("th", {}, "Date"), h("th", {}, "Why flagged"),
      h("th", {}, canEdit ? "Decision" : "Status"))),
    body));
  drawRows();
  return h("div", {},
    pageHead("Review queue", "Highest risk first. Click a row to see the reasons. Your decisions tune the scoring for your company.", runBtn),
    h("div", { class: "row" }, tabs, h("span", { class: "spacer" }), search),
    spacer(12), wrap, emptyBox);
}

function spacer(px) { const d = h("div"); d.style.height = px + "px"; return d; }

function updateNavCount() {
  const link = document.querySelector('.nav a[href="#/review"]');
  if (!link) return;
  const old = link.querySelector(".count");
  if (old) old.remove();
  if (openCount) link.append(h("span", { class: "count" }, whole.format(openCount)));
}

/* ------------------------------------------------------------ upload */
function toBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(binary);
}

async function uploadView() {
  const holder = h("div");
  const steps = (n) => h("div", { class: "steps", "aria-label": "Progress" },
    ["Choose file", "Match columns", "Results"].map((label, i) =>
      h("div", { class: "step" + (i + 1 === n ? " on" : i + 1 < n ? " done" : "") },
        h("span", { class: "dot" }, i + 1 < n ? "✓" : i + 1), label)));

  function chooseStep(error) {
    const input = h("input", { type: "file", accept: ".xlsx,.xlsm,.csv,.txt,.tsv", class: "hidden", id: "file-input",
      onchange: () => input.files[0] && readFile(input.files[0]) });
    const drop = h("div", { class: "drop",
      ondragover: (e) => { e.preventDefault(); drop.classList.add("over"); },
      ondragleave: () => drop.classList.remove("over"),
      ondrop: (e) => { e.preventDefault(); drop.classList.remove("over"); if (e.dataTransfer.files[0]) readFile(e.dataTransfer.files[0]); } },
      h("div", { class: "big" }, "Drop an Excel or CSV file here"),
      h("p", { class: "muted" }, "An invoice export from QuickBooks, Xero, NetSuite, SAP or your own spreadsheet. Up to 10 MB."),
      h("button", { class: "btn primary", onclick: () => input.click() }, "Choose file"), input);
    put(holder, steps(1), error ? failure(error) : null, error ? spacer(12) : null, drop,
      spacer(16), h("p", { class: "muted small" },
        "Each row should be one invoice with at least an invoice number, vendor, amount and date. A bank account column is optional but enables the bank checks. Columns can have any names; you'll match them in the next step."));
  }

  async function readFile(file) {
    if (file.size > 10 * 1024 * 1024) return chooseStep(new Error("That file is larger than 10 MB. Split it into smaller files."));
    put(holder, steps(1), h("p", { class: "muted" }, `Reading ${file.name}…`));
    try {
      const content = toBase64(await file.arrayBuffer());
      const info = await api("POST", "/import/preview", { filename: file.name, content_base64: content });
      mapStep(file.name, content, info);
    } catch (err) { if (err.status !== 401) chooseStep(err); }
  }

  function mapStep(filename, content, info) {
    const memoryKey = "mapping:" + info.headers.join("|");
    let saved = null;
    try { saved = JSON.parse(store.get(memoryKey, true) || "null"); } catch { saved = null; }
    const mapping = Object.assign({}, info.mapping, saved && saved.mapping);
    let dateOrder = (saved && saved.date_order) || info.date_order;

    const err = h("div", { class: "error-box hidden", role: "alert" });
    const previewTable = h("div", { class: "table-wrap" });
    function drawPreview() {
      const fieldFor = {};
      for (const [f, col] of Object.entries(mapping)) if (col) fieldFor[col] = info.fields.find((x) => x.name === f).label.replace(" (optional)", "");
      put(previewTable, h("table", {},
        h("thead", {}, h("tr", {}, info.headers.map((col) => h("th", {}, col, fieldFor[col] ? h("span", { class: "maps" }, "→ " + fieldFor[col]) : null)))),
        h("tbody", {}, info.sample_rows.map((r) => h("tr", {}, r.map((v) => h("td", {}, v)))))));
    }
    const selects = info.fields.map((f) => {
      const sel = h("select", { id: "map-" + f.name, onchange: () => { mapping[f.name] = sel.value || null; drawPreview(); } },
        h("option", { value: "" }, f.required ? "Choose a column…" : "Not in this file"),
        info.headers.map((col) => h("option", { value: col }, col)));
      sel.value = mapping[f.name] || "";
      return h("label", { class: "field", for: "map-" + f.name },
        h("span", {}, f.label.replace(" (optional)", ""), f.required ? h("span", { class: "req", "aria-hidden": "true" }, " *") : h("span", { class: "muted" }, " (optional)")), sel);
    });
    const orderSel = h("select", { id: "date-order", onchange: () => { dateOrder = orderSel.value; } },
      h("option", { value: "MDY" }, "Month first: 03/05/2026 is March 5"),
      h("option", { value: "DMY" }, "Day first: 03/05/2026 is 3 May"));
    orderSel.value = dateOrder;
    const detect = h("input", { type: "checkbox", id: "run-detect", checked: true });
    const go = h("button", { class: "btn primary", onclick: async () => {
      err.classList.add("hidden");
      const missing = info.fields.filter((f) => f.required && !mapping[f.name]);
      if (missing.length) {
        err.textContent = "Choose a column for: " + missing.map((f) => f.label).join(", ");
        return err.classList.remove("hidden");
      }
      go.disabled = true;
      go.textContent = "Importing…";
      try {
        const result = await api("POST", "/import", { filename, content_base64: content, mapping, date_order: dateOrder, run_detection: detect.checked });
        store.set(memoryKey, JSON.stringify({ mapping, date_order: dateOrder }), true);
        await refreshCount();
        updateNavCount();
        resultStep(filename, result);
      } catch (ex) {
        if (ex.status === 401) return;
        err.textContent = ex.message;
        err.classList.remove("hidden");
        go.disabled = false;
        go.textContent = "Import invoices";
      }
    } }, "Import invoices");
    drawPreview();
    put(holder, steps(2),
      h("div", { class: "card" },
        h("h2", {}, "Which column is which?"),
        h("p", { class: "muted" }, `${filename}${info.sheet ? ` · sheet "${info.sheet}"` : ""} · ${whole.format(info.total_rows)} rows. `,
          saved ? "We've reused the matching you chose last time for a file like this." : "We've guessed from the column names. Check and adjust."),
        h("div", { class: "map-grid" }, selects,
          h("label", { class: "field", for: "date-order" }, "Date format", orderSel)),
        info.date_order_ambiguous && !saved ? h("div", { class: "note", role: "note" },
          "Dates like 03/05/2026 could mean March 5 or 3 May, and nothing in this file settles it. Check the date format above.") : null),
      spacer(16), h("h2", {}, "First rows of your file"), previewTable, spacer(16),
      err, spacer(8),
      h("div", { class: "row" },
        h("label", { class: "row", for: "run-detect" }, detect, "Run detection after importing"),
        h("span", { class: "spacer" }),
        h("button", { class: "btn", onclick: () => chooseStep() }, "Choose a different file"), go));
  }

  function resultStep(filename, r) {
    const kpi = (label, value, alert) => h("div", { class: "card kpi result-kpis" + (alert ? " alert" : "") },
      h("div", { class: "label" }, label), h("div", { class: "value" }, whole.format(value)));
    put(holder, steps(3),
      h("div", { class: "kpis" }, kpi("Invoices added", r.added), kpi("Already stored, skipped", r.already_stored),
        kpi("Rows with problems", r.error_count, r.error_count > 0),
        r.detection ? kpi("Flagged from this file", r.detection.flagged_from_file, r.detection.flagged_from_file > 0) : null),
      r.detection ? h("div", { class: "row" }, h("a", { class: "btn primary", href: "#/review" }, "Open review queue"),
        h("button", { class: "btn", onclick: () => chooseStep() }, "Upload another file"))
        : h("div", { class: "row" }, h("button", { class: "btn", onclick: () => chooseStep() }, "Upload another file")),
      r.blank_rows ? h("div", {}, spacer(8), h("p", { class: "muted small" }, `${whole.format(r.blank_rows)} blank or total rows were ignored.`)) : null,
      r.errors.length ? h("div", {}, spacer(16), h("h2", {}, "Rows that were not imported"),
        h("p", { class: "muted" }, "Fix these in your file and upload it again. Rows already imported are skipped automatically."),
        h("div", { class: "table-wrap" }, h("table", {},
          h("thead", {}, h("tr", {}, h("th", { class: "num" }, "Row"), h("th", {}, "Problem"))),
          h("tbody", {}, r.errors.map((e) => h("tr", {}, h("td", { class: "num" }, e.row), h("td", {}, e.message))))))) : null);
    toast(`Imported ${whole.format(r.added)} invoices from ${filename}`);
  }

  chooseStep();
  return h("div", {}, pageHead("Upload invoices", "Add invoices from an Excel or CSV export."), holder);
}

/* ------------------------------------------------------------ invoices */
async function invoicesView() {
  let sort = "invoice_date";
  let query = "";
  const isAdmin = session.role === "admin";
  const body = h("tbody");
  const note = h("p", { class: "muted small" });
  let rows = [];
  async function load() {
    rows = await api("GET", `/invoices?sort=${encodeURIComponent(sort)}&limit=500`);
    draw();
  }
  function draw() {
    const q = query.toLowerCase();
    const shown = rows.filter((r) => !q || (r.id + " " + r.vendor).toLowerCase().includes(q));
    put(body, ...shown.map((r) => {
      const bankCell = h("td", {}, isAdmin ? h("button", { class: "btn link small", onclick: async () => {
        try {
          const b = await api("GET", `/invoices/${encodeURIComponent(r.id)}/bank`);
          put(bankCell, h("span", { class: "mono" }, b.bank_account || "none on file"));
        } catch (err) { toast(err.message, true); }
      } }, "Show") : h("span", { class: "muted small" }, "admins only"));
      return h("tr", {}, h("td", { class: "mono" }, r.id), h("td", {}, r.vendor),
        h("td", { class: "num" }, fmtAmount(r.amount)), h("td", {}, fmtDate(r.invoice_date)), bankCell);
    }));
    note.textContent = rows.length === 500 ? "Showing the first 500 invoices in this order." : `${whole.format(rows.length)} invoices.`;
  }
  const sortSel = h("select", { "aria-label": "Sort by", onchange: async () => { sort = sortSel.value; await load(); } },
    h("option", { value: "invoice_date" }, "Sort by date"), h("option", { value: "amount" }, "Sort by amount"),
    h("option", { value: "vendor" }, "Sort by vendor"), h("option", { value: "id" }, "Sort by invoice number"));
  await load();
  return h("div", {}, pageHead("Invoices", "Everything stored for your company. Bank accounts are encrypted; viewing one is logged."),
    h("div", { class: "row" }, sortSel, h("span", { class: "spacer" }),
      h("input", { class: "search", type: "search", placeholder: "Search invoice or vendor", "aria-label": "Search invoices",
        oninput: (e) => { query = e.target.value; draw(); } })),
    spacer(12),
    h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "Invoice"), h("th", {}, "Vendor"), h("th", { class: "num" }, "Amount"),
        h("th", {}, "Date"), h("th", {}, "Bank account"))), body)),
    spacer(8), note);
}

/* ------------------------------------------------------------ activity log */
async function auditView() {
  const rows = await api("GET", "/audit?limit=300");
  return h("div", {}, pageHead("Activity log", "Who did what in your company. Entries can't be edited or deleted."),
    h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "When"), h("th", {}, "Who"), h("th", {}, "What"), h("th", {}, "Details"))),
      h("tbody", {}, rows.map((r) => h("tr", {}, h("td", {}, fmtDate(r.at)), h("td", {}, r.username),
        h("td", {}, ACTIONS[r.action] || r.action), h("td", { class: "muted" }, r.detail)))))));
}

/* ------------------------------------------------------------ settings */
async function settingsView() {
  const isAdmin = session.role === "admin";
  const cfg = await api("GET", "/settings");
  const threshold = h("input", { id: "threshold", type: "number", min: "1", step: "1", value: cfg.approval_threshold, disabled: !isAdmin });
  const company = h("div", { class: "card" }, h("h2", {}, "Company settings"),
    h("form", { class: "form", onsubmit: async (e) => {
      e.preventDefault();
      try {
        await api("PUT", "/settings", { approval_threshold: Number(threshold.value) });
        toast("Saved. Run detection to apply it to existing invoices.");
      } catch (err) { toast(err.message, true); }
    } },
      h("label", { class: "field", for: "threshold" }, "Approval threshold", threshold),
      h("p", { class: "muted small" }, "Invoices at or above this amount need extra sign-off at your company. Used to spot invoices just under the limit and bills split to avoid it."),
      isAdmin ? h("div", {}, h("button", { class: "btn primary", type: "submit" }, "Save")) : h("p", { class: "muted small" }, "Only admins can change this.")));

  const you = h("div", { class: "card" }, h("h2", {}, "Your account"),
    h("p", {}, h("strong", {}, session.username), ` · ${session.role} · ${session.company}`),
    h("p", { class: "muted small" }, roleHelp(session.role)));

  if (!isAdmin) return h("div", {}, pageHead("Settings"), h("div", { class: "grid grid-2" }, company, you));

  const users = await api("GET", "/users");
  const name = h("input", { id: "new-username", autocomplete: "off", required: true, placeholder: "e.g. maria@company.com" });
  const pw = h("input", { id: "new-password", type: "password", autocomplete: "new-password", required: true, minlength: "12" });
  const role = h("select", { id: "new-role" }, h("option", { value: "accountant" }, "Accountant"),
    h("option", { value: "auditor" }, "Auditor"), h("option", { value: "admin" }, "Admin"));
  const err = h("div", { class: "error-box hidden", role: "alert" });
  const team = h("div", { class: "card" }, h("h2", {}, "People in your company"),
    h("div", { class: "table-wrap" }, h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Username"), h("th", {}, "Role"))),
      h("tbody", {}, users.map((u) => h("tr", {}, h("td", {}, u.username), h("td", {}, u.role)))))),
    spacer(16), h("h2", {}, "Add a person"),
    h("form", { class: "form", onsubmit: async (e) => {
      e.preventDefault();
      err.classList.add("hidden");
      try {
        await api("POST", "/users", { username: name.value.trim(), password: pw.value, role: role.value });
        toast(`Added ${name.value.trim()}`);
        render();
      } catch (ex) { err.textContent = ex.message; err.classList.remove("hidden"); }
    } },
      h("label", { class: "field", for: "new-username" }, "Username", name),
      h("label", { class: "field", for: "new-password" }, "Temporary password (12+ characters)", pw),
      h("label", { class: "field", for: "new-role" }, "Role", role),
      h("p", { class: "muted small" }, roleHelp(null)),
      err, h("div", {}, h("button", { class: "btn primary", type: "submit" }, "Add person"))));
  return h("div", {}, pageHead("Settings"), h("div", { class: "grid grid-2" }, h("div", { class: "grid" }, company, you), team));
}

function roleHelp(role) {
  const help = {
    admin: "Admins can do everything, including managing people, settings and viewing bank accounts.",
    accountant: "Accountants can upload invoices, run detection and review flags.",
    auditor: "Auditors can see everything except bank accounts, but can't change anything.",
  };
  return role ? help[role] : Object.values(help).join(" ");
}

/* ------------------------------------------------------------ start */
window.addEventListener("hashchange", render);
document.addEventListener("DOMContentLoaded", async () => {
  try { session = JSON.parse(store.get("session") || "null"); } catch { session = null; }
  if (session) await refreshCount();
  render();
});
