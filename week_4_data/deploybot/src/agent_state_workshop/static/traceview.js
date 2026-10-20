/* Shared trace viewer for the offline comparison export and the GUI.
 * Plain DOM, no dependencies. All recorded data is inserted with
 * textContent, never innerHTML, so artifact contents cannot inject markup. */
(function (global) {
  "use strict";

  function el(tag, attrs) {
    var node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        if (k === "class") node.className = attrs[k];
        else if (k === "text") node.textContent = attrs[k];
        else if (k === "style") node.setAttribute("style", attrs[k]);
        else if (k.slice(0, 2) === "on") node.addEventListener(k.slice(2), attrs[k]);
        else node.setAttribute(k, attrs[k]);
      });
    }
    for (var i = 2; i < arguments.length; i++) {
      var c = arguments[i];
      if (c === null || c === undefined || c === false) continue;
      node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    }
    return node;
  }

  function fmtMs(ms) {
    if (ms >= 60000) return (ms / 60000).toFixed(1) + " min";
    if (ms >= 1000) return (ms / 1000).toFixed(2) + " s";
    return ms.toFixed(2) + " ms";
  }
  function durMs(s) { return (s.end_time_unix_nano - s.start_time_unix_nano) / 1e6; }
  function fmtVal(v) {
    if (Array.isArray(v)) return "[" + v.join(", ") + "]";
    if (v === null || v === undefined) return "-";
    if (typeof v === "object") return JSON.stringify(v);
    return String(v);
  }
  function pretty(obj) { return JSON.stringify(obj, null, 2); }

  function buildTree(spans) {
    var byId = {}, roots = [];
    spans.forEach(function (s) { byId[s.span_id] = { span: s, children: [] }; });
    Object.keys(byId).forEach(function (id) {
      var n = byId[id], p = byId[n.span.parent_span_id || ""];
      (p ? p.children : roots).push(n);
    });
    function sort(list) {
      list.sort(function (a, b) { return a.span.start_time_unix_nano - b.span.start_time_unix_nano; });
      list.forEach(function (n) { sort(n.children); });
    }
    sort(roots);
    return roots;
  }

  /* Domain outcome badge for a span, separate from its OTel status. */
  function domainBadge(s) {
    var a = s.attributes || {};
    if (s.name === "policy.validate") {
      var allowed = a["workshop.gate.outcome"] === "allowed";
      return el("span", { class: "badge " + (allowed ? "ok" : "bad"), text: "gate " + a["workshop.gate.outcome"] });
    }
    if (s.name === "planner.propose") return el("span", { class: "badge", text: "proposed " + a["workshop.proposal.action"] });
    if (s.name === "tool.deploy") return el("span", { class: "badge ok", text: "EXECUTED mock deploy" });
    if (s.name === "context.build" && a["workshop.annotation.complete"] === false)
      return el("span", { class: "badge warn", text: "annotation incomplete" });
    if (s.name === "model.generate") return el("span", { class: "badge sim", text: "LLM call" });
    return null;
  }

  function statusBadge(s) {
    var code = (s.status || {}).code || "UNSET";
    var cls = code === "ERROR" ? "bad" : "";
    return el("span", { class: "badge " + cls, title: "OpenTelemetry span status (runtime success, not domain outcome)",
      text: "span " + code });
  }

  function artifactFor(name, artifacts) {
    return artifacts && Object.prototype.hasOwnProperty.call(artifacts, name) ? artifacts[name] : undefined;
  }

  function renderDetail(target, s, artifacts) {
    target.textContent = "";
    var a = s.attributes || {};
    var head = el("div", null,
      el("h3", { text: s.name }),
      el("div", { class: "mono muted", text: "span " + s.span_id + "  parent " + (s.parent_span_id || "-") +
        "  " + fmtMs(durMs(s)) }),
      el("div", { class: "domain-note" }, statusBadge(s), domainBadge(s)));
    target.appendChild(head);
    var kv = el("table", { class: "kv" });
    Object.keys(a).sort().forEach(function (k) {
      kv.appendChild(el("tr", null, el("td", { class: "mono", text: k }), el("td", { class: "mono", text: fmtVal(a[k]) })));
    });
    target.appendChild(el("h3", { text: "Attributes" }));
    target.appendChild(kv);
    if ((s.events || []).length) {
      target.appendChild(el("h3", { text: "Span events" }));
      s.events.forEach(function (ev) {
        var t = el("table", { class: "kv" });
        Object.keys(ev.attributes || {}).forEach(function (k) {
          t.appendChild(el("tr", null, el("td", { class: "mono", text: k }), el("td", { class: "mono", text: fmtVal(ev.attributes[k]) })));
        });
        target.appendChild(el("div", null, el("strong", { text: ev.name }), t));
      });
    }
    var refs = Object.keys(a).filter(function (k) { return k.indexOf("workshop.artifact.") === 0; });
    refs.forEach(function (k) {
      var data = artifactFor(a[k], artifacts);
      if (data === undefined) return;
      target.appendChild(el("details", { open: "" }, el("summary", { text: "Artifact " + a[k] }),
        el("pre", { text: pretty(data) })));
    });
  }

  /* Render one run's trace: header, warnings, expandable tree with duration bars, detail pane. */
  function renderTrace(container, run, opts) {
    opts = opts || {};
    container.textContent = "";
    var spans = run.spans || [], facts = run.facts || {}, p = run.progress || {};
    var mode = p.planner_mode || facts.planner_mode;
    var header = el("div", null,
      el("h2", { text: opts.title || (p.strategy + " / " + mode) }),
      el("div", null,
        el("span", { class: "badge sim", text: "planner_mode=" + mode }),
        el("span", { class: "badge", text: "strategy=" + p.strategy }),
        el("span", { class: "badge", text: "status=" + p.status }),
        mode === "simulated" ? el("span", { class: "muted", text: " no LLM inference, no token metrics" }) :
          el("span", { class: "muted", text: " " + (p.provider || "") + " " + (p.model || "") })),
      el("div", { class: "mono muted", text: "trace " + (p.trace_id || "-") + "  " + (run.run_dir || "") }));
    container.appendChild(header);

    if (facts.annotation_complete === false)
      container.appendChild(el("div", { class: "notice", text: "Evidence annotation on context.build is incomplete. Missing: " +
        (facts.annotation_missing || []).join(", ") }));
    if ((facts.unsourced_facts || []).length)
      container.appendChild(el("div", { class: "notice bad", text: "Context presents facts with no source event: " +
        facts.unsourced_facts.join(", ") }));
    if (!spans.length) { container.appendChild(el("p", { class: "muted", text: "No spans recorded yet." })); return; }

    var t0 = Math.min.apply(null, spans.map(function (s) { return s.start_time_unix_nano; }));
    var t1 = Math.max.apply(null, spans.map(function (s) { return s.end_time_unix_nano; }));
    var total = Math.max(1, t1 - t0);
    var table = el("table", { class: "tv-tree" });
    var detail = el("div", { class: "tv-detail" });
    var rows = [];

    function addNode(node, depth, parentRows) {
      var s = node.span, hasKids = node.children.length > 0;
      var toggle = el("span", { class: "tv-toggle", text: hasKids ? "▾" : "" });
      var left = ((s.start_time_unix_nano - t0) / total) * 100;
      var width = Math.max(0.4, ((s.end_time_unix_nano - s.start_time_unix_nano) / total) * 100);
      var tr = el("tr", null,
        el("td", { class: "tv-name", style: "padding-left:" + (6 + depth * 16) + "px" }, toggle, s.name),
        el("td", { class: "mono muted", text: s.span_id }),
        el("td", { class: "mono", style: "text-align:right", text: fmtMs(durMs(s)) }),
        el("td", { class: "tv-barcell" }, el("div", { class: "tv-track" },
          el("div", { class: "tv-bar" + (depth === 0 ? " root" : ""), style: "left:" + left + "%;width:" + width + "%" }))),
        el("td", null, statusBadge(s), domainBadge(s)));
      var kids = [];
      tr.addEventListener("click", function (e) {
        if (e.target === toggle && hasKids) {
          var collapsed = toggle.textContent === "▸";
          toggle.textContent = collapsed ? "▾" : "▸";
          setVisible(kids, collapsed);
          return;
        }
        rows.forEach(function (r) { r.classList.remove("selected"); });
        tr.classList.add("selected");
        renderDetail(detail, s, run.artifacts);
      });
      table.appendChild(tr);
      rows.push(tr);
      parentRows.forEach(function (list) { list.push(tr); });
      node.children.forEach(function (c) { addNode(c, depth + 1, parentRows.concat([kids])); });
    }
    function setVisible(list, show) { list.forEach(function (r) { r.style.display = show ? "" : "none"; }); }
    buildTree(spans).forEach(function (n) { addNode(n, 0, []); });
    container.appendChild(el("div", { style: "overflow-x:auto" }, table));
    container.appendChild(el("div", { class: "muted", style: "font-size:12px;margin-top:4px",
      text: "Click a span for attributes, events, and linked artifacts. Click ▾ to collapse. " +
        "Bars use recorded start/end timestamps; in stepped runs the root also spans the time between steps." }));
    container.appendChild(detail);
    var wanted = opts.selectSpan || "context.build";
    var initial = spans.filter(function (s) { return s.name === wanted; })[0] || buildTree(spans)[0].span;
    renderDetail(detail, initial, run.artifacts);
  }

  function renderComparisonTable(container, data) {
    container.textContent = "";
    var t = el("table", { class: "cmp-table" },
      el("tr", null, el("th", { text: "" }), el("th", { text: "Before", style: "width:41%" }), el("th", { text: "After", style: "width:41%" })));
    data.rows.forEach(function (r) {
      t.appendChild(el("tr", { class: r.changed ? "changed" : "" },
        el("th", { text: r.label }), el("td", { text: r.before }), el("td", { text: r.after })));
    });
    container.appendChild(t);
    (data.notices || []).forEach(function (n) { container.appendChild(el("div", { class: "notice", text: n })); });
  }

  function renderArtifacts(container, run) {
    var names = ["state.json", "state-diff.json", "context.json", "planner-input.json", "proposal.json",
      "gate.json", "tools.json", "receipt.json", "llm-request.json", "llm-response.json", "memory-loaded.json"];
    names.forEach(function (n) {
      var data = artifactFor(n, run.artifacts);
      if (data === undefined) return;
      container.appendChild(el("details", null, el("summary", { text: n }), el("pre", { text: pretty(data) })));
    });
    if (artifactFor("receipt.json", run.artifacts) === undefined)
      container.appendChild(el("p", { class: "muted", text: "receipt.json absent: no deployment was executed in this run." }));
  }

  /* Full comparison page: summary table, then each trace with its artifacts. */
  function renderComparison(root, data) {
    root.textContent = "";
    var summary = el("div", { class: "panel" });
    root.appendChild(el("section", null, el("h2", { text: "Focused comparison" }), summary));
    renderComparisonTable(summary, data);
    var grid = el("div", { class: "two-col", style: "margin-top:12px" });
    [["Before", data.before], ["After", data.after]].forEach(function (pair) {
      var panel = el("div", { class: "panel" });
      var traceBox = el("div");
      panel.appendChild(traceBox);
      renderTrace(traceBox, pair[1], { title: pair[0] + ": " + pair[1].progress.strategy + " / " + pair[1].progress.planner_mode });
      var arts = el("div");
      panel.appendChild(el("h3", { text: "Artifacts" }));
      panel.appendChild(arts);
      renderArtifacts(arts, pair[1]);
      grid.appendChild(panel);
    });
    root.appendChild(grid);
    var diffs = el("div", { class: "panel", style: "margin-top:12px" }, el("h2", { text: "Artifact differences" }));
    Object.keys(data.diffs || {}).forEach(function (name) {
      var changes = data.diffs[name] || [];
      var t = el("table", { class: "kv" });
      changes.forEach(function (c) {
        t.appendChild(el("tr", null, el("td", { class: "mono", text: c.change + " " + c.path }),
          el("td", { class: "mono", text: fmtVal(c.before) + "  →  " + fmtVal(c.after) })));
      });
      diffs.appendChild(el("details", null, el("summary", { text: name + " (" + changes.length + " changes)" }),
        changes.length ? t : el("p", { class: "muted", text: "No differences." })));
    });
    root.appendChild(diffs);
  }

  global.TraceView = { el: el, renderTrace: renderTrace, renderComparison: renderComparison,
    renderComparisonTable: renderComparisonTable, renderArtifacts: renderArtifacts, fmtMs: fmtMs, pretty: pretty };
})(window);
