/* Workshop GUI. Reads with GET; changes state only through POST /api/ops with an
 * operation ID per user action. Pending operation IDs are kept in sessionStorage
 * so a page refresh re-attaches to them (by polling) instead of resubmitting. */
(function () {
  "use strict";
  var el = TraceView.el;
  var S = { config: null, session: null, status: null, runs: [], selectedRun: null, runData: null,
            tab: "state", events: [], lastCheck: null, busy: false };

  // ------------------------------------------------------------------ http
  function get(url) {
    return fetch(url, { headers: { Accept: "application/json" } }).then(function (r) {
      if (!r.ok) return r.json().catch(function () { return {}; }).then(function (b) { throw new Error(b.detail || r.statusText); });
      return r.json();
    });
  }
  function newOpId() {
    if (window.crypto && crypto.randomUUID) return "gui-" + crypto.randomUUID();
    return "gui-" + Date.now().toString(36) + Math.random().toString(36).slice(2, 12);
  }
  function post(body, attempt) {
    attempt = attempt || 1;
    return fetch("/api/ops", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
      .then(function (r) {
        return r.json().then(function (b) { if (!r.ok) throw new Error(b.detail || r.statusText); return b; });
      })
      .catch(function (err) {
        // Network failure: retry with the SAME op_id; the server never runs an ID twice.
        if (attempt < 3 && err instanceof TypeError) return new Promise(function (res) { setTimeout(res, 500 * attempt); })
          .then(function () { return post(body, attempt + 1); });
        throw err;
      });
  }

  // --------------------------------------------------------------- pending
  function pending() { try { return JSON.parse(sessionStorage.getItem("pendingOps") || "[]"); } catch (e) { return []; } }
  function setPending(list) { try { sessionStorage.setItem("pendingOps", JSON.stringify(list)); } catch (e) { /* ignore */ } }

  function toast(text, bad) {
    var box = document.getElementById("toast");
    var n = el("div", { class: "notice" + (bad ? " bad" : ""), text: text });
    box.appendChild(n);
    setTimeout(function () { n.remove(); }, bad ? 9000 : 4500);
  }
  function setBusy(b) {
    S.busy = b;
    document.querySelectorAll("button[data-action]").forEach(function (btn) { btn.disabled = b; });
  }

  function watch(opId) {
    setBusy(true);
    return new Promise(function (resolve) {
      (function poll() {
        get("/api/ops/" + encodeURIComponent(opId)).then(function (op) {
          if (op.status === "queued" || op.status === "running") return setTimeout(poll, 600);
          setPending(pending().filter(function (id) { return id !== opId; }));
          setBusy(false);
          resolve(op);
        }).catch(function () { setTimeout(poll, 1500); });
      })();
    });
  }

  function act(kind, params) {
    if (S.busy) return Promise.resolve();
    var opId = newOpId();
    setPending(pending().concat([opId]));
    setBusy(true);
    return post({ op_id: opId, kind: kind, params: params }).then(function () { return watch(opId); })
      .then(function (op) { report(op); return refreshAll(op); })
      .catch(function (err) { setBusy(false); setPending(pending().filter(function (id) { return id !== opId; })); toast(err.message, true); });
  }

  function report(op) {
    if (op.status === "failed" || op.status === "interrupted") { toast(op.kind + " " + op.status + ": " + (op.error || ""), true); return; }
    var r = op.result || {};
    if (op.kind === "check") { S.lastCheck = r; S.tab = "checks"; toast("checks: " + r.summary.pass + " passed, " + r.summary.todo + " unfinished, " + (r.summary.fail + r.summary.error) + " failing"); return; }
    if (op.kind === "export") { showExportLinks(r); toast("evidence exported to submissions/"); return; }
    if (r.run_dir) {
      S.selectedRun = relRun(r.run_dir);
      if (r.status === "failed") toast((r.exercise_incomplete ? "Unfinished exercise (expected until you edit exercises.py): " : "Run failed: ") + r.error, true);
      else toast(op.kind + ": " + (r.executed_stages || []).map(function (s) { return s.stage + " " + s.status; }).join(", "));
      return;
    }
    toast(op.kind + " done");
  }
  function relRun(p) {
    var root = S.config.root.replace(/\/$/, "") + "/";
    return p.indexOf(root) === 0 ? p.slice(root.length) : p;
  }
  function showExportLinks(r) {
    var box = document.getElementById("export-links");
    box.textContent = "";
    (r.files || []).forEach(function (f) {
      var name = f.split("/").pop();
      box.appendChild(el("a", { href: "/api/files/submissions/" + encodeURIComponent(name), target: "_blank", rel: "noopener", text: name }));
      box.appendChild(document.createTextNode("  "));
    });
  }

  // ---------------------------------------------------------------- loading
  var lastSignature = null;
  function refreshAll(op, background) {
    return get("/api/sessions").then(function (sessions) {
      var sel = document.getElementById("session-select");
      var current = S.session || localStorage.getItem("session") || (sessions[0] && sessions[0].name);
      sel.textContent = "";
      sessions.forEach(function (s) { sel.appendChild(el("option", { value: s.name, text: s.name + " (through " + s.through_sequence + ")" })); });
      if (!sessions.length) sel.appendChild(el("option", { value: "", text: "no sessions yet" }));
      if (current && sessions.some(function (s) { return s.name === current; })) sel.value = current;
      S.session = sel.value || null;
      return Promise.all([
        S.session ? get("/api/sessions/" + encodeURIComponent(S.session)) : Promise.resolve(null),
        get("/api/runs"),
        S.session ? get("/api/sessions/" + encodeURIComponent(S.session) + "/events") : Promise.resolve([]),
        get("/api/ops?limit=15"),
      ]);
    }).then(function (res) {
      renderOps(res[3]);
      var sig = JSON.stringify([S.session, res[0] && res[0].through_sequence, res[0] && res[0].runs, res[1]]);
      if (background && sig === lastSignature) return null;  // nothing changed: keep open panels as they are
      lastSignature = sig;
      S.status = res[0]; S.runs = res[1]; S.events = res[2];
      renderSessionInfo(); renderRuns(); renderCompareSelectors();
      var sessionRuns = runsForSession();
      if (S.status && S.status.active_run) S.selectedRun = S.status.active_run.run_dir;
      if (!S.selectedRun || !S.runs.some(function (r) { return r.run_dir === S.selectedRun; }))
        S.selectedRun = sessionRuns.length ? sessionRuns[sessionRuns.length - 1].run_dir : null;
      return loadRun();
    }).catch(function (err) { toast(err.message, true); });
  }

  function runsForSession() {
    return S.runs.filter(function (r) { return r.session === S.session; });
  }

  function loadRun() {
    if (!S.selectedRun) { S.runData = null; renderRunViews(); return Promise.resolve(); }
    return get("/api/run?path=" + encodeURIComponent(S.selectedRun)).then(function (d) { S.runData = d; renderRunViews(); });
  }

  // -------------------------------------------------------------- rendering
  function renderSessionInfo() {
    var info = document.getElementById("session-info"), fx = document.getElementById("fixtures");
    info.textContent = ""; fx.textContent = "";
    if (!S.status) { info.textContent = "Prepare a session to begin (same as `workshop prepare`)."; return; }
    var m = S.status.meta;
    info.appendChild(el("div", { text: "Events through " + S.status.through_sequence + ". Paused by pid " + m.checkpoint_pid +
      "; resumed history appended by pid " + m.resumer_pid + "." }));
    info.appendChild(el("div", { text: "Follow-ups applied: " + (S.status.followups_applied.join(", ") || "none") }));
    S.config.fixtures.forEach(function (f) {
      var applied = S.status.followups_applied.indexOf(f) >= 0;
      var b = el("button", { "data-action": "append", "data-fixture": f, text: (applied ? "✓ " : "Append ") + f });
      b.disabled = applied || S.busy;
      fx.appendChild(b);
    });
  }

  function renderStages() {
    var list = document.getElementById("stage-list");
    list.textContent = "";
    var prog = S.runData ? S.runData.progress : null;
    var byStage = {};
    ((prog && prog.stages) || []).forEach(function (s) { byStage[s.stage] = s; });
    S.config.stages.forEach(function (st) {
      var rec = byStage[st.name];
      var cls = rec ? rec.status : (prog && prog.next_stage === st.name ? "next" : "");
      var label = st.name + (rec ? " — " + rec.status + (rec.duration_ms != null ? " (" + rec.duration_ms.toFixed(2) + " ms)" : "") : "");
      list.appendChild(el("li", { class: cls, title: st.description, text: label }));
    });
  }

  function renderRuns() {
    var t = document.getElementById("runs");
    t.textContent = "";
    var head = el("tr");
    ["run", "impl", "mode", "thru", "proposed", "gate", "executed", "status", ""].forEach(function (h) {
      head.appendChild(el("th", { text: h })); });
    t.appendChild(head);
    runsForSession().forEach(function (r) {
      var tr = el("tr", { class: r.run_dir === S.selectedRun ? "selected" : "" },
        el("td", { text: r.run_dir.replace(/^runs\//, "") }), el("td", { text: r.strategy }), el("td", { text: r.planner_mode }),
        el("td", { text: r.through_sequence == null ? "-" : String(r.through_sequence) }), el("td", { text: r.proposed_action || "-" }),
        el("td", { text: r.gate || "-" }), el("td", { text: r.executed_action }), el("td", { text: r.status }),
        el("td", null,
          el("button", { title: "use as Before", text: "B", onclick: function (e) { e.stopPropagation(); setCmp("cmp-before", r.run_dir); } }),
          el("button", { title: "use as After", text: "A", onclick: function (e) { e.stopPropagation(); setCmp("cmp-after", r.run_dir); } })));
      tr.addEventListener("click", function () { S.selectedRun = r.run_dir; renderRuns(); loadRun(); });
      t.appendChild(tr);
    });
  }

  function renderOps(ops) {
    var ul = document.getElementById("ops");
    ul.textContent = "";
    ops.forEach(function (o) {
      ul.appendChild(el("li", { title: o.op_id + (o.error ? "\n" + o.error : ""),
        text: o.created_at.slice(11, 19) + " " + o.interface + " " + o.kind + " " + o.status }));
    });
  }

  function renderCompareSelectors() {
    ["cmp-before", "cmp-after"].forEach(function (id) {
      var sel = document.getElementById(id), keep = sel.value || localStorage.getItem(id);
      sel.textContent = "";
      S.runs.forEach(function (r) {
        sel.appendChild(el("option", { value: r.run_dir, text: r.run_dir + " (" + r.strategy + ", " + r.planner_mode + ")" }));
      });
      if (keep && S.runs.some(function (r) { return r.run_dir === keep; })) sel.value = keep;
    });
  }
  function setCmp(id, value) { document.getElementById(id).value = value; try { localStorage.setItem(id, value); } catch (e) { /* ignore */ } }

  function renderRunViews() { renderStages(); renderChat(); renderInspector(); }

  function card(title, big, sub, cls) {
    return el("div", { class: "card" }, el("h4", { text: title }), el("div", { class: "big " + (cls || ""), text: big }),
      sub ? el("div", { class: "small muted", text: sub }) : null);
  }

  function renderChat() {
    var chat = document.getElementById("chat");
    chat.textContent = "";
    var pause = S.events.filter(function (e) { return e.type === "session.paused"; })[0];
    var req = S.events.filter(function (e) { return e.type === "session.resume_requested"; }).pop();
    if (pause) chat.appendChild(el("div", { class: "small muted", text: "Agent paused at " + pause.event_id + " (" + pause.payload.reason +
      "). " + S.events.filter(function (e) { return e.sequence > pause.sequence; }).length + " events arrived while it was stopped." }));
    if (req) chat.appendChild(el("div", { class: "msg user" }, el("div", { class: "who", text: "user · " + req.event_id }), req.payload.message));
    var d = S.runData;
    if (!d) { chat.appendChild(el("p", { class: "muted", text: "No run selected. Use Step or Run to resume the agent." })); return; }
    var p = d.progress, a = d.artifacts || {}, prop = a["proposal.json"], gate = a["gate.json"], rcpt = a["receipt.json"];
    var label = p.strategy + " / planner_mode=" + p.planner_mode + (p.provider ? " (" + p.provider + " " + p.model + ")" : "");
    if (prop) chat.appendChild(el("div", { class: "msg" }, el("div", { class: "who", text: "assistant · " + label }), prop.explanation));
    var cards = el("div", { class: "cards" });
    cards.appendChild(prop ? card("Proposed action", prop.action + " " + (prop.commit || "?") + " → " + prop.environment,
      "blockers: " + (prop.blockers.join(", ") || "none")) : card("Proposed action", "not yet", "planner.propose has not run"));
    cards.appendChild(gate ? card("Gate outcome", gate.outcome, gate.reason_codes.join(", "), gate.outcome === "allowed" ? "check-pass" : "check-fail")
      : card("Gate outcome", "not yet", "policy.validate has not run"));
    var finished = p.status === "completed" || p.status === "failed";
    cards.appendChild(rcpt ? card("Executed action", "mock deploy", rcpt.receipt_id + (rcpt.reused_existing ? " (existing receipt)" : ""), "check-pass")
      : card("Executed action", finished ? "nothing" : "not yet", finished ? "no tool.deploy span, no receipt" : "tool.deploy has not been reached"));
    chat.appendChild(cards);
    if (p.status === "active") chat.appendChild(el("div", { class: "notice", text: "Run in progress. Next stage: " + p.next_stage + ". Step again or finish it." }));
    if (p.status === "failed") chat.appendChild(el("div", { class: "notice bad", text: (p.exercise_incomplete ?
      "Unfinished exercise (expected until you complete exercises.py; the runtime is fine): " : "Run failed: ") + p.error }));
  }

  function jsonBlock(title, data, open) {
    if (data === undefined || data === null) return null;
    return el("details", open ? { open: "" } : null, el("summary", { text: title }), el("pre", { text: TraceView.pretty(data) }));
  }

  function renderInspector() {
    document.querySelectorAll("#tabs button").forEach(function (b) { b.classList.toggle("active", b.getAttribute("data-tab") === S.tab); });
    var body = document.getElementById("inspector-body");
    body.textContent = "";
    var d = S.runData, a = (d && d.artifacts) || {};
    var none = function (t) { body.appendChild(el("p", { class: "muted", text: t })); };
    if (S.tab === "events") return renderEvents(body);
    if (S.tab === "checks") return renderChecks(body);
    if (!d) return none("Select a run.");
    if (S.tab === "state") {
      body.appendChild(el("p", { class: "small muted", text: "Derived working state (state.json), the snapshot or summary it started from, and what changed." }));
      [["state.json", true], ["state-diff.json", true], ["snapshot-before.json"], ["memory-loaded.json"]].forEach(function (x) {
        var b = jsonBlock(x[0], a[x[0]], x[1]); if (b) body.appendChild(b); });
      if (!a["state.json"]) none("memory.project has not run yet.");
    } else if (S.tab === "model") {
      body.appendChild(el("p", { class: "small muted", text: d.progress.planner_mode === "simulated" ?
        "Simulated planner: no LLM request was made. This is the exact context it consumed." :
        "Exact messages sent to the model (credentials are never recorded)." }));
      ["planner-input.json", "llm-request.json", "llm-response.json", "context.json"].forEach(function (n, i) {
        var b = jsonBlock(n, a[n], i === 0); if (b) body.appendChild(b); });
      if (!a["context.json"]) none("context.build has not run yet.");
    } else if (S.tab === "tools") {
      var calls = a["tools.json"] || [];
      if (!calls.length) none("No tool calls recorded in this run.");
      calls.forEach(function (c) {
        body.appendChild(jsonBlock(c.stage + " → " + c.tool + (c.read_only ? " (read-only)" : " (side effect)"), c, true));
      });
      body.appendChild(a["receipt.json"] ? jsonBlock("receipt.json", a["receipt.json"], true) :
        el("p", { class: "muted", text: "No receipt: the mock deployment tool did not execute." }));
    } else if (S.tab === "validation") {
      body.appendChild(el("p", { class: "small muted", text: "The gate reads the event store directly. It does not trust the context or the cited evidence IDs." }));
      [["proposal.json", true], ["gate.json", true]].forEach(function (x) { var b = jsonBlock(x[0], a[x[0]], x[1]); if (b) body.appendChild(b); });
      if (!a["gate.json"]) none("policy.validate has not run yet.");
    } else if (S.tab === "trace") {
      var box = el("div");
      body.appendChild(box);
      TraceView.renderTrace(box, { spans: d.spans, artifacts: d.artifacts, progress: d.progress, facts: d.facts, run_dir: d.run_dir });
      if (d.progress.status === "active") body.appendChild(el("p", { class: "small muted",
        text: "The root agent.resume span is written when the run finishes; stage spans appear as they complete." }));
    }
  }

  function renderEvents(body) {
    var cited = {};
    var ctx = S.runData && S.runData.artifacts["context.json"];
    ((ctx && ctx.evidence_event_ids) || []).forEach(function (id) { cited[id] = true; });
    var t = el("table", { class: "kv" });
    S.events.forEach(function (e) {
      var payload = Object.keys(e.payload).map(function (k) { return k + "=" + TraceView.pretty(e.payload[k]).replace(/^"|"$/g, ""); }).join(", ");
      t.appendChild(el("tr", { style: cited[e.event_id] ? "font-weight:600" : "" },
        el("td", { class: "mono", text: e.sequence + " " + e.event_id + (cited[e.event_id] ? " ← cited" : "") }),
        el("td", { class: "mono", text: e.type + " (" + e.actor + ") " + payload })));
    });
    body.appendChild(el("p", { class: "small muted", text: "Authoritative event history for this session. Bold rows are cited by the selected run's context." }));
    body.appendChild(t);
  }

  function renderChecks(body) {
    var r = S.lastCheck;
    if (!r) { body.appendChild(el("p", { class: "muted", text: "Run the exercise checks from the controls (same as `workshop check`)." })); return; }
    body.appendChild(el("p", { text: "strategy=" + r.strategy + ": " + r.summary.pass + " passed, " + r.summary.todo +
      " unfinished, " + r.summary.fail + " failed, " + r.summary.error + " errors" }));
    var t = el("table", { class: "kv" });
    r.results.forEach(function (c) {
      t.appendChild(el("tr", null, el("td", { class: "check-" + c.status, text: c.status.toUpperCase() + "  [" + c.exercise + "] " + c.name }),
        el("td", { class: "small", text: c.detail })));
    });
    body.appendChild(t);
  }

  // ------------------------------------------------------------- controls
  function runParams(extra) {
    var p = { session: S.session, strategy: val("strategy"), planner: val("planner"), provider: val("provider"),
              target_environment: val("target-env") };
    Object.keys(extra || {}).forEach(function (k) { p[k] = extra[k]; });
    return p;
  }
  function val(id) { var n = document.getElementById(id); return n ? n.value : null; }

  document.addEventListener("click", function (e) {
    var btn = e.target.closest("button[data-action]");
    if (btn) {
      var kind = btn.getAttribute("data-action");
      if (kind !== "prepare" && kind !== "check" && kind !== "export" && !S.session) return toast("Prepare a session first.", true);
      if (kind === "prepare") {
        var name = document.getElementById("new-session").value.trim();
        if (!name) return toast("Enter a session name.", true);
        S.session = name; try { localStorage.setItem("session", name); } catch (err) { /* ignore */ }
        return act("prepare", { session: name });
      }
      if (kind === "reset") {
        if (!confirm("Reset session '" + S.session + "' to the original fixture? Run directories are kept.")) return;
        return act("reset", { session: S.session });
      }
      if (kind === "append") return act("append", { session: S.session, fixture: btn.getAttribute("data-fixture") });
      if (kind === "step") return act("step", runParams({ until_done: false }));
      if (kind === "run") return act("run", runParams({ until_done: true }));
      if (kind === "finish") return act("step", { session: S.session, until_done: true });
      if (kind === "abandon") return act("abandon", { session: S.session });
      if (kind === "check") return act("check", { strategy: val("check-strategy") });
      if (kind === "export") return act("export", { before: val("cmp-before"), after: val("cmp-after"),
        followups: runsForSession().filter(function (r) { return r.run_dir !== val("cmp-before") && r.run_dir !== val("cmp-after") && r.status === "completed" && r.strategy !== "baseline"; })
          .map(function (r) { return r.run_dir; }) });
      return;
    }
    var tab = e.target.closest("#tabs button");
    if (tab) { S.tab = tab.getAttribute("data-tab"); renderInspector(); }
  });

  document.getElementById("session-select").addEventListener("change", function (e) {
    S.session = e.target.value; S.selectedRun = null;
    try { localStorage.setItem("session", S.session); } catch (err) { /* ignore */ }
    refreshAll();
  });
  document.getElementById("planner").addEventListener("change", function () {
    document.getElementById("provider-row").style.display = val("planner") === "live" ? "" : "none";
  });
  ["cmp-before", "cmp-after"].forEach(function (id) {
    document.getElementById(id).addEventListener("change", function (e) { setCmp(id, e.target.value); });
  });
  document.getElementById("cmp-show").addEventListener("click", function () {
    var b = val("cmp-before"), a = val("cmp-after");
    if (!b || !a) return toast("Choose a before and an after run.", true);
    get("/api/compare?before=" + encodeURIComponent(b) + "&after=" + encodeURIComponent(a))
      .then(function (data) { TraceView.renderComparison(document.getElementById("comparison"), data); })
      .catch(function (err) { toast(err.message, true); });
  });

  // ----------------------------------------------------------------- start
  get("/api/config").then(function (cfg) {
    S.config = cfg;
    document.getElementById("root-path").textContent = cfg.root;
    var fill = function (id, items, labeler) {
      var sel = document.getElementById(id);
      items.forEach(function (x) { sel.appendChild(el("option", { value: x, text: labeler ? labeler(x) : x })); });
    };
    fill("strategy", cfg.strategies);
    document.getElementById("strategy").value = "student";
    fill("planner", cfg.planner_modes);
    fill("provider", Object.keys(cfg.providers), function (p) {
      var s = cfg.providers[p]; return p + " (" + s.model + (s.key_configured ? "" : ", no key configured") + ")"; });
    document.getElementById("provider-row").style.display = "none";
    return refreshAll();
  }).then(function () {
    // Re-attach to operations still pending from before a refresh (read-only polling).
    pending().forEach(function (id) { watch(id).then(function (op) { report(op); refreshAll(); }); });
    setInterval(function () { if (!S.busy && document.visibilityState === "visible") refreshAll(null, true); }, 5000);
  });
})();
