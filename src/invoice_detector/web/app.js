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
  totals_mismatch: ["Totals don't add up", "The invoice's line items, subtotal, tax and total don't agree", "mid"],
  credited_invoice: ["Cancelled by a credit note", "A credit note for the same vendor and amount cancels this invoice", "high"],
  matched_credit: ["Cancels an invoice", "This credit note cancels a specific invoice in full", "mid"],
  unmatched_credit: ["Credit with no matching invoices", "Credit note from a vendor with nothing, or less, billed", "mid"],
  duplicate_credit: ["Duplicate credit note", "Same vendor, amount and date as another credit note", "mid"],
  reissued_invoice: ["Replaces a cancelled invoice", "Same vendor and amount as an invoice a credit note cancelled", "low"],
};

/* A small tag after the number of a credit note (its amount is shown negative). */
const kindTag = (kind) => (kind === "credit" ? [" ", h("span", { class: "pill credit" }, "Credit note")] : null);
const ACTIONS = {
  login: "Signed in", login_failed: "Failed sign-in", file_imported: "Imported a file",
  documents_imported: "Imported invoice documents", ai_document_read: "Sent a document for AI reading",
  logout: "Signed out", access_removed: "Removed a person's access", access_restored: "Gave a person access back",
  ai_summary: "Asked for an AI summary",
  report_exported: "Downloaded a report", workbook_exported: "Downloaded a checked workbook",
  invoices_imported: "Imported invoices", detection_run: "Ran detection", invoice_reviewed: "Reviewed an invoice",
  bank_account_viewed: "Viewed a bank account", user_created: "Added a user", settings_changed: "Changed settings",
  two_factor_enabled: "Turned on two-factor", two_factor_disabled: "Turned off two-factor",
  recovery_codes_replaced: "Made new recovery codes", password_changed: "Changed password",
  password_reset: "Reset a password", password_reset_requested: "Asked for a reset link",
};
const CAN_EDIT = ["admin", "accountant"];
const DECISIONS = {
  do_not_pay: ["Do not pay", "Confirmed problem. Don't pay this invoice."],
  hold: ["Hold", "Confirmed problem. Check it before paying."],
  review: ["Review first", "Flagged, not reviewed yet."],
  pay: ["Pay", "No problem found."],
};

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
// Who is signed in. The sign-in itself is an HttpOnly cookie that this script can't read
// (so an injected script couldn't steal it either); this is only what the page shows.
let session = null; // { username, role, company, needs2fa }
// Requests where a 401 means "wrong password or code", not "session expired".
const AUTH_PATHS = ["/login", "/password/", "/me/password", "/me/2fa/disable", "/me/2fa/recovery-codes"];

/* Load who is signed in (the server knows from the session cookie). */
async function loadSession() {
  const me = await api("GET", "/me");
  session = { username: me.username, role: me.role, company: me.company,
    needs2fa: me.two_factor_required && !me.signed_in_with_two_factor };
}

/* Finish signing in: the server has set the session cookie. */
async function startSession() {
  await loadSession();
  await refreshCount();
  go(session.needs2fa ? "#/security" : "#/dashboard");
}

/* Go to a page. Changing the address fires "hashchange", which redraws; only redraw here
 * when the address is already right. (Doing both would redraw twice, and the second
 * redraw would wipe anything typed in between.) */
function go(hash) {
  if (location.hash === hash) render();
  else location.hash = hash;
}

function hashParams() {
  const q = location.hash.split("?")[1] || "";
  return new URLSearchParams(q);
}

class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

// Sent with every request. Other sites can't add this header to a forged form, and the
// server refuses cookie-signed changes without it.
const BASE_HEADERS = { "X-Requested-With": "fetch" };

async function api(method, path, body) {
  const headers = { ...BASE_HEADERS, Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let res;
  try {
    res = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  } catch {
    throw new ApiError(0, "Can't reach the server. Is it still running?");
  }
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (res.status === 401 && session && !AUTH_PATHS.some((p) => path.startsWith(p))) {
    signOut("Your session expired. Please sign in again.");
    throw new ApiError(401, "Signed out");
  }
  if (res.status === 403 && res.headers.get("X-Requires-2FA") && session) {
    session.needs2fa = true;
    if (location.hash !== "#/security") location.hash = "#/security";
    throw new ApiError(403, "Your company requires two-factor authentication. Set it up to continue.");
  }
  if (!res.ok) {
    let msg = data && data.detail;
    if (Array.isArray(msg)) msg = msg.map((d) => `${(d.loc || []).slice(1).join(".")}: ${d.msg}`).join("; ");
    throw new ApiError(res.status, msg || `Request failed (${res.status})`);
  }
  return data;
}

/* Fetch a file with the sign-in token and hand it to the browser as a download. */
async function download(method, path, body) {
  const headers = { ...BASE_HEADERS };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let res;
  try {
    res = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  } catch {
    throw new ApiError(0, "Can't reach the server. Is it still running?");
  }
  if (res.status === 401 && session) { signOut("Your session expired. Please sign in again."); throw new ApiError(401, "Signed out"); }
  if (!res.ok) {
    let msg = `Download failed (${res.status})`;
    try { const d = await res.json(); if (typeof d.detail === "string") msg = d.detail; } catch { /* not JSON */ }
    throw new ApiError(res.status, msg);
  }
  const blob = await res.blob();
  const cd = res.headers.get("Content-Disposition") || "";
  const star = /filename\*=UTF-8''([^;]+)/i.exec(cd);
  const plain = /filename="([^"]+)"/i.exec(cd);
  let name = "download.xlsx";
  try { name = star ? decodeURIComponent(star[1]) : plain ? plain[1] : name; } catch { name = plain ? plain[1] : name; }
  const url = URL.createObjectURL(blob);
  const a = h("a", { href: url, download: name, class: "hidden" });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
  return name;
}

function downloadButton(label, method, path, body, cls) {
  const btn = h("button", { class: "btn" + (cls ? " " + cls : ""), onclick: async () => {
    btn.disabled = true;
    const text = btn.textContent;
    btn.textContent = "Preparing…";
    try {
      const name = await download(method, path, typeof body === "function" ? body() : body);
      toast(`Downloaded ${name}`);
    } catch (err) { if (err.status !== 401) toast(err.message, true); }
    btn.disabled = false;
    btn.textContent = text;
  } }, label);
  return btn;
}

function signOut(message) {
  session = null;
  fetch("/logout", { method: "POST", headers: BASE_HEADERS }).catch(() => {});
  if (message) toast(message, true);
  go("#/login");
}

/* ------------------------------------------------------------ shell */
const PAGES = [
  { id: "dashboard", label: "Dashboard", view: dashboardView },
  { id: "review", label: "Review queue", view: reviewView },
  { id: "upload", label: "Upload invoices", view: uploadView, roles: CAN_EDIT },
  { id: "invoices", label: "Invoices", view: invoicesView },
  { id: "audit", label: "Activity log", view: auditView, roles: ["admin", "auditor"] },
  { id: "settings", label: "Settings", view: settingsView },
  { id: "security", label: "Your account", view: securityView },
];
let openCount = null;

function shell(active, content) {
  const nav = h("nav", { class: "nav", "aria-label": "Main" },
    PAGES.filter((p) => !p.roles || p.roles.includes(session.role)).map((p) =>
      h("a", { href: "#/" + p.id, class: p.id === active ? "active" : null,
               "aria-current": p.id === active ? "page" : null,
               // the address doesn't change when you click the page you're on, so start it afresh
               onclick: (e) => { if (location.hash === "#/" + p.id) { e.preventDefault(); render(); } } },
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
    document.title = "Sign in · Invoice Review";
    put(root, route === "forgot" ? forgotView() : route === "reset" ? resetView() : loginView());
    return;
  }
  if (session.needs2fa && route !== "security") { location.hash = "#/security"; return; }
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
function authCard(subtitle, ...content) {
  return h("div", { class: "login-wrap" },
    h("div", { class: "card login" },
      h("div", { class: "brand" }, h("div", { class: "logo", "aria-hidden": "true" }, "IR"),
        h("div", {}, h("div", { class: "brand-name" }, "Invoice Review"),
          h("div", { class: "brand-org" }, subtitle))),
      content));
}

function loginView() {
  const err = h("div", { class: "error-box hidden", role: "alert" });
  // A message from a failed single sign-on arrives as a token signed by the server. Only
  // text the server vouches for is shown: a crafted link can't make this page say anything.
  const notice = hashParams().get("notice");
  if (notice) {
    api("GET", "/auth/notice?token=" + encodeURIComponent(notice))
      .then((r) => { err.textContent = r.message; err.classList.remove("hidden"); })
      .catch(() => {});
  }
  const user = h("input", { id: "username", name: "username", autocomplete: "username", required: true, autofocus: true });
  const pass = h("input", { id: "password", name: "password", type: "password", autocomplete: "current-password", required: true });
  const submit = h("button", { class: "btn primary", type: "submit" }, "Sign in");
  const ssoBox = h("div", { class: "sso" });
  const body = h("div", { class: "form" });

  function fail(ex) {
    err.textContent = ex.message;
    err.classList.remove("hidden");
  }

  function codeStep(mfaToken) {
    // Second step: a code from the authenticator app, or a recovery code.
    const code = h("input", { id: "code", name: "code", autocomplete: "one-time-code", inputmode: "text",
      required: true, autofocus: true, maxlength: "40", placeholder: "123 456" });
    const go = h("button", { class: "btn primary", type: "submit" }, "Verify");
    err.classList.add("hidden");
    put(body, h("form", { class: "form", onsubmit: async (e) => {
      e.preventDefault();
      go.disabled = true;
      err.classList.add("hidden");
      try {
        const r = await api("POST", "/login/2fa", { mfa_token: mfaToken, code: code.value.trim() });
        if (r.recovery_codes_left !== undefined && r.recovery_codes_left < 3) {
          toast(`Only ${r.recovery_codes_left} recovery codes left. Make new ones under Your account.`, true);
        }
        await startSession();
      } catch (ex) {
        fail(ex);
        go.disabled = false;
        if (/timed out/i.test(ex.message)) setTimeout(() => render(), 2500);
      }
    } },
      h("p", {}, "Enter the 6-digit code from your authenticator app."),
      h("label", { class: "field", for: "code" }, "Code", code),
      h("p", { class: "muted small" }, "Lost your phone? Enter one of your recovery codes instead."),
      err, go,
      h("button", { class: "btn link small", type: "button", onclick: () => render() }, "Back")));
    code.focus();
  }

  const form = h("form", { class: "form", onsubmit: async (e) => {
    e.preventDefault();
    submit.disabled = true;
    err.classList.add("hidden");
    try {
      const r = await api("POST", "/login", { username: user.value.trim(), password: pass.value });
      if (r.mfa_required) { codeStep(r.mfa_token); return; }
      await startSession();
    } catch (ex) {
      session = null;
      fail(ex);
      submit.disabled = false;
    }
  } },
    h("label", { class: "field", for: "username" }, "Username or email", user),
    h("label", { class: "field", for: "password" }, "Password", pass),
    err, submit,
    h("a", { class: "small", href: "#/forgot" }, "Forgot your password?"));
  put(body, ssoBox, form);

  api("GET", "/auth/providers").then((providers) => {
    if (!providers.length) return;
    put(ssoBox, providers.map((p) => h("a", { class: "btn sso-btn", href: `/auth/${encodeURIComponent(p.name)}/start` },
      `Sign in with ${p.label}`)), h("div", { class: "divider" }, h("span", {}, "or")));
  }).catch(() => { /* sign-in still works without the buttons */ });

  return authCard("Sign in to your company", body,
    h("p", { class: "muted small" }, "Flags are signals for review, not proof of fraud."));
}

function forgotView() {
  const user = h("input", { id: "username", autocomplete: "username", required: true, autofocus: true, placeholder: "you@company.com" });
  const err = h("div", { class: "error-box hidden", role: "alert" });
  const box = h("div", { class: "form" });
  const go = h("button", { class: "btn primary", type: "submit" }, "Email me a reset link");
  put(box, h("form", { class: "form", onsubmit: async (e) => {
    e.preventDefault();
    go.disabled = true;
    try {
      const r = await api("POST", "/password/forgot", { username: user.value.trim() });
      put(box, r.email_enabled
        ? h("div", { class: "note", role: "status" }, "If that account exists and its username is an email address, we've sent a link to it. The link works for 30 minutes.")
        : h("div", { class: "note", role: "status" }, "Password reset by email isn't set up on this server. Ask your company's admin to give you a temporary password."),
        h("a", { href: "#/login" }, "Back to sign in"));
    } catch (ex) { err.textContent = ex.message; err.classList.remove("hidden"); go.disabled = false; }
  } },
    h("p", {}, "Enter the email address you sign in with."),
    h("label", { class: "field", for: "username" }, "Email", user), err, go,
    h("a", { class: "small", href: "#/login" }, "Back to sign in")));
  return authCard("Reset your password", box);
}

function resetView() {
  const token = hashParams().get("token") || "";
  const pw = h("input", { id: "new-password", type: "password", autocomplete: "new-password", required: true, minlength: "12", autofocus: true });
  const again = h("input", { id: "new-password-again", type: "password", autocomplete: "new-password", required: true });
  const err = h("div", { class: "error-box hidden", role: "alert" });
  const box = h("div", { class: "form" });
  const go = h("button", { class: "btn primary", type: "submit" }, "Set new password");
  put(box, h("form", { class: "form", onsubmit: async (e) => {
    e.preventDefault();
    err.classList.add("hidden");
    if (pw.value !== again.value) { err.textContent = "The two passwords don't match."; err.classList.remove("hidden"); return; }
    go.disabled = true;
    try {
      await api("POST", "/password/reset", { token, new_password: pw.value });
      history.replaceState(null, "", "#/login");
      put(box, h("div", { class: "note", role: "status" }, "Your password has been changed, and any other sessions were signed out."),
        h("a", { class: "btn primary", href: "#/login" }, "Sign in"));
    } catch (ex) { err.textContent = ex.message; err.classList.remove("hidden"); go.disabled = false; }
  } },
    h("label", { class: "field", for: "new-password" }, "New password (12+ characters)", pw),
    h("label", { class: "field", for: "new-password-again" }, "Type it again", again), err, go));
  return authCard("Choose a new password", box);
}

/* ------------------------------------------------------------ your account */
function downloadText(name, text) {
  const url = URL.createObjectURL(new Blob([text], { type: "text/plain" }));
  const a = h("a", { href: url, download: name, class: "hidden" });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}

function recoveryCodesBox(codes, onDone) {
  return h("div", { class: "card recovery" },
    h("h2", {}, "Save your recovery codes"),
    h("p", {}, "If you lose your phone, each of these codes lets you sign in once. Keep them somewhere safe, such as a password manager. They won't be shown again."),
    h("ol", { class: "codes mono" }, codes.map((c) => h("li", {}, c))),
    h("div", { class: "row" },
      h("button", { class: "btn", onclick: () => downloadText("invoice-review-recovery-codes.txt",
        `Invoice Review recovery codes for ${session.username}\nEach code works once.\n\n${codes.join("\n")}\n`) }, "Download as a text file"),
      h("button", { class: "btn primary", onclick: onDone }, "I've saved them")));
}

async function securityView() {
  const [me, tf] = await Promise.all([api("GET", "/me"), api("GET", "/me/2fa")]);

  // The server replaced the session cookie (new sign-in level or new password).
  function updateSession(needs2fa) { session.needs2fa = needs2fa; }

  // ----- two-factor -----
  const twoFactor = h("div", { class: "card" });
  function drawTwoFactor(state) {
    const err = h("div", { class: "error-box hidden", role: "alert" });
    const show = (ex) => { err.textContent = ex.message; err.classList.remove("hidden"); };
    if (state.enabled) {
      const code = h("input", { id: "tf-code", autocomplete: "one-time-code", maxlength: "40", placeholder: "Code from the app" });
      const password = me.sso_only ? null : h("input", { id: "tf-password", type: "password", autocomplete: "current-password", placeholder: "Your password" });
      put(twoFactor, h("h2", {}, "Two-factor authentication"),
        h("p", {}, h("span", { class: "pill d-pay" }, "On"), ` · ${state.recovery_codes_left} recovery code${state.recovery_codes_left === 1 ? "" : "s"} left`),
        h("p", { class: "muted small" }, "Signing in asks for a code from your authenticator app after your password."),
        h("div", { class: "form" }, password ? h("label", { class: "field", for: "tf-password" }, "Password", password) : null,
          h("label", { class: "field", for: "tf-code" }, "Code from your app", code), err,
          h("div", { class: "row" },
            h("button", { class: "btn", onclick: async () => {
              try {
                const r = await api("POST", "/me/2fa/recovery-codes", { code: code.value.trim() });
                put(twoFactor, recoveryCodesBox(r.recovery_codes, () => render()));
              } catch (ex) { show(ex); }
            } }, "Make new recovery codes"),
            state.required_by_company ? h("span", { class: "muted small" }, "Your company requires two-factor, so it can't be turned off.")
              : h("button", { class: "btn danger", onclick: async () => {
                try {
                  await api("POST", "/me/2fa/disable", { password: password ? password.value : null, code: code.value.trim() });
                  updateSession(false);
                  toast("Two-factor authentication is off");
                  render();
                } catch (ex) { show(ex); }
              } }, "Turn off"))));
      return;
    }
    put(twoFactor, h("h2", {}, "Two-factor authentication"),
      state.required_by_company ? h("div", { class: "note", role: "note" }, "Your company requires two-factor authentication. Set it up to continue using the app.") : null,
      h("p", {}, "Protect your account with a code from an authenticator app (Microsoft Authenticator, Google Authenticator, 1Password, Authy and others) as well as your password."),
      err,
      h("button", { class: "btn primary", onclick: async () => {
        try { setupStep(await api("POST", "/me/2fa/setup")); } catch (ex) { show(ex); }
      } }, "Set up two-factor"));
  }

  function setupStep(setup) {
    const err = h("div", { class: "error-box hidden", role: "alert" });
    const code = h("input", { id: "tf-confirm", autocomplete: "one-time-code", inputmode: "numeric", maxlength: "7", placeholder: "123456", autofocus: true });
    const qr = h("img", { src: setup.qr, alt: "QR code for your authenticator app", class: "qr", width: "228", height: "228" });
    put(twoFactor, h("h2", {}, "Set up two-factor authentication"),
      h("ol", { class: "steps-list" },
        h("li", {}, "Open your authenticator app and add an account."),
        h("li", {}, "Scan this QR code. On a phone, or if you can't scan, type the key below instead."),
        h("li", {}, "Enter the 6-digit code the app shows.")),
      h("div", { class: "setup-grid" }, qr,
        h("div", {}, h("div", { class: "muted small" }, "Key"), h("div", { class: "mono secret", id: "tf-secret" }, setup.secret),
          h("a", { class: "small", href: setup.uri }, "Open in an authenticator app on this device"))),
      h("form", { class: "form", onsubmit: async (e) => {
        e.preventDefault();
        err.classList.add("hidden");
        try {
          const r = await api("POST", "/me/2fa/enable", { code: code.value.trim() });
          updateSession(false);
          put(twoFactor, recoveryCodesBox(r.recovery_codes, () => { toast("Two-factor authentication is on"); render(); }));
        } catch (ex) { err.textContent = ex.message; err.classList.remove("hidden"); }
      } }, h("label", { class: "field", for: "tf-confirm" }, "Code from the app", code), err,
        h("div", { class: "row" }, h("button", { class: "btn primary", type: "submit" }, "Turn on"),
          h("button", { class: "btn", type: "button", onclick: () => drawTwoFactor(tf) }, "Cancel"))));
  }
  drawTwoFactor(tf);

  // ----- password -----
  const passwordCard = h("div", { class: "card" }, h("h2", {}, "Password"));
  if (me.sso_only) {
    passwordCard.append(h("p", {}, "You sign in with single sign-on, so there's no password to manage here."));
  } else {
    const cur = h("input", { id: "pw-current", type: "password", autocomplete: "current-password", required: true });
    const nw = h("input", { id: "pw-new", type: "password", autocomplete: "new-password", required: true, minlength: "12" });
    const again = h("input", { id: "pw-again", type: "password", autocomplete: "new-password", required: true });
    const err = h("div", { class: "error-box hidden", role: "alert" });
    passwordCard.append(h("form", { class: "form", onsubmit: async (e) => {
      e.preventDefault();
      err.classList.add("hidden");
      if (nw.value !== again.value) { err.textContent = "The two new passwords don't match."; err.classList.remove("hidden"); return; }
      try {
        await api("POST", "/me/password", { current_password: cur.value, new_password: nw.value });
        updateSession(session.needs2fa);
        cur.value = nw.value = again.value = "";
        toast("Password changed. Any other sessions were signed out.");
      } catch (ex) { err.textContent = ex.message; err.classList.remove("hidden"); }
    } },
      h("label", { class: "field", for: "pw-current" }, "Current password", cur),
      h("label", { class: "field", for: "pw-new" }, "New password (12+ characters)", nw),
      h("label", { class: "field", for: "pw-again" }, "New password again", again), err,
      h("div", {}, h("button", { class: "btn primary", type: "submit" }, "Change password"))));
  }

  return h("div", {}, pageHead("Your account", `${me.username} · ${me.role} · ${me.company}`),
    h("div", { class: "grid grid-2" }, twoFactor, passwordCard));
}

/* ------------------------------------------------------------ dashboard */
async function dashboardView() {
  const [s, m] = await Promise.all([api("GET", "/summary"), api("GET", "/metrics")]);
  openCount = s.open;
  const canEdit = CAN_EDIT.includes(session.role);
  const head = pageHead("Dashboard", s.company,
    s.flagged ? downloadButton("Download Excel report", "GET", "/export/review.xlsx") : null,
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
    kpi("Flag accuracy", m.precision === null ? "Not yet" : Math.round(m.precision * 100) + "%",
      m.reviewed ? `from ${whole.format(m.reviewed)} review${m.reviewed === 1 ? "" : "s"}` : "after your first reviews"));

  // Payments: how the money splits by decision, as a bar over a small ledger.
  const order = [["pay", "Ready to pay"], ["review", "Review first"], ["hold", "On hold"], ["do_not_pay", "Stopped"]];
  const totalAmount = order.reduce((t, [d]) => t + (s.amounts[d] || 0), 0);
  const totalCount = order.reduce((t, [d]) => t + (s.decisions[d] || 0), 0);
  const stack = h("div", { class: "stack", role: "img",
    "aria-label": order.map(([d, label]) => `${label} ${fmtAmount(s.amounts[d])}`).join(", ") },
  order.filter(([d]) => s.amounts[d] > 0).map(([d, label]) => {
    const positive = order.reduce((t, [x]) => t + Math.max(0, s.amounts[x] || 0), 0);   // credits can make a row negative
    const seg = h("span", { class: "d-" + d, title: `${label}: ${fmtAmount(s.amounts[d])}` });
    seg.style.width = (100 * s.amounts[d] / (positive || 1)) + "%";
    return seg;
  }));
  const cn = s.credit_notes || { count: 0 };
  const creditLine = cn.count ? h("p", { class: "muted small", id: "credit-line" },
    `Includes ${whole.format(cn.count)} credit note${cn.count === 1 ? "" : "s"} worth ${fmtAmount(cn.amount)}. ` +
    `Amounts are net: ${fmtAmount(cn.to_apply)} of credit is ready to deduct from what those vendors are paid.`) : null;
  const ledger = h("div", { class: "table-wrap" }, h("table", { class: "ledger" },
    h("thead", {}, h("tr", {}, h("th", {}, "Decision"), h("th", { class: "num" }, "Invoices"),
      h("th", { class: "num" }, "Amount"), h("th", { class: "meaning" }, "Meaning"))),
    h("tbody", {}, order.map(([d, label]) => h("tr", { class: d === "do_not_pay" && s.amounts[d] > 0 ? "stop" : "" },
      h("td", {}, h("span", { class: "swatch d-" + d }), label),
      h("td", { class: "num" }, whole.format(s.decisions[d])),
      h("td", { class: "num" }, fmtAmount(s.amounts[d])),
      h("td", { class: "meaning muted" }, DECISIONS[d][1])))),
    h("tfoot", {}, h("tr", {}, h("td", {}, "Total"), h("td", { class: "num" }, whole.format(totalCount)),
      h("td", { class: "num" }, fmtAmount(totalAmount)), h("td", { class: "meaning" })))));
  const payments = h("section", { class: "card payments" },
    h("div", { class: "section-head" }, h("h2", {}, "Payments"), h("span", { class: "spacer" }),
      downloadButton("Download payment file", "GET", "/export/payment-file.xlsx", undefined, "sm")),
    stack, ledger, creditLine ? spacer(8) : null, creditLine);
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
  return h("div", {}, head, kpis, payments, h("div", { class: "grid grid-2" }, reasons, perf), spacer(12), last);
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
  const expanded = new Set(); // rows the user has opened stay open after a decision

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

  function decisionPill(item) {
    const [text, help] = DECISIONS[item.decision] || [item.decision, ""];
    return h("span", { class: "pill d-" + item.decision, title: help }, text);
  }

  async function send(item, body, message) {
    const r = await api("POST", "/review/" + encodeURIComponent(item.invoice_id), body);
    if (item.status !== body.verdict) {
      counts[item.status] -= 1;
      counts[body.verdict] += 1;
    }
    item.status = body.verdict;
    item.action = r.action;
    item.decision = body.verdict === "dismissed" ? "pay" : r.action;
    if (body.note !== undefined) item.note = body.note || null;
    openCount = counts.open;
    toast(message(item));
    updateTabs();
    drawRows();
    updateNavCount();
  }

  function actionsFor(item) {
    if (!canEdit) return decisionPill(item);
    const decide = (verdict) => async (e) => {
      e.stopPropagation();
      e.currentTarget.disabled = true;
      try {
        await send(item, { verdict, note: item.note || undefined }, (it) => verdict === "confirmed"
          ? `${it.invoice_id} confirmed: ${DECISIONS[it.decision][0].toLowerCase()}`
          : `${it.invoice_id} dismissed as a false alarm`);
      } catch (err) {
        toast(err.message, true);
        e.currentTarget.disabled = false;
      }
    };
    if (item.status === "open") {
      return h("div", { class: "row" },
        h("button", { class: "btn sm danger", onclick: decide("confirmed"), "aria-label": `Confirm ${item.invoice_id} is a problem`,
          title: `Confirming marks it: ${DECISIONS[item.suggested_action][0]}` }, "Confirm"),
        h("button", { class: "btn sm", onclick: decide("dismissed"), "aria-label": `Dismiss ${item.invoice_id} as a false alarm` }, "Dismiss"));
    }
    const other = item.status === "confirmed" ? "dismissed" : "confirmed";
    return h("div", { class: "row" }, decisionPill(item),
      h("button", { class: "btn link small", onclick: decide(other) }, other === "confirmed" ? "Confirm instead" : "Dismiss instead"));
  }

  function decisionEditor(item) {
    // Only for confirmed problems: choose what happens to the payment and leave a note.
    const sel = h("select", { "aria-label": `Payment decision for ${item.invoice_id}` },
      h("option", { value: "do_not_pay" }, "Do not pay"), h("option", { value: "hold" }, "Hold until checked"),
      h("option", { value: "pay" }, "Pay anyway (checked, it's fine)"));
    sel.value = item.action || item.suggested_action;
    const note = h("input", { type: "text", maxlength: "500", placeholder: "Note, e.g. \"Called vendor 12 Nov, bank change is genuine\"",
      "aria-label": `Note for ${item.invoice_id}`, value: item.note || "" });
    const save = h("button", { class: "btn sm primary", onclick: async () => {
      save.disabled = true;
      try {
        await send(item, { verdict: "confirmed", action: sel.value, note: note.value },
          (it) => `${it.invoice_id}: ${DECISIONS[it.decision][0].toLowerCase()}${it.note ? ", note saved" : ""}`);
      } catch (err) { toast(err.message, true); save.disabled = false; }
    } }, "Save decision");
    return h("div", { class: "editor" }, h("strong", {}, "Payment decision"),
      h("div", { class: "row" }, sel, note, save));
  }

  function addRow(item) {
    const level = riskClass(item.risk);
    const fill = h("div", { class: "fill" });
    fill.style.width = item.risk + "%";
    const details = h("tr", { class: "details hidden" }, h("td", { colspan: 7 },
      h("div", { class: "detail-grid" },
        h("div", {}, h("strong", {}, "Why it was flagged"),
          h("ul", {}, item.details.map((d, i) => h("li", {}, h("strong", {}, ruleLabel(item.rules[i]) + ": "), d)))),
        h("div", {}, h("strong", {}, "What to do"),
          h("ul", { class: "fixes" }, (item.fixes || []).map((f) => h("li", {}, f))),
          item.status === "open" ? h("p", { class: "muted small" },
            `Confirming marks it “${DECISIONS[item.suggested_action][0]}”. You can change that afterwards.`) : null,
          item.note && !(canEdit && item.status === "confirmed") ? h("p", { class: "small" }, h("strong", {}, "Note: "), item.note) : null)),
      canEdit && item.status === "confirmed" ? decisionEditor(item) : null));
    const row = h("tr", { class: "clickable", tabindex: 0, "aria-expanded": "false",
      onclick: () => toggle(), onkeydown: (e) => { if (e.target === row && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); toggle(); } } },
      h("td", { class: "c-risk" }, h("span", { class: "risk " + level, title: `Risk ${item.risk} of 100` },
        h("span", { class: "n" }, item.risk), h("span", { class: "bar" }, fill))),
      h("td", { class: "mono c-id" }, item.invoice_id, kindTag(item.kind)),
      h("td", { class: "c-vendor" }, item.vendor),
      h("td", { class: "num c-amount" }, fmtAmount(item.amount)),
      h("td", { class: "c-date" }, fmtDate(item.date)),
      h("td", { class: "c-why" }, h("div", { class: "chips" }, item.rules.map((r) =>
        h("span", { class: "chip " + ruleLevel(r), title: (RULES[r] || [, ""])[1] }, ruleLabel(r))))),
      h("td", { class: "c-act", onclick: (e) => e.stopPropagation() }, actionsFor(item)));
    function toggle() {
      const open = details.classList.toggle("hidden") === false;
      row.setAttribute("aria-expanded", String(open));
      if (open) expanded.add(item.invoice_id); else expanded.delete(item.invoice_id);
    }
    if (expanded.has(item.invoice_id)) { details.classList.remove("hidden"); row.setAttribute("aria-expanded", "true"); }
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
      h("th", {}, canEdit ? "Decision" : "Payment"))),
    body));
  drawRows();
  return h("div", {},
    pageHead("Review queue", "Highest risk first. Click a row for the reasons and what to do. Confirming sets what happens to the payment; your decisions also tune the scoring.",
      items.length ? downloadButton("Download Excel", "GET", "/export/review.xlsx") : null,
      items.length ? downloadButton("Download payment file", "GET", "/export/payment-file.xlsx") : null, runBtn),
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

  let samples = null;
  async function loadSamples(box) {
    try { samples = samples || await api("GET", "/samples"); } catch { return; }
    put(box, h("h2", {}, "No file handy? Try a sample"),
      h("p", { class: "muted small" }, "Made-up invoices in the shapes real exports take, with some problems planted in them. The template is a blank file with the columns the app needs."),
      h("div", { class: "samples" }, samples.map((smp) => h("div", { class: "sample card" },
        h("div", { class: "sample-title" }, smp.title),
        h("div", { class: "muted small" }, smp.description),
        h("div", { class: "row" },
          smp.name.includes("template") ? null : h("button", { class: "btn sm primary", "data-sample": smp.name,
            onclick: async () => {
              try {
                const res = await fetch("/samples/" + encodeURIComponent(smp.name));
                if (!res.ok) throw new Error("Couldn't load the sample.");
                readFile(new File([await res.blob()], smp.name));
              } catch (err) { chooseStep(err); }
            } }, "Try it"),
          downloadButton("Download", "GET", "/samples/" + encodeURIComponent(smp.name), undefined, "sm"))))));
  }

  // Two kinds of upload: a list of invoices (an export, one per row), or invoice documents
  // (one invoice per file: PDFs, Excel invoice forms, scans).
  let mode = "list";
  const modeTabs = () => h("div", { class: "tabs", role: "tablist", "aria-label": "What are you uploading?" },
    [["list", "A list of invoices (export)"], ["docs", "Invoice documents (PDF, Excel, scans)"]].map(([id, label]) =>
      h("button", { class: mode === id ? "on" : "", role: "tab", "aria-selected": String(mode === id), id: "mode-" + id,
        onclick: () => { mode = id; if (id === "docs") docsChoose(); else chooseStep(); } }, label)));

  // One spreadsheet goes to the list importer; PDFs, photos or several files are documents.
  function routeFiles(list) {
    const files = [...list];
    if (files.length === 1 && /\.(xlsx|xlsm|csv|txt|tsv)$/i.test(files[0].name)) return readFile(files[0]);
    mode = "docs";
    docsRead(files);
  }

  function chooseStep(error) {
    const input = h("input", { type: "file", multiple: true, accept: ".xlsx,.xlsm,.csv,.txt,.tsv,.pdf,.png,.jpg,.jpeg",
      class: "hidden", id: "file-input", onchange: () => input.files.length && routeFiles(input.files) });
    const drop = h("div", { class: "drop",
      ondragover: (e) => { e.preventDefault(); drop.classList.add("over"); },
      ondragleave: () => drop.classList.remove("over"),
      ondrop: (e) => { e.preventDefault(); drop.classList.remove("over"); if (e.dataTransfer.files.length) routeFiles(e.dataTransfer.files); } },
      h("div", { class: "big" }, "Drop an Excel or CSV file here"),
      h("p", { class: "muted" }, "An invoice export from QuickBooks, Xero, NetSuite, SAP or your own spreadsheet. Up to 10 MB."),
      h("button", { class: "btn primary", onclick: () => input.click() }, "Choose file"), input);
    const sampleBox = h("div");
    put(holder, modeTabs(), spacer(16), steps(1), error ? failure(error) : null, error ? spacer(12) : null, drop,
      spacer(16), h("p", { class: "muted small" },
        "Each row should be one invoice with at least an invoice number, vendor, amount and date. A bank account column is optional but enables the bank checks. Columns can have any names; you'll match them in the next step."),
      spacer(12), sampleBox);
    loadSamples(sampleBox);
  }

  async function readFile(file, sheet) {
    if (file.size > 10 * 1024 * 1024) return chooseStep(new Error("That file is larger than 10 MB. Split it into smaller files."));
    put(holder, steps(1), h("p", { class: "muted" }, `Reading ${file.name}…`));
    try {
      const content = toBase64(await file.arrayBuffer());
      const info = await api("POST", "/import/preview", { filename: file.name, content_base64: content, sheet: sheet || null });
      mapStep(file, content, info);
    } catch (err) { if (err.status !== 401) chooseStep(err); }
  }

  let lastInfo = {};
  function mapStep(file, content, info) {
    lastInfo = info;
    const filename = file.name;
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
    const sheetSel = info.sheets && info.sheets.length > 1 ? h("select", { id: "sheet",
      onchange: () => readFile(file, sheetSel.value) }, info.sheets.map((n) => h("option", { value: n }, n))) : null;
    if (sheetSel) sheetSel.value = info.sheet;
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
        const request = { filename, content_base64: content, sheet: info.sheet, mapping, date_order: dateOrder };
        const result = await api("POST", "/import", { ...request, run_detection: detect.checked });
        store.set(memoryKey, JSON.stringify({ mapping, date_order: dateOrder }), true);
        await refreshCount();
        updateNavCount();
        resultStep(filename, result, request, file);
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
      info.looks_like_document ? h("div", { class: "note", role: "note", id: "looks-like-document" },
        "This looks like a single invoice rather than a list of invoices. ",
        h("button", { class: "btn sm", onclick: () => { mode = "docs"; docsRead([file]); } }, "Read it as an invoice document")) : null,
      info.looks_like_document ? spacer(12) : null,
      h("div", { class: "card" },
        h("h2", {}, "Which column is which?"),
        h("p", { class: "muted" }, `${filename}${info.sheet ? ` · sheet "${info.sheet}"` : ""} · ${whole.format(info.total_rows)} rows. `,
          saved ? "We've reused the matching you chose last time for a file like this." : "We've guessed from the column names. Check and adjust."),
        sheetSel ? h("div", { class: "note", role: "note" }, `This workbook has ${info.sheets.length} sheets with invoices. `,
          h("label", { for: "sheet" }, "Import sheet: "), sheetSel, " Import each one in turn to add them all.") : null,
        sheetSel ? spacer(12) : null,
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

  function resultStep(filename, r, request, file) {
    const sheets = (file && request.sheet) ? (lastInfo.sheets || []) : [];
    const nextSheet = sheets[sheets.indexOf(request.sheet) + 1];
    const kpi = (label, value, alert) => h("div", { class: "card kpi result-kpis" + (alert ? " alert" : "") },
      h("div", { class: "label" }, label), h("div", { class: "value" }, whole.format(value)));
    put(holder, steps(3),
      h("div", { class: "kpis" }, kpi("Invoices added", r.added), kpi("Already stored, skipped", r.already_stored),
        r.credit_notes ? kpi("Of which credit notes", r.credit_notes) : null,
        kpi("Rows with problems", r.error_count, r.error_count > 0),
        r.detection ? kpi("Flagged from this file", r.detection.flagged_from_file, r.detection.flagged_from_file > 0) : null),
      h("div", { class: "row" },
        downloadButton("Download your file with flags", "POST", "/export/annotated", request, "primary"),
        r.detection ? h("a", { class: "btn", href: "#/review" }, "Open review queue") : null,
        nextSheet ? h("button", { class: "btn", onclick: () => readFile(file, nextSheet) }, `Import sheet "${nextSheet}"`) : null,
        h("button", { class: "btn", onclick: () => chooseStep() }, "Upload another file")),
      spacer(8), h("p", { class: "muted small" },
        "The download is your own workbook with Risk, Review status, Flags and Why flagged columns added to each row, risky rows shaded, and a ranked “Flagged invoices” sheet. Your data and formulas are unchanged; pictures and charts are not copied."),
      r.blank_rows ? h("div", {}, spacer(8), h("p", { class: "muted small" }, r.blank_rows === 1 ? "1 blank or total row was ignored." : `${whole.format(r.blank_rows)} blank or total rows were ignored.`)) : null,
      r.errors.length ? h("div", {}, spacer(16), h("h2", {}, "Rows that were not imported"),
        h("p", { class: "muted" }, "Fix these in your file and upload it again. Rows already imported are skipped automatically."),
        h("div", { class: "table-wrap" }, h("table", {},
          h("thead", {}, h("tr", {}, h("th", { class: "num" }, "Row"), h("th", {}, "Problem"))),
          h("tbody", {}, r.errors.map((e) => h("tr", {}, h("td", { class: "num" }, e.row), h("td", {}, e.message))))))) : null);
    toast(`Imported ${whole.format(r.added)} invoices from ${filename}`);
  }

  /* ---------------- invoice documents ---------------- */
  let docSamples = null;
  async function docsChoose(error) {
    const input = h("input", { type: "file", multiple: true, class: "hidden", id: "doc-input",
      accept: ".pdf,.xlsx,.xlsm,.png,.jpg,.jpeg,.tif,.tiff,.webp",
      onchange: () => input.files.length && docsRead([...input.files]) });
    const drop = h("div", { class: "drop",
      ondragover: (e) => { e.preventDefault(); drop.classList.add("over"); },
      ondragleave: () => drop.classList.remove("over"),
      ondrop: (e) => { e.preventDefault(); drop.classList.remove("over"); if (e.dataTransfer.files.length) docsRead([...e.dataTransfer.files]); } },
      h("div", { class: "big" }, "Drop invoices here"),
      h("p", { class: "muted" }, "PDFs, Excel invoices, or photos and scans of paper invoices. Several at once is fine: up to 50 files, 10 MB each."),
      h("button", { class: "btn primary", onclick: () => input.click() }, "Choose files"), input);
    const sampleBox = h("div");
    put(holder, modeTabs(), spacer(16), error ? failure(error) : null, error ? spacer(12) : null, drop, spacer(16),
      h("p", { class: "muted small" }, "One invoice per file. You'll see what was read from each file and can correct it before anything is saved. Files are read on this server; nothing is sent elsewhere unless an admin has switched on AI reading in Settings."),
      spacer(12), sampleBox);
    try { docSamples = docSamples || await api("GET", "/samples/documents"); } catch { return; }
    const tryBtn = h("button", { class: "btn primary", id: "try-docs", onclick: async () => {
      tryBtn.disabled = true;
      try {
        docsRead(await Promise.all(docSamples.map(async (d) => {
          const res = await fetch("/samples/documents/" + encodeURIComponent(d.name));
          if (!res.ok) throw new Error("Couldn't load the sample invoices.");
          return new File([await res.blob()], d.name);
        })));
      } catch (err) { docsChoose(err); }
    } }, "Read the sample invoices");
    put(sampleBox, h("h2", {}, "No invoices handy? Try these"),
      h("p", { class: "muted small" }, "Made-up invoices from vendors in the sample data. Load the sample data first (Upload > A list of invoices > Try it) so the checks have history to compare with."),
      h("ul", { class: "sample-docs" }, docSamples.map((d) => h("li", {}, h("span", { class: "mono small" }, d.name), " ", h("span", { class: "muted" }, d.description)))),
      h("div", { class: "row" }, tryBtn, downloadButton("Download them", "GET", "/samples/invoice-documents.zip")));
  }

  async function docsRead(files) {
    const extra = files.length > 50 ? files.length - 50 : 0;
    files = files.slice(0, 50);
    const status = h("p", { class: "muted", id: "doc-progress" }, `Reading ${files.length} file${files.length === 1 ? "" : "s"}…`);
    const fill = h("div", { class: "fill" });
    fill.style.width = "0%";
    put(holder, modeTabs(), spacer(16), h("div", { class: "card" }, h("h2", {}, "Reading invoices"), status,
      h("div", { class: "hbar" }, h("span", {}, ""), h("div", { class: "track" }, fill), h("span", {}, ""))));
    const results = new Array(files.length);
    let next = 0;
    let done = 0;
    async function worker() {   // three at a time: quick, without flooding the server
      while (next < files.length) {
        const i = next++;
        const f = files[i];
        try {
          if (f.size > 10 * 1024 * 1024) throw new Error("This file is larger than 10 MB.");
          const content = toBase64(await f.arrayBuffer());
          results[i] = await api("POST", "/documents/extract", { filename: f.name, content_base64: content });
        } catch (err) {
          if (err.status === 401) throw err;
          results[i] = { file: f.name, status: "unreadable", message: err.message };
        }
        done += 1;
        status.textContent = `Read ${done} of ${files.length}: ${f.name}`;
        fill.style.width = (100 * done / files.length) + "%";
      }
    }
    try { await Promise.all([worker(), worker(), worker()]); } catch { return; }
    if (extra) toast(`Only the first 50 files were read; ${extra} were left out.`, true);
    docsReview(results);
  }

  const DOC_FIELDS = [["id", "Invoice #", "text"], ["vendor", "Vendor", "text"], ["date", "Invoice date", "date"],
    ["amount", "Total", "number"], ["bank_account", "Bank account", "text"]];

  function docsReview(results) {
    const usable = results.filter((r) => r.draft);
    const others = results.filter((r) => !r.draft);
    const rows = usable.map((r) => ({ r, include: true, inputs: {} }));
    const err = h("div", { class: "error-box hidden", role: "alert" });
    const addBtn = h("button", { class: "btn primary", id: "add-docs" });
    const count = () => {
      const n = rows.filter((st) => st.include).length;
      addBtn.textContent = `Add ${n} invoice${n === 1 ? "" : "s"}`;
      addBtn.disabled = n === 0;
    };
    const tbody = h("tbody", {}, rows.map((st, index) => {
      const r = st.r;
      const cells = DOC_FIELDS.map(([f, label, type]) => {
        const value = r.fields[f];
        const empty = value === null || value === undefined || value === "";
        const unsure = !empty && (r.confidence[f] ?? 1) < 0.75;
        const shown = empty ? "" : f === "amount" ? Number(value).toFixed(2) : value;
        const input = h("input", { type, value: shown, id: `doc-${index}-${f}`,
          "aria-label": `${label} for ${r.file}`, step: type === "number" ? "0.01" : null, min: type === "number" ? "0" : null,
          class: "doc-input" + (empty && f !== "bank_account" ? " missing" : unsure ? " unsure" : ""),
          placeholder: empty && f !== "bank_account" ? "Type it in" : null,
          title: empty ? (f === "bank_account" ? "Not on this invoice" : "Not found on the invoice: type it in")
            : `${unsure ? "Not sure about this. " : ""}Read from: “${r.source[f] || value}”`,
          oninput: () => input.classList.remove("missing", "unsure") });
        st.inputs[f] = input;
        return h("td", { class: "c-" + f }, input);
      });
      const include = h("input", { type: "checkbox", checked: true, "aria-label": `Include ${r.file}`,
        onchange: () => {
          st.include = include.checked;
          [tr, notes].forEach((row) => row && row.classList.toggle("excluded", !include.checked));
          count();
        } });
      const kind = h("select", { class: "doc-kind", "aria-label": `Type of ${r.file}` },
        h("option", { value: "invoice", selected: r.kind !== "credit" }, "Invoice"),
        h("option", { value: "credit", selected: r.kind === "credit" }, "Credit note"));
      st.kind = kind;
      const tr = h("tr", { class: "doc-row" + (r.warnings.length ? " has-notes" : "") }, h("td", {}, include),
        h("td", { class: "doc-file" }, h("div", { class: "small" }, r.file), h("div", { class: "muted small" }, r.method), kind),
        cells);
      // Notes go on their own line under the invoice, full width, so they're never cut off.
      const notes = r.warnings.length ? h("tr", { class: "doc-notes" }, h("td", {}),
        h("td", { colspan: String(DOC_FIELDS.length + 1) }, h("ul", {}, r.warnings.map((w) => h("li", {}, w))))) : null;
      return [tr, notes];
    }));
    count();
    const detect = h("input", { type: "checkbox", id: "doc-detect", checked: true });
    addBtn.onclick = async () => {
      err.classList.add("hidden");
      const items = [];
      let missing = 0;
      for (const st of rows.filter((x) => x.include)) {
        const item = { draft: st.r.draft, file: st.r.file, kind: st.kind.value };
        for (const [f] of DOC_FIELDS) {
          const v = st.inputs[f].value.trim();
          item[f] = f === "amount" ? (v === "" ? null : Number(v)) : (v || null);
          if (f !== "bank_account" && !v) { st.inputs[f].classList.add("missing"); missing += 1; }
        }
        items.push(item);
      }
      if (missing) {
        err.textContent = "Fill in the boxes outlined in red, or untick those invoices.";
        return err.classList.remove("hidden");
      }
      addBtn.disabled = true;
      addBtn.textContent = "Adding…";
      try {
        const res = await api("POST", "/documents/import", { items, run_detection: detect.checked });
        await refreshCount();
        updateNavCount();
        docsResult(res);
      } catch (ex) {
        if (ex.status === 401) return;
        err.textContent = ex.message;
        err.classList.remove("hidden");
        count();
      }
    };
    const unsureCount = rows.filter((st) => Object.values(st.inputs).some((i) => i.classList.contains("unsure") || i.classList.contains("missing"))).length;
    put(holder, modeTabs(), spacer(16),
      h("div", { class: "section-head" }, h("h2", {}, "Check what was read"), h("span", { class: "spacer" }),
        h("span", { class: "muted small" }, `${usable.length} of ${results.length} file${results.length === 1 ? "" : "s"} read` +
          (unsureCount ? ` · ${unsureCount} to look at` : ""))),
      h("p", { class: "muted" }, "Amber boxes are ones the reader wasn't sure about; red ones are missing. Hover over a box to see the text it came from. Nothing is saved until you add the invoices."),
      usable.length ? h("div", { class: "table-wrap" }, h("table", { class: "doc-table" },
        h("thead", {}, h("tr", {}, h("th", {}, ""), h("th", {}, "File"), DOC_FIELDS.map(([, label]) => h("th", {}, label)))),
        tbody)) : null,
      others.length ? h("div", {}, spacer(16), h("h2", {}, "Not added"),
        h("div", { class: "table-wrap" }, h("table", { id: "doc-others" },
          h("thead", {}, h("tr", {}, h("th", {}, "File"), h("th", {}, "Why"))),
          h("tbody", {}, others.map((r) => h("tr", {}, h("td", { class: "mono small" }, r.file), h("td", {}, r.message))))))) : null,
      spacer(16), err, spacer(8),
      h("div", { class: "row" },
        h("label", { class: "row", for: "doc-detect" }, detect, "Run detection after adding"),
        h("span", { class: "spacer" }),
        h("button", { class: "btn", onclick: () => docsChoose() }, "Start again"),
        usable.length ? addBtn : null));
  }

  async function docsResult(r) {
    const kpi = (label, value, alert) => h("div", { class: "card kpi result-kpis" + (alert ? " alert" : "") },
      h("div", { class: "label" }, label), h("div", { class: "value" }, whole.format(value)));
    let flagged = [];
    if (r.detection && r.ids.length) {
      try {
        const queue = await api("GET", "/review-queue?limit=500");
        flagged = queue.filter((q) => r.ids.includes(q.invoice_id));
      } catch { flagged = []; }
    }
    put(holder, modeTabs(), spacer(16),
      h("div", { class: "kpis" }, kpi("Invoices added", r.added), kpi("Skipped", r.skipped.length, r.skipped.length > 0),
        kpi("Fields you corrected", r.fields_corrected), r.detection ? kpi("Flagged for review", flagged.length, flagged.length > 0) : null),
      flagged.length ? h("div", {}, h("h2", {}, "Flagged from these invoices"),
        h("div", { class: "table-wrap" }, h("table", { id: "doc-flagged" },
          h("thead", {}, h("tr", {}, h("th", {}, "Risk"), h("th", {}, "Invoice"), h("th", {}, "Vendor"), h("th", { class: "num" }, "Amount"), h("th", {}, "Why flagged"))),
          h("tbody", {}, flagged.map((q) => h("tr", {},
            h("td", {}, h("span", { class: "risk " + riskClass(q.risk) }, h("span", { class: "n" }, q.risk))),
            h("td", { class: "mono" }, q.invoice_id, kindTag(q.kind)), h("td", {}, q.vendor), h("td", { class: "num" }, fmtAmount(q.amount)),
            h("td", {}, h("div", { class: "chips" }, q.rules.map((rule) => h("span", { class: "chip " + ruleLevel(rule), title: (RULES[rule] || [, ""])[1] }, ruleLabel(rule)))))))))),
        spacer(16)) : null,
      r.skipped.length ? h("div", {}, h("h2", {}, "Skipped"),
        h("div", { class: "table-wrap" }, h("table", {},
          h("thead", {}, h("tr", {}, h("th", {}, "File"), h("th", {}, "Why"))),
          h("tbody", {}, r.skipped.map((x) => h("tr", {}, h("td", { class: "mono small" }, x.file || ""), h("td", {}, x.reason))))))
      , spacer(16)) : null,
      h("div", { class: "row" },
        r.detection ? h("a", { class: "btn primary", href: "#/review" }, "Open review queue") : null,
        h("button", { class: "btn", onclick: () => docsChoose() }, "Read more invoices")));
    toast(`Added ${whole.format(r.added)} invoice${r.added === 1 ? "" : "s"} from documents`);
  }

  chooseStep();
  return h("div", {}, pageHead("Upload invoices", "Add invoices from an export, or from the invoices themselves."), holder);
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
      return h("tr", {}, h("td", { class: "mono" }, r.id, kindTag(r.kind)), h("td", {}, r.vendor),
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
  const [cfg, me] = await Promise.all([api("GET", "/settings"), api("GET", "/me")]);
  const threshold = h("input", { id: "threshold", type: "number", min: "1", step: "1", value: cfg.approval_threshold, disabled: !isAdmin });
  const require2fa = h("input", { id: "require-2fa", type: "checkbox", checked: cfg.require_2fa,
    disabled: !isAdmin || (!me.two_factor_enabled && !cfg.require_2fa) });
  const aiReading = h("input", { id: "ai-reading", type: "checkbox", checked: cfg.ai_extraction,
    disabled: !isAdmin || (!cfg.ai_available && !cfg.ai_extraction) });
  const company = h("div", { class: "card" }, h("h2", {}, "Company settings"),
    h("form", { class: "form", onsubmit: async (e) => {
      e.preventDefault();
      try {
        await api("PUT", "/settings", { approval_threshold: Number(threshold.value), require_2fa: require2fa.checked,
          ai_extraction: aiReading.checked });
        toast("Saved. Run detection to apply a new threshold to existing invoices.");
      } catch (err) { toast(err.message, true); }
    } },
      h("label", { class: "field", for: "threshold" }, "Approval threshold", threshold),
      h("p", { class: "muted small" }, "Invoices at or above this amount need extra sign-off at your company. Used to spot invoices just under the limit and bills split to avoid it."),
      h("label", { class: "row", for: "require-2fa" }, require2fa, "Require two-factor authentication for everyone"),
      h("p", { class: "muted small" }, isAdmin && !me.two_factor_enabled && !cfg.require_2fa
        ? h("span", {}, "Turn on two-factor for your own account first (", h("a", { href: "#/security" }, "Your account"), "), so you can't lock yourself out.")
        : "People without it are asked to set it up the next time they sign in. Single sign-on users are covered by their provider's own two-factor."),
      h("label", { class: "row", for: "ai-reading" }, aiReading, "Use AI to read invoices the app can't read by itself"),
      h("p", { class: "muted small" }, cfg.ai_available
        ? "Only for scans, photos and unusual layouts that come back incomplete. Those files are sent to Anthropic (Claude) to be read, and each one is noted in the activity log. Everything else is read on this server. This switch also allows AI-written summaries of flags."
        : "Not available: the server has no Anthropic API key (ANTHROPIC_API_KEY). Invoices are read on this server only."),
      isAdmin ? h("div", {}, h("button", { class: "btn primary", type: "submit" }, "Save")) : h("p", { class: "muted small" }, "Only admins can change these.")));

  const you = h("div", { class: "card" }, h("h2", {}, "Your account"),
    h("p", {}, h("strong", {}, session.username), ` · ${session.role} · ${session.company}`),
    h("p", { class: "muted small" }, roleHelp(session.role)),
    h("a", { class: "btn", href: "#/security" }, "Password and two-factor"));

  if (!isAdmin) return h("div", {}, pageHead("Settings"), h("div", { class: "grid grid-2" }, company, you));

  const users = await api("GET", "/users");
  function userActions(u) {
    const cell = h("td", { class: "user-actions" });
    function menu() {
      put(cell, h("div", { class: "row" },
        u.sso_only || u.disabled ? null : h("button", { class: "btn link small", onclick: resetForm }, "Reset password"),
        u.username !== session.username ? h("button", { class: "btn link small", onclick: async () => {
          try {
            await api("POST", `/users/${encodeURIComponent(u.username)}/access`, { enabled: u.disabled });
            toast(u.disabled ? `${u.username} can sign in again.` : `${u.username} was signed out and can no longer sign in.`);
            render();
          } catch (ex) { toast(ex.message, true); }
        } }, u.disabled ? "Give access back" : "Remove access") : null,
        u.two_factor && !u.disabled && u.username !== session.username ? h("button", { class: "btn link small", onclick: async () => {
          try {
            await api("POST", `/users/${encodeURIComponent(u.username)}/2fa/reset`);
            toast(`Two-factor turned off for ${u.username}. They can set it up again.`);
            render();
          } catch (ex) { toast(ex.message, true); }
        } }, "Turn off two-factor") : null));
    }
    function resetForm() {
      const temp = h("input", { type: "password", minlength: "12", required: true, autocomplete: "new-password",
        placeholder: "Temporary password", "aria-label": `Temporary password for ${u.username}` });
      put(cell, h("form", { class: "row", onsubmit: async (e) => {
        e.preventDefault();
        try {
          await api("POST", `/users/${encodeURIComponent(u.username)}/password`, { new_password: temp.value });
          toast(`Temporary password set for ${u.username}. Their other sessions were signed out.`);
          menu();
        } catch (ex) { toast(ex.message, true); }
      } }, temp, h("button", { class: "btn sm primary", type: "submit" }, "Set"),
        h("button", { class: "btn sm", type: "button", onclick: menu }, "Cancel")));
      temp.focus();
    }
    menu();
    return cell;
  }

  const name = h("input", { id: "new-username", autocomplete: "off", required: true, placeholder: "e.g. maria@company.com" });
  const pw = h("input", { id: "new-password", type: "password", autocomplete: "new-password", required: true, minlength: "12" });
  const pwField = h("label", { class: "field", for: "new-password" }, "Temporary password (12+ characters)", pw);
  const ssoOnly = h("input", { id: "new-sso-only", type: "checkbox", onchange: () => {
    pwField.classList.toggle("hidden", ssoOnly.checked);
    pw.required = !ssoOnly.checked;
  } });
  const role = h("select", { id: "new-role" }, h("option", { value: "accountant" }, "Accountant"),
    h("option", { value: "auditor" }, "Auditor"), h("option", { value: "admin" }, "Admin"));
  const err = h("div", { class: "error-box hidden", role: "alert" });
  const team = h("div", { class: "card" }, h("h2", {}, "People in your company"),
    h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "Username"), h("th", {}, "Role"), h("th", {}, "Sign-in"), h("th", {}, ""))),
      h("tbody", {}, users.map((u) => h("tr", {}, h("td", {}, u.username), h("td", {}, u.role),
        h("td", {}, u.disabled ? h("span", { class: "pill dismissed" }, "Access removed")
          : [u.sso_only ? "Single sign-on" : "Password", " ", u.two_factor ? h("span", { class: "pill d-pay" }, "2FA") : null]),
        userActions(u)))))),
    spacer(16), h("h2", {}, "Add a person"),
    h("form", { class: "form", onsubmit: async (e) => {
      e.preventDefault();
      err.classList.add("hidden");
      try {
        await api("POST", "/users", { username: name.value.trim(), password: ssoOnly.checked ? null : pw.value, role: role.value });
        toast(`Added ${name.value.trim()}`);
        render();
      } catch (ex) { err.textContent = ex.message; err.classList.remove("hidden"); }
    } },
      h("label", { class: "field", for: "new-username" }, "Email address", name),
      h("label", { class: "row", for: "new-sso-only" }, ssoOnly, "Signs in with single sign-on only (no password)"),
      pwField,
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
  // Already signed in (session cookie), including straight back from single sign-on?
  try { await loadSession(); await refreshCount(); } catch { session = null; }
  render();
});
