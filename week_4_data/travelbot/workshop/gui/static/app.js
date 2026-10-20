"use strict";
/*
 * Workshop GUI. Plain JavaScript: no framework, no CDN, no build step.
 *
 * Three rules shape this file.
 *   1. Server text (model output, tool results, user text) is untrusted. It reaches the
 *      page only through textContent, by way of the h() helper. innerHTML is never used.
 *   2. Loading, refreshing, switching tabs, picking a session, and polling send GET
 *      requests only. A POST goes out only when the user presses an action button.
 *   3. Each user action gets one operation ID. A retry reuses it, so the server returns
 *      the stored result instead of running the action twice.
 */

const RETRIES = 3; // network-failure retries, each with the same operation ID
const POLL_MS = 1500; // progress polling interval while an action is in flight
const SEP = " · ";
const STORAGE_KEY = "workshop.session";
const REASON_ORDER = ["hard_constraint", "quote_validity", "preference", "confirmation", "claim"];
const REASON_TITLES = {
  hard_constraint: "Hard constraints", quote_validity: "Quote validity", preference: "Preferences",
  confirmation: "Confirmation", claim: "Claimed total",
};
const CHANGE_LABELS = {
  PartySizeChanged: "Add the child",
  "HotelAccepted+EarlierFlightRequested": "Accept hotel + request earlier flight",
  QuoteExpired: "Expire the hotel quote",
};

// Everything the page remembers lives here.
const S = {
  options: null, sessions: [], session: null, status: null,
  busy: false, busyLabel: "", busyStart: 0,
  polling: false, pollSession: null, pollInFlight: false, timer: null,
  snapshot: null, callIndex: null, hideBookkeeping: true, lastDiff: null, inspectorToken: 0,
};

// ---------------------------------------------------------------------------------
// Small DOM and formatting helpers
// ---------------------------------------------------------------------------------

const $ = (id) => document.getElementById(id);
const enc = encodeURIComponent;

function add(node, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.appendChild(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

// Build an element. attrs: class, on<event> handlers, or plain attributes. Strings become text nodes.
function h(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  add(node, children);
  return node;
}

function fill(node, ...children) {
  node.replaceChildren();
  add(node, children);
}

const span = (cls, text) => h("span", { class: cls }, text);
const note = (text) => h("p", { class: "muted" }, text);
const flag = (ok, yes, no, badClass) => span(ok ? "ok" : badClass || "bad", ok ? yes : no);
const fold = (title, content, open) => h("details", { open }, h("summary", {}, title), content);
const pre = (value) => h("pre", {}, typeof value === "string" ? value : JSON.stringify(value, null, 2));

function kv(pairs) {
  const box = h("div", { class: "kv" });
  for (const [label, value] of pairs) add(box, [span("muted", label), h("span", {}, value)]);
  return box;
}

function table(headers, rows) {
  return h("table", {},
    h("tr", {}, headers.map((t) => h("th", {}, t))),
    rows.map((cells) => h("tr", {}, cells.map((c) => h("td", {}, c)))));
}

function usd(cents) {
  if (cents === null || cents === undefined) return "unknown";
  const whole = cents % 100 === 0;
  return "$" + (cents / 100).toLocaleString("en-US", { minimumFractionDigits: whole ? 0 : 2, maximumFractionDigits: 2 });
}
const num = (n) => (n === null || n === undefined ? "n/a" : Number(n).toLocaleString("en-US"));
const clock = (iso) => (iso ? String(iso).replace("T", " ").replace("+00:00", " UTC").replace("Z", " UTC") : "unknown");
const words = (s) => String(s).replace(/_/g, " ");

function short(value, max = 140) {
  const text = value === null || value === undefined ? "" : typeof value === "string" ? value : JSON.stringify(value);
  return text.length > max ? text.slice(0, max) + "..." : text;
}

function sessionUrl(suffix) {
  return "/api/sessions/" + enc(S.session) + (suffix || "");
}

// ---------------------------------------------------------------------------------
// API layer
// ---------------------------------------------------------------------------------

class ApiError extends Error {
  constructor(message, status, type) {
    super(message);
    this.status = status;
    this.type = type;
  }
}

async function readJson(res) {
  let data = null;
  try { data = await res.json(); } catch (e) { data = null; }
  if (res.ok) return data;
  let message = data && data.error;
  if (!message && data && data.detail) message = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
  throw new ApiError(message || "HTTP " + res.status, res.status, data && data.type);
}

async function apiGet(path) {
  let res;
  try {
    res = await fetch(path, { headers: { Accept: "application/json" }, cache: "no-store" });
  } catch (e) {
    throw new ApiError("Cannot reach the server.", 0, "Network");
  }
  return readJson(res);
}

function newOpId() {
  const c = window.crypto;
  if (c && typeof c.randomUUID === "function") return c.randomUUID();
  const bytes = new Uint8Array(16);
  if (c && typeof c.getRandomValues === "function") c.getRandomValues(bytes);
  else for (let i = 0; i < bytes.length; i++) bytes[i] = Math.floor(Math.random() * 256);
  return "op-" + Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// POST with a fixed operation ID. Only a network failure (fetch throws) is retried.
async function apiPost(path, body, opId) {
  const payload = JSON.stringify({ ...body, op_id: opId });
  for (let attempt = 0; ; attempt++) {
    let res;
    try {
      res = await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Workshop-Client": "gui" },
        body: payload,
      });
    } catch (e) {
      if (attempt >= RETRIES) {
        throw new ApiError("Could not reach the server after " + (attempt + 1) + " attempts. The action may have run; the page re-reads the session to show what happened.", 0, "Network");
      }
      setOp("Network error. Retrying the same operation (" + (attempt + 1) + " of " + RETRIES + ")...", "warn");
      await sleep(400 * 2 ** attempt);
      continue;
    }
    return readJson(res);
  }
}

function errorText(err) {
  if (err.type === "SessionExists") return "That session name is taken. Choose another name, or open the session and use Reset.";
  if (err.status === 409) return err.message + " The page was refreshed with the current state.";
  return err.message || String(err);
}

function setOp(text, cls) {
  const line = $("op-status");
  line.className = "small " + (cls || "muted");
  line.textContent = text;
}

// ---------------------------------------------------------------------------------
// Running one action: lock the controls, poll for progress, then refresh from the server
// ---------------------------------------------------------------------------------

async function act(label, path, body, describe, onSuccess) {
  if (S.busy) return; // a second click while an action runs does nothing
  S.busy = true;
  S.busyLabel = label;
  S.busyStart = Date.now();
  updateControls();
  const opId = newOpId(); // one ID per user action; apiPost reuses it for retries
  startProgress(path === "/api/sessions" ? null : S.session);
  try {
    const result = await apiPost(path, body, opId);
    if (onSuccess) onSuccess(result);
    const text = describe ? describe(result) : "Done.";
    if (result && result.replayed) setOp("Already done. This operation ID was applied earlier, so nothing ran again. " + text, "warn");
    else setOp(text, "ok");
  } catch (err) {
    setOp(errorText(err), "bad");
  } finally {
    stopProgress();
    await refreshAll(); // controls stay locked until the fresh status is on screen
    S.busy = false;
    updateControls();
  }
}

function progressText() {
  const secs = Math.round((Date.now() - S.busyStart) / 1000);
  const c = S.status && S.pollSession ? S.status.counts : null;
  return S.busyLabel + " in progress, " + secs + " s" + (c ? " (stored so far: " + c.model_calls + " model calls, " + c.tool_calls + " tool calls)" : "") + "...";
}

function startProgress(sessionName) {
  S.pollSession = sessionName;
  S.polling = true;
  S.pollInFlight = false;
  setOp(progressText(), "muted");
  S.timer = setInterval(tick, POLL_MS);
}

function stopProgress() {
  S.polling = false;
  clearInterval(S.timer);
  S.timer = null;
}

// GET the session status so a long run shows what the server has stored so far.
async function tick() {
  if (!S.polling) return;
  if (S.pollSession && !S.pollInFlight) {
    S.pollInFlight = true;
    try {
      const status = await apiGet("/api/sessions/" + enc(S.pollSession));
      if (S.polling && S.pollSession === S.session) {
        S.status = status;
        renderStatus();
      }
    } catch (e) {
      // The next tick tries again.
    } finally {
      S.pollInFlight = false;
    }
  }
  if (S.polling) setOp(progressText(), "muted");
}

function sessionBody() {
  return { expected_version: S.status ? S.status.cursor.version : null };
}

// One-line summary of a step, a turn, or a whole run.
function describeDetail(d) {
  if (Array.isArray(d)) {
    const steps = d.filter((x) => x.result !== "change_applied");
    const tools = steps.filter((x) => x.result === "tool_called").length;
    const rejected = steps.filter((x) => x.result === "plan_rejected").length;
    const changes = d.length - steps.length;
    const ends = steps.filter((x) => ["plan_presented", "question_asked", "incomplete"].includes(x.result))
      .map((x) => (x.result === "plan_presented" ? x.proposal_id + " (" + usd(x.total_cents) + ")" : words(x.result)));
    return steps.length + " steps (" + tools + " tool calls" + (rejected ? ", " + rejected + " plans rejected by the validator" : "") + ")"
      + (changes ? ", " + changes + " changes applied" : "") + ". Turn outcomes: " + (ends.join(", ") || "none") + ".";
  }
  switch (d.result) {
    case "tool_called": return "Called " + d.tool + (d.ok === false ? " (the tool returned an error)." : ".");
    case "plan_presented": return "Presented " + d.proposal_id + ", total " + usd(d.total_cents) + (d.confirmation_status === "reconfirmation_required" ? " (reconfirmation required)." : ".");
    case "plan_rejected": return "The validator rejected the plan (" + d.codes.join(", ") + "). The agent can try again.";
    case "question_asked": return "The agent asked: " + d.question;
    case "incomplete": return "The turn ended incomplete" + (d.reason ? ": " + d.reason : "") + ".";
    default:
      if (d.applied) {
        return "Applied turn " + d.turn + " (" + (d.applied.join(", ") || "no changes") + ")." + (d.unresolved && d.unresolved.length ? " Unresolved: " + short(d.unresolved) : "");
      }
      return d.message || "Done.";
  }
}

// ---------------------------------------------------------------------------------
// Refreshing from the server (GET only)
// ---------------------------------------------------------------------------------

function rememberSession() {
  try {
    if (S.session) localStorage.setItem(STORAGE_KEY, S.session);
  } catch (e) {
    // Private windows can refuse storage; the page works without it.
  }
}

function recalledSession() {
  try { return localStorage.getItem(STORAGE_KEY); } catch (e) { return null; }
}

async function refreshAll() {
  try {
    S.sessions = await apiGet("/api/sessions");
    if (!S.sessions.some((s) => s.session === S.session)) S.session = S.sessions.length ? S.sessions[0].session : null;
    rememberSession();
    S.status = S.session ? await apiGet(sessionUrl("")) : null;
  } catch (err) {
    setOp("Could not refresh: " + errorText(err), "bad");
  }
  renderSessionSelect();
  renderCompareSelect();
  renderStatus();
  suggestName();
  await refreshInspector();
}

async function selectSession(name) {
  S.session = name || null;
  S.snapshot = null;
  S.callIndex = null;
  rememberSession();
  setOp("", "muted");
  await refreshAll();
}

function renderSessionSelect() {
  const sel = $("session-select");
  const label = (s) => s.session + " (" + s.config.implementation + ", " + s.config.planner + ", " + s.phase + ")";
  fill(sel, S.sessions.length
    ? S.sessions.map((s) => h("option", { value: s.session }, label(s)))
    : h("option", { value: "" }, "(no sessions yet)"));
  sel.value = S.session || "";
}

function renderCompareSelect() {
  const sel = $("compare-select");
  const previous = sel.value;
  const others = S.sessions.filter((s) => s.session !== S.session);
  fill(sel, others.length
    ? others.map((s) => h("option", { value: s.session }, s.session + " (" + s.config.implementation + ")"))
    : h("option", { value: "" }, "(no other session)"));
  if (others.some((s) => s.session === previous)) sel.value = previous;
}

function renderReportLink() {
  const link = $("report-link");
  link.hidden = !S.session;
  if (S.session) link.setAttribute("href", sessionUrl("/report"));
}

// ---------------------------------------------------------------------------------
// Header, scenario change buttons, and control state
// ---------------------------------------------------------------------------------

function modeText(planner, provider, model) {
  return planner === "live"
    ? "planner_mode=live (" + (provider || "provider unknown") + ", " + (model || "model unknown") + ")"
    : "planner_mode=simulated (rule-based, not an LLM measurement)";
}

function renderHeader() {
  const badge = $("mode-badge");
  const st = S.status;
  if (!st) {
    badge.className = "badge muted";
    badge.textContent = "no session";
    badge.removeAttribute("title");
    $("status-line").textContent = "Create a session or open one.";
    return;
  }
  const cfg = st.config;
  const cur = st.cursor;
  const settings = cfg.planner_settings || {};
  badge.className = "badge " + (cfg.planner === "live" ? "ok" : "warn");
  badge.textContent = modeText(cfg.planner, cfg.provider, settings.model);
  badge.setAttribute("title", cfg.planner === "live"
    ? "A real model chose the actions. Results are one sample."
    : "A deterministic program chose the actions. It shows how the data design behaves, not how an LLM behaves.");
  const parts = [
    cfg.implementation + (cfg.implementation === "state" ? " (" + cfg.strategy + " strategy)" : "") + (cfg.restated_brief ? " (brief restated)" : ""),
    "scenario " + cfg.scenario,
    "phase " + cur.phase,
    "turn " + cur.turn,
    "step " + cur.step_in_turn + " of 12, tool calls " + cur.tool_calls_in_turn + " of 8",
    "version " + cur.version,
    "fixture clock " + clock(st.clock),
  ];
  $("status-line").textContent = parts.join(SEP);
}

function changeLabel(turn) {
  return CHANGE_LABELS[turn.change_types.join("+")] || turn.text;
}

function renderChanges() {
  const box = $("change-buttons");
  const st = S.status;
  if (!st) { fill(box, note("No session selected.")); return; }
  const turns = st.scenario.turns;
  if (!turns.length) { fill(box, note("This scenario has no scheduled changes.")); return; }
  const firstPending = turns.findIndex((t) => !t.applied);
  fill(box, turns.map((t, i) => {
    let state = span("muted small", "later");
    if (t.applied) state = span("done small", "applied");
    else if (t.next) state = span("ok small", "next: ready to apply");
    else if (i === firstPending) state = span("warn small", "waits for the agent to finish turn " + st.cursor.turn);
    return h("button", {
      type: "button", class: "change-btn", "data-next": t.next ? "1" : "0", title: "Turn " + t.turn + " (" + t.source + "): " + t.text,
      onclick: () => act("Apply change", sessionUrl("/apply-change"), sessionBody(), (r) => describeDetail(r.detail)),
    }, h("div", {}, "Turn " + t.turn + ": " + changeLabel(t)), state);
  }));
}

// Which buttons need which session state. Everything else is enabled whenever nothing is in flight.
const NEEDS = {
  "btn-reset": (st) => !!st,
  "btn-step": (st) => !!st && st.can_step,
  "btn-turn": (st) => !!st && st.can_step,
  "btn-run": (st) => !!st && (st.can_step || st.can_apply_change),
  "btn-export": (st) => !!st,
  "btn-compare": () => !!S.session && !!$("compare-select").value,
  "btn-compare-export": () => !!S.session && !!$("compare-select").value,
};

function updateControls() {
  const st = S.status;
  for (const e of $("controls").querySelectorAll("button, select, input")) {
    let ok = !S.busy;
    if (ok && NEEDS[e.id]) ok = NEEDS[e.id](st);
    if (ok && e.classList.contains("change-btn")) ok = !!(st && st.can_apply_change && e.dataset.next === "1");
    e.disabled = !ok;
  }
}

function renderStatus() {
  renderHeader();
  renderChanges();
  renderConversation();
  renderPlan();
  renderReportLink();
  updateControls();
}

// ---------------------------------------------------------------------------------
// Conversation and plan card
// ---------------------------------------------------------------------------------

function worldQuoteMap() {
  const map = {};
  for (const q of (S.status && S.status.world_quotes) || []) map[q.quote_id] = q;
  return map;
}

function reasonsBlock(reasons) {
  if (!reasons.length) return h("p", { class: "small muted" }, "Reason codes: none.");
  return h("div", {},
    h("div", { class: "small muted" }, "Reason codes by category"),
    REASON_ORDER.filter((cat) => reasons.some((r) => r.category === cat)).map((cat) => h("div", { class: "small" },
      h("strong", {}, REASON_TITLES[cat] + ": "),
      reasons.filter((r) => r.category === cat).map((r) => h("div", {}, h("code", {}, r.code), " " + r.message)))));
}

// worldQuotes maps quote_id to the world's current view; pass {} when it is not known.
function planCard(p, worldQuotes) {
  const v = p.validation;
  const reconfirm = p.confirmation_status === "reconfirmation_required";
  const now = (id) => (worldQuotes[id] ? (worldQuotes[id].valid_now ? "valid now" : "expired now") : "not checked");
  return h("div", { class: "card plan" + (reconfirm ? " reconfirm" : "") },
    h("div", {}, h("strong", {}, p.proposal_id), " (turn " + p.turn + ")"),
    kv([
      ["Flight", p.flight_id + ", quote " + p.flight_quote_id],
      ["Hotel", p.hotel_id + ", quote " + p.hotel_quote_id],
      ["Computed total", h("strong", {}, usd(p.computed_total_cents))],
      ["Claimed total", [usd(p.claimed_total_cents), " ", flag(p.claimed_total_cents === p.computed_total_cents, "(matches)", "(does not match)")]],
      ["Hard constraints", flag(v.hard_constraints_ok, "pass", "fail")],
      ["Quote freshness", [flag(v.quotes_fresh, "fresh when shown", "stale when shown"), " (flight " + now(p.flight_quote_id) + ", hotel " + now(p.hotel_quote_id) + ")"]],
      ["Preferences", flag(v.preferences_met, "met", "unmet", "warn")],
      ["Confirmation", reconfirm ? h("strong", { class: "warn" }, "reconfirmation required") : words(p.confirmation_status)],
    ]),
    reasonsBlock(v.reasons),
    h("div", { class: "small muted" }, "Explanation shown to the user"),
    h("p", { class: "small" }, p.explanation));
}

function renderConversation() {
  const box = $("conversation");
  const st = S.status;
  if (!st) { fill(box, note("No session selected.")); return; }
  if (!st.conversation.length) { fill(box, note("Nothing yet.")); return; }
  const wq = worldQuoteMap();
  fill(box, st.conversation.map((item) => {
    if (item.speaker !== "assistant") {
      const who = item.speaker === "user" ? "You" : "Inventory notice";
      return h("div", { class: "msg " + item.speaker }, h("div", { class: "who" }, who + SEP + "turn " + item.turn), h("div", {}, item.text));
    }
    const head = h("div", { class: "who" }, "Assistant" + SEP + "turn " + item.turn);
    if (item.outcome === "plan_presented") {
      const p = item.proposal;
      if (!p) return h("div", { class: "msg plan" }, head, h("div", {}, "Presented a plan, but its record is missing."));
      return h("div", { class: "msg plan" }, head,
        h("div", {}, "Presented " + p.proposal_id + ": flight " + p.flight_id + " + hotel " + p.hotel_id + ", total " + usd(p.computed_total_cents) + ". ",
          p.confirmation_status === "reconfirmation_required" ? h("strong", { class: "warn" }, "Reconfirmation required.") : null),
        fold("Plan details", planCard(p, wq)));
    }
    if (item.outcome === "question_asked") {
      const o = st.outcomes.find((x) => x.turn === item.turn && x.kind === "question_asked");
      return h("div", { class: "msg question" }, head, h("div", {}, item.text),
        o && o.unresolved_constraint ? h("div", { class: "small muted" }, "Unresolved constraint: " + o.unresolved_constraint) : null);
    }
    return h("div", { class: "msg incomplete" }, head, h("div", {}, h("strong", { class: "bad" }, "Incomplete. "), item.text || "No reason recorded."));
  }));
}

function renderPlan() {
  const box = $("plan-card");
  const st = S.status;
  if (!st) { fill(box, note("No session selected.")); return; }
  if (!st.displayed_proposal) { fill(box, note("No plan has been shown to the user yet.")); return; }
  const last = st.outcomes[st.outcomes.length - 1];
  fill(box,
    last && last.kind !== "plan_presented"
      ? h("p", { class: "callout small" }, "The latest turn ended with " + words(last.kind) + ". This is the last plan that was shown.")
      : null,
    planCard(st.displayed_proposal, worldQuoteMap()));
}

// ---------------------------------------------------------------------------------
// Inspector tabs
// ---------------------------------------------------------------------------------

function activeTab(group) {
  const nav = document.querySelector('nav[data-group="' + group + '"]');
  const active = Array.from(nav.querySelectorAll("button")).find((b) => b.classList.contains("active"));
  return active ? active.dataset.tab : null;
}

function showTab(group, name) {
  const nav = document.querySelector('nav[data-group="' + group + '"]');
  for (const b of nav.querySelectorAll("button")) b.classList.toggle("active", b.dataset.tab === name);
  for (const p of Array.from(nav.parentElement.children)) {
    if (p.classList.contains("panel")) p.classList.toggle("active", p.dataset.panel === name);
  }
}

const INSPECTOR_VIEWS = ["state-view", "model-view", "tools-view", "validation-view"];

async function refreshInspector() {
  const token = ++S.inspectorToken;
  if (!S.session || !S.status) {
    for (const id of INSPECTOR_VIEWS) fill($(id), note("No session selected."));
    return;
  }
  const tab = activeTab("inspector");
  const target = S.session;
  const views = { state: "state-view", model: "model-view", tools: "tools-view", validation: "validation-view" };
  try {
    if (tab === "state") await loadStateTab(token);
    else if (tab === "model") {
      const calls = await apiGet(sessionUrl("/records/model_calls"));
      if (token === S.inspectorToken && target === S.session) renderModelCalls(calls);
    } else if (tab === "tools") {
      const calls = await apiGet(sessionUrl("/records/tool_calls"));
      if (token === S.inspectorToken && target === S.session) renderToolCalls(calls);
    } else if (tab === "validation") {
      const records = await apiGet(sessionUrl("/records/validations"));
      if (token === S.inspectorToken && target === S.session) renderValidations(records);
    }
  } catch (err) {
    if (token === S.inspectorToken && views[tab]) fill($(views[tab]), h("p", { class: "bad" }, errorText(err)));
  }
}

// -- State tab --------------------------------------------------------------------

async function loadStateTab(token) {
  const st = S.status;
  if (st.config.implementation !== "state" || !st.state_summary) {
    fill($("state-view"), noStateNode(st));
    return;
  }
  const query = S.snapshot ? "?snapshot=" + S.snapshot : "";
  let state, diff;
  try {
    [state, diff] = await Promise.all([apiGet(sessionUrl("/state" + query)), apiGet(sessionUrl("/diff" + query))]);
  } catch (err) {
    if (S.snapshot === null) throw err;
    S.snapshot = null; // the remembered snapshot no longer exists (for example after a reset)
    return loadStateTab(token);
  }
  if (token !== S.inspectorToken) return;
  S.lastDiff = diff;
  fill($("state-view"), stateNode(st, state));
  renderDiff();
}

function worldQuotesTable(st) {
  return table(["Quote", "Item", "Kind", "Party", "Total", "Revision", "At fixture clock"],
    st.world_quotes.map((q) => [q.quote_id, q.item_id, q.kind, q.party, usd(q.total_cents), q.revision, flag(q.valid_now, "valid", "expired")]));
}

function noStateNode(st) {
  const impl = st.config.implementation;
  return h("div", {},
    h("p", { class: "callout" }, impl === "transcript"
      ? "The transcript implementation keeps no structured task state. Its only memory is the message history, replayed to the model in full on every step (see the Model input tab)."
      : "The one-shot implementation keeps no structured task state. Each call sees one request message, and nothing carries over unless the caller restates the brief."),
    h("h3", {}, "Quotes as the world sees them"),
    h("p", { class: "small muted" }, "Fixture clock " + clock(st.clock) + ". These are the quotes the mock inventory has issued, and whether each still holds."),
    worldQuotesTable(st));
}

function decisionNode(d) {
  if (!d) return note("No hotel has been accepted yet.");
  const cls = { accepted: "ok", needs_reconfirmation: "warn", no_longer_suitable: "bad" }[d.status] || "muted";
  return h("div", { class: "card" + (d.requires_reconfirmation ? " reconfirm" : "") }, kv([
    ["Hotel", d.hotel_id],
    ["Status", h("strong", { class: cls }, words(d.status))],
    ["Requires reconfirmation", flag(!d.requires_reconfirmation, "no", "yes", "warn")],
    ["Reason", d.reason || "none"],
    ["Accepted total", usd(d.accepted_total_cents)],
    ["Accepted quote", d.accepted_quote_id],
    ["From proposal", d.accepted_from_proposal_id],
    ["Accepted by change", d.accepted_by_change_id],
  ]));
}

function anchorNode(a) {
  if (!a) return note("No earlier-arrival request yet.");
  return h("div", { class: "card" }, kv([
    ["Reference proposal", a.reference_proposal_id],
    ["Reference flight", a.reference_flight_id],
    ["Reference arrival", a.reference_arrival_local + " " + a.time_zone],
    ["Rule", words(a.direction) + ", " + words(a.comparison) + " the reference. The reference does not move."],
    ["Requested by change", a.requested_by_change_id],
  ]));
}

function quoteFacts(q) {
  return q.kind === "flight"
    ? "arrives " + q.arrive_local + ", " + q.stops + (q.stops === 1 ? " stop" : " stops")
    : "sleeps " + q.capacity + ", " + q.walk_minutes + " min walk";
}

function quoteTable(quotes, invalidated) {
  if (!quotes.length) return note("None.");
  return table(["Quote", "Item", "Facts", "Party", "Total", invalidated ? "Status and reason" : "Expires"],
    quotes.map((q) => [q.quote_id, q.item_id + " (" + q.kind + ")", quoteFacts(q), q.adults + "a" + q.children + "c", usd(q.total_cents),
      invalidated ? [h("strong", { class: "warn" }, q.status), " " + (q.status_reason || "")] : clock(q.expires_at)]));
}

function stateNode(st, state) {
  const ss = st.state_summary;
  const unsuitable = Object.values(ss.unsuitable_items);
  const latest = state.snapshots[state.snapshots.length - 1].index;
  const select = h("select", {
    id: "snapshot-select",
    onchange: (ev) => {
      const index = Number(ev.target.value);
      S.snapshot = index === latest ? null : index;
      refreshInspector();
    },
  }, state.snapshots.map((s) => h("option", { value: s.index, selected: s.index === state.index },
    s.index + ": after " + s.change_id + (s.index === latest ? " (latest)" : ""))));
  return h("div", {},
    h("p", { class: "small muted" }, "Summary of the latest task state: revision " + ss.revision + ", requirements revision " + ss.requirements_revision
      + ", party " + (ss.party || "unknown") + "."),
    h("h3", {}, "Retained hotel decision"), decisionNode(ss.decisions.hotel),
    h("h3", {}, "Earlier-arrival anchor"), anchorNode(ss.earlier_arrival),
    h("h3", {}, "Current quotes (" + ss.current_quotes.length + ")"), quoteTable(ss.current_quotes, false),
    h("h3", {}, "Invalidated quotes (" + ss.invalidated_quotes.length + ")"), quoteTable(ss.invalidated_quotes, true),
    h("h3", {}, "Unsuitable items"),
    unsuitable.length ? table(["Item", "Reason", "Since change", "Requirement revision"],
      unsuitable.map((n) => [n.item_id, n.reason, n.since_change_id, n.requirement_revision])) : note("None."),
    h("h3", {}, "Pending tasks"),
    ss.pending.length ? table(["Task", "Kind", "Target", "Reason", "Status", "Closed by"],
      ss.pending.map((t) => [t.task_id, words(t.kind), t.target || "", t.reason,
        span(t.status === "open" ? "warn" : "ok", t.status), t.closed_by || ""])) : note("None."),
    ss.open_question ? h("p", { class: "callout" }, "Open question: " + ss.open_question) : null,
    ss.proposal ? h("p", { class: "small muted" }, "Current proposal in state: " + ss.proposal.proposal_id + ", status " + words(ss.proposal.status)
      + (ss.proposal.status_reason ? " (" + ss.proposal.status_reason + ")" : "") + ".") : null,
    h("h3", {}, "State snapshots"),
    h("label", {}, "Snapshot ", select),
    h("div", { id: "diff-box" }),
    fold("Full state JSON (snapshot " + state.index + ")", pre(state.state)),
    fold("Quotes as the world sees them", worldQuotesTable(st)));
}

// The diff table for the selected snapshot. Redrawn when the bookkeeping checkbox changes.
function renderDiff() {
  const box = $("diff-box");
  if (!box || !S.lastDiff) return;
  const d = S.lastDiff;
  const noise = /^(revision|last_seq|last_input_seq|applied_change_ids)/;
  const changes = S.hideBookkeeping ? d.changes.filter((c) => !noise.test(c.path)) : d.changes;
  const cell = (v) => h("span", { title: short(v, 2000) }, short(v));
  fill(box,
    h("h3", {}, "What snapshot " + d.index + " changed (" + d.change_id + ")"),
    h("label", {}, h("input", {
      type: "checkbox", checked: S.hideBookkeeping,
      onchange: (ev) => { S.hideBookkeeping = ev.target.checked; renderDiff(); },
    }), " Hide revision counters and applied-change bookkeeping"),
    changes.length ? table(["Op", "Path", "Before", "After"], changes.map((c) => [c.op, c.path, cell(c.before), cell(c.after)])) : note("No differences to show."));
}

// -- Model input tab ----------------------------------------------------------------

function callRowText(c) {
  const u = c.usage;
  return "#" + c.index + SEP + "turn " + c.turn + " step " + c.step + SEP + words(c.purpose) + SEP + num(c.input_chars) + " chars"
    + (u ? SEP + num(u.input_tokens) + " in / " + num(u.output_tokens) + " out tokens" : "") + (c.errors.length ? SEP + "errors" : "");
}

function renderModelCalls(calls) {
  const view = $("model-view");
  if (!calls.length) { fill(view, note("No model calls yet. Step the agent to create one.")); return; }
  if (S.callIndex === null || !calls.some((c) => c.index === S.callIndex)) S.callIndex = calls[calls.length - 1].index;
  const chosen = calls.find((c) => c.index === S.callIndex);
  fill(view,
    h("p", { class: "small muted" }, "Select a call to see exactly what the model received and returned."),
    h("div", { class: "call-list" }, calls.map((c) => h("button", {
      type: "button", class: c.index === S.callIndex ? "selected" : "",
      onclick: () => { S.callIndex = c.index; renderModelCalls(calls); },
    }, callRowText(c)))),
    modelCallDetail(chosen));
}

function messageFold(m, open) {
  const label = m.role + (m.tool_call ? " (calls " + m.tool_call.name + ")" : "") + (m.tool_call_id ? " (result for " + m.tool_call_id + ")" : "")
    + SEP + num(m.content.length) + " chars";
  return fold(label, h("div", {},
    m.tool_call ? [h("div", { class: "small muted" }, "tool_call " + m.tool_call.id + ": " + m.tool_call.name), pre(m.tool_call.arguments)] : null,
    pre(m.content)), open);
}

function modelCallDetail(c) {
  const input = c.input;
  const settings = c.settings || {};
  return h("div", {},
    h("h3", {}, "Call #" + c.index),
    kv([
      ["Turn and step", "turn " + c.turn + ", step " + c.step],
      ["Purpose", words(c.purpose)],
      ["Trigger", c.trigger],
      ["Implementation", c.implementation],
      ["Planner", modeText(c.planner_mode, settings.provider, settings.model)],
      ["Input size", num(c.input_chars) + " characters (system prompt plus messages)"],
      ["Usage", c.usage ? num(c.usage.input_tokens) + " input / " + num(c.usage.output_tokens) + " output tokens" : "not reported"],
      ["Latency", c.latency_ms === null || c.latency_ms === undefined ? "not measured" : Math.round(c.latency_ms) + " ms"],
      ["Attempts", c.attempts],
      ["Errors", c.errors.length ? span("bad", c.errors.join("; ")) : "none"],
    ]),
    h("h3", {}, "Returned action"),
    pre(c.action || "No valid action was returned."),
    h("h3", {}, "Model input"),
    fold("System prompt (" + num(input.system.length) + " chars)", pre(input.system)),
    h("div", { class: "small muted" }, "Messages (" + input.messages.length + ")"),
    input.messages.map((m) => messageFold(m, input.messages.length === 1)),
    fold("Tools offered (" + input.tools.map((t) => t.name).join(", ") + ")", pre(input.tools)),
    c.provider_request ? fold("Provider request body", pre(c.provider_request)) : null);
}

// -- Tool calls and validations ------------------------------------------------------

function renderToolCalls(calls) {
  const view = $("tools-view");
  if (!calls.length) { fill(view, note("No tool calls yet.")); return; }
  fill(view, calls.map((c) => {
    const failed = c.result && c.result.ok === false;
    return h("div", { class: "card" },
      h("div", {}, h("strong", {}, c.name), SEP + c.tool_call_id + SEP + "turn " + c.turn + " step " + c.step + SEP
        + (c.caller === "agent" ? "called by the agent" : "called by the application")),
      h("div", { class: "small muted" }, "Arguments"), pre(c.arguments),
      fold(failed ? "Result (the tool reported an error)" : "Result", pre(c.result)));
  }));
}

function renderValidations(records) {
  const view = $("validation-view");
  if (!records.length) { fill(view, note("No validations yet.")); return; }
  fill(view, h("p", { class: "small muted" }, "agent_tool is the agent asking the validator. runner_gate is the application checking a plan before showing it."),
    records.map((rec) => {
      const v = rec.result;
      return h("div", { class: "card" },
        h("div", {}, h("strong", {}, rec.source), SEP + rec.tool_call_id + SEP + "turn " + rec.turn),
        kv([
          ["Flight quote", v.flight_quote_id + " (" + (v.flight_id || "unknown item") + ")"],
          ["Hotel quote", v.hotel_quote_id + " (" + (v.hotel_id || "unknown item") + ")"],
          ["Computed total", usd(v.computed_total_cents)],
          ["Claimed total", usd(v.claimed_total_cents)],
          ["Hard constraints ok", flag(v.hard_constraints_ok, "true", "false")],
          ["Quotes fresh", flag(v.quotes_fresh, "true", "false")],
          ["Preferences met", flag(v.preferences_met, "true", "false", "warn")],
          ["Confirmation required", v.confirmation_required ? span("warn", "true") : span("muted", "false")],
          ["Requirement revision", v.requirement_revision],
        ]),
        reasonsBlock(v.reasons));
    }));
}

// ---------------------------------------------------------------------------------
// Check results
// ---------------------------------------------------------------------------------

// Labels match the command line's `workshop check` output.
const EXERCISE_STATUS = {
  pass: ["PASS", "ok"], todo: ["TODO", "warn"], not_yet: ["NOT YET", "warn"],
  student_error: ["ERROR in exercises.py", "bad"], infrastructure_error: ["SETUP PROBLEM", "bad"],
};

function scenarioChecksTable(checks) {
  if (!checks.length) return note("No checks recorded.");
  return table(["Result", "Check", "Detail"], checks.map((c) => [
    c.passed === true ? span("ok", "PASS") : c.passed === false ? span("warn", "NOT YET") : span("muted", "n/a"),
    h("span", { title: c.description }, c.id), c.detail]));
}

function renderChecks(report) {
  const tone = report.overall === "pass" ? "ok" : /error/.test(report.overall) ? "bad" : "warn";
  fill($("check-view"),
    h("h3", {}, span(tone, report.summary || "")),
    h("p", {}, report.next_step || ""),
    report.error ? h("p", { class: "bad" }, report.error) : null,
    h("h3", {}, "Exercise checks"),
    report.exercise_checks.length ? table(["Status", "Check", "Detail"], report.exercise_checks.map((c) => {
      const [label, cls] = EXERCISE_STATUS[c.status] || [c.status, "muted"];
      return [span(cls, label), h("span", { title: c.description }, c.id),
        [c.detail, c.traceback ? fold("Traceback", pre(c.traceback)) : null]];
    })) : note("No exercise checks ran."),
    h("h3", {}, "Scenario runs (simulated state agent)"),
    report.scenario_runs.map((r) => h("div", { class: "card" },
      h("strong", {}, r.scenario), SEP, r.status === "ran" ? span("muted", "ran") : span("bad", (EXERCISE_STATUS[r.status] || [words(r.status)])[0]),
      r.error ? [h("p", { class: "bad" }, r.error), r.traceback ? fold("Traceback", pre(r.traceback)) : null] : null,
      scenarioChecksTable(r.checks))));
}

// ---------------------------------------------------------------------------------
// Side-by-side comparison
// ---------------------------------------------------------------------------------

function totalsText(t) {
  return num(t.model_calls) + " model calls, " + num(t.agent_tool_calls) + " agent tool calls, " + num(t.application_tool_calls)
    + " application prefetches, " + num(t.input_chars) + " input characters"
    + (t.usage_reported ? "; " + num(t.input_tokens) + " input / " + num(t.output_tokens) + " output tokens (provider reported)" : "; token usage not reported");
}

function outcomeNode(o) {
  if (!o) return h("p", { class: "muted small" }, "Turn not finished.");
  if (o.kind === "plan_presented") return h("p", { class: "ok small" }, "Plan presented: " + o.proposal_id + " (" + o.tool_calls + " tool calls, " + o.steps + " steps)");
  if (o.kind === "question_asked") return h("p", { class: "warn small" }, "Question asked (" + o.unresolved_constraint + "): " + o.question);
  return h("p", { class: "bad small" }, "Incomplete: " + o.reason);
}

function turnCell(run, n) {
  const t = run.turns.find((x) => x.turn === n);
  if (!t) return h("div", { class: "card muted" }, "not reached");
  const chars = t.steps.reduce((sum, s) => sum + s.input_chars, 0);
  const reported = t.steps.filter((s) => s.usage && s.usage.input_tokens !== null && s.usage.input_tokens !== undefined);
  const tokens = reported.length
    ? "; " + num(reported.reduce((a, s) => a + s.usage.input_tokens, 0)) + " input / " + num(reported.reduce((a, s) => a + (s.usage.output_tokens || 0), 0)) + " output tokens" : "";
  return h("div", { class: "card" },
    t.app_tool_calls.length ? h("p", { class: "small muted" }, "Application prefetch calls: " + t.app_tool_calls.length) : null,
    t.steps.length ? h("ol", {}, t.steps.map((s) => h("li", {}, s.action, h("br"), span("muted small", s.result)))) : h("p", { class: "muted small" }, "No model calls."),
    h("p", { class: "small muted" }, t.steps.length + " model calls, " + num(chars) + " input characters" + tokens),
    outcomeNode(t.outcome),
    t.proposal ? planCard(t.proposal, {}) : null);
}

function runHeader(run) {
  const settings = run.settings || {};
  return h("div", { class: "card" },
    h("strong", {}, run.config.implementation), SEP + run.session, h("br"),
    h("span", { class: "badge " + (run.planner_mode === "live" ? "ok" : "warn") }, modeText(run.planner_mode, run.config.provider, settings.model)),
    h("p", { class: "small" }, totalsText(run.totals)),
    fold("Model settings", pre(settings)));
}

function renderCompare(runs) {
  const sameSettings = new Set(runs.map((r) => JSON.stringify(r.settings))).size === 1;
  const turnNumbers = [...new Set(runs.flatMap((r) => r.turns.map((t) => t.turn)))].sort((a, b) => a - b);
  const grid = (cells) => h("div", { class: "compare-grid" }, cells);
  fill($("compare-view"),
    h("p", { class: "small muted" }, "Each column is one recorded run. One run is one sample; a single run does not establish reliability or superiority."),
    sameSettings ? null : h("p", { class: "callout" }, "Model settings differ between these runs, so differences in outcome may come from settings and not from data design."),
    grid(runs.map(runHeader)),
    h("h3", {}, "Turn by turn"),
    turnNumbers.map((n) => {
      const inputs = runs.map((r) => r.turns.find((t) => t.turn === n)).find((t) => t && t.inputs.length);
      return h("div", {}, h("h3", {}, "Turn " + n + ": " + (inputs ? inputs.inputs.map((i) => i.text).join(" / ") : "initial request")), grid(runs.map((r) => turnCell(r, n))));
    }),
    h("h3", {}, "Retained hotel decision"),
    grid(runs.map((r) => h("div", {}, h("strong", {}, r.config.implementation + SEP + r.session),
      r.final_state ? decisionNode((r.final_state.decisions || {}).hotel) : note("This implementation keeps no structured state, so there is no retained decision record.")))),
    h("h3", {}, "Independent checks"),
    grid(runs.map((r) => h("div", {}, h("strong", {}, r.config.implementation + SEP + r.session), scenarioChecksTable(r.checks)))));
}

async function compareSideBySide() {
  const other = $("compare-select").value;
  if (!S.session || !other) return;
  showTab("center", "compare");
  fill($("compare-view"), note("Loading comparison..."));
  try {
    renderCompare(await apiGet("/api/compare?sessions=" + enc(S.session) + "&sessions=" + enc(other)));
  } catch (err) {
    fill($("compare-view"), h("p", { class: "bad" }, errorText(err)));
  }
}

// ---------------------------------------------------------------------------------
// Create form
// ---------------------------------------------------------------------------------

const chosenImpl = () => Array.from(document.querySelectorAll('input[name="impl"]')).find((r) => r.checked).value;

function updateCreateForm() {
  $("provider-wrap").hidden = $("f-planner").value !== "live";
  $("restated-wrap").hidden = chosenImpl() !== "one-shot";
}

// Propose an unused session name, unless the user typed their own.
function suggestName() {
  const input = $("f-name");
  if (input.value && !/^(one-shot|transcript|state)(-\d+)?$/.test(input.value)) return;
  const taken = new Set(S.sessions.map((s) => s.session));
  const base = chosenImpl();
  let name = base;
  for (let n = 2; taken.has(name); n++) name = base + "-" + n;
  input.value = name;
}

function fillOptions() {
  const scenarios = (S.options && S.options.scenarios) || [{ name: "changing-trip", description: "", turns: [] }];
  fill($("f-scenario"), scenarios.map((sc) => h("option", { value: sc.name, title: sc.description }, sc.name + " (" + sc.turns.length + " scheduled changes)")));
  if (scenarios.some((sc) => sc.name === "changing-trip")) $("f-scenario").value = "changing-trip";
  const configured = (S.options && S.options.live_providers_configured) || {};
  for (const opt of $("f-provider").options) {
    if (opt.value) opt.textContent = opt.value + (configured[opt.value] ? " (credentials found)" : " (no credentials found)");
  }
}

function onCreate(ev) {
  ev.preventDefault();
  const impl = chosenImpl();
  const live = $("f-planner").value === "live";
  const body = {
    session: $("f-name").value.trim(), implementation: impl, scenario: $("f-scenario").value,
    planner: live ? "live" : "simulated", provider: live ? $("f-provider").value || null : null,
    strategy: $("f-strategy").value, restated_brief: impl === "one-shot" && $("f-restated").checked,
  };
  act("Create session", "/api/sessions", body, (r) => "Created session " + r.session + " (" + r.detail.message + ")", (r) => {
    S.session = r.session;
    S.snapshot = null;
    S.callIndex = null;
  });
}

// ---------------------------------------------------------------------------------
// Wiring and startup
// ---------------------------------------------------------------------------------

function wire() {
  $("session-select").addEventListener("change", (ev) => selectSession(ev.target.value));
  $("create-form").addEventListener("submit", onCreate);
  $("f-planner").addEventListener("change", updateCreateForm);
  for (const radio of document.querySelectorAll('input[name="impl"]')) {
    radio.addEventListener("change", () => { updateCreateForm(); suggestName(); });
  }
  $("compare-select").addEventListener("change", updateControls);

  $("btn-reset").addEventListener("click", () => {
    if (!S.session) return;
    if (!window.confirm('Reset session "' + S.session + '"? Its progress, records, and exported files are discarded and it starts again from the initial request.')) return;
    act("Reset", sessionUrl("/reset"), sessionBody(), (r) => "Session reset (version " + r.version + ").", () => { S.snapshot = null; S.callIndex = null; });
  });
  $("btn-step").addEventListener("click", () => act("Step", sessionUrl("/step"), sessionBody(), (r) => describeDetail(r.detail)));
  $("btn-turn").addEventListener("click", () => act("Run turn", sessionUrl("/run-turn"), sessionBody(), (r) => describeDetail(r.detail)));
  $("btn-run").addEventListener("click", () => act("Run to completion", sessionUrl("/run"), sessionBody(), (r) => describeDetail(r.detail)));
  $("btn-export").addEventListener("click", () => act("Export", sessionUrl("/export"), sessionBody(),
    (r) => "Exported " + r.files.length + " files to " + S.status.path + ": " + r.files.join(", ") + "."));

  $("btn-check").addEventListener("click", () => act("Run checks", "/api/check", { strategy: $("c-strategy").value },
    (r) => "Checks finished: " + r.report.summary + ".",
    (r) => { renderChecks(r.report); showTab("center", "checks"); }));

  $("btn-compare").addEventListener("click", compareSideBySide);
  $("btn-compare-export").addEventListener("click", () => {
    const other = $("compare-select").value;
    act("Export comparison", "/api/compare", { sessions: [S.session, other], output: "submissions/comparison.html" },
      (r) => "Wrote the comparison to " + r.output + ".");
  });

  for (const nav of document.querySelectorAll("nav.tabs")) {
    for (const button of nav.querySelectorAll("button")) {
      button.addEventListener("click", () => {
        showTab(nav.dataset.group, button.dataset.tab);
        if (nav.dataset.group === "inspector") refreshInspector(); // GET only
      });
    }
  }
}

async function init() {
  wire();
  updateCreateForm();
  try {
    S.options = await apiGet("/api/options");
  } catch (err) {
    setOp("Could not load options: " + errorText(err), "bad");
  }
  fillOptions();
  S.session = recalledSession();
  await refreshAll();
  updateControls();
}

document.addEventListener("DOMContentLoaded", init);
