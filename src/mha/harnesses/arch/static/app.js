/* A3 Workbench — event-driven UI over the arch harness protocol.
   One SSE connection in; POST /input, /permission, /interrupt out.
   Late joiners are replayed by the server, so refresh rebuilds everything. */
"use strict";

const S = {
  conn: "connecting",      // connecting | connected | disconnected | complete
  ready: null,
  arch: null,              // latest arch_state event
  changed: null,           // {kind, id} from the latest arch_state
  transcript: [],          // {t:"user"|"agent"|"notice"|"turn", ...}
  stream: null,            // in-flight agent text
  running: false,
  permission: null,        // pending toplevel_approval / finalize request
  artifacts: [],
  finalized: false,
  tab: "transcript",
  detail: null,            // component id open in the Components tab
  selected: null,          // component id highlighted in the diagram
  drill: null,             // component id whose facet fills the hero pane
  draft: "",
  lastCount: -1,
};

const $ = (id) => document.getElementById(id);

function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const c of children) if (c != null) node.append(c);
  return node;
}

/* ================= event intake ================= */

const NOTICE_GLYPHS = {
  brief: "≡", component: "+", connect: "→", flow: "◇", decide: "◆",
  ask: "?", answer: "✓", expand: "▸", amend_toplevel: "±", done: "⛳",
  read: "≡", kg_query: "⌕", WebSearch: "⌕", WebFetch: "≡", skill: "☰",
};

function noticeFor(call) {
  let arg = "";
  try {
    const a = JSON.parse(call.arguments_json || "{}");
    arg = a.id || a.component_id || a.src && `${a.src} → ${a.dst}` || a.topic ||
      a.path || a.question || a.query || a.url || a.name || "";
  } catch { /* leave arg empty */ }
  const glyph = NOTICE_GLYPHS[call.name] || "·";
  return `${glyph} ${call.name}${arg ? " " + String(arg).slice(0, 70) : ""}`;
}

function onHarnessEvent(ev) {
  const { event, data } = ev;
  if (event === "run_start") {
    S.transcript.push({ t: "user", text: data.task || "" });
    S.running = true;
    S.stream = "";
    renderComposer(); renderPill(); renderTranscriptIfActive();
  } else if (event === "assistant_delta") {
    if (S.stream === null) S.stream = "";
    S.stream += data.text || "";
    updateStream();
  } else if (event === "assistant") {
    if (data.content) S.transcript.push({ t: "agent", text: data.content });
    S.stream = S.running ? "" : null;
    for (const call of data.tool_calls || []) {
      S.transcript.push({ t: "notice", text: noticeFor(call) });
    }
    renderTranscriptIfActive();
  } else if (event === "tool_result" && data.is_error) {
    S.transcript.push({ t: "notice", err: true, text: `✗ ${data.name} — see agent's next step` });
    renderTranscriptIfActive();
  } else if (event === "abort") {
    S.transcript.push({ t: "notice", err: true, text: `✗ aborted: ${data.reason}` });
    renderTranscriptIfActive();
  }
}

function onEvent(ev) {
  switch (ev.type) {
    case "ready":
      S.ready = ev;
      if (S.conn !== "complete") S.conn = "connected";
      renderStatus(); renderRail(); renderPill(); renderComposer();
      break;
    case "harness_event":
      onHarnessEvent(ev);
      break;
    case "arch_state":
      S.arch = ev;
      S.changed = ev.changed || null;
      if (ev.phase === "finalized" && !S.finalized) {
        S.finalized = true;
        S.conn = "complete";
        S.permission = null;
        showOverlay();
      }
      renderAll();
      break;
    case "permission_request":
      if (ev.kind === "toplevel_approval" || ev.kind === "finalize") {
        S.permission = ev;
        if (ev.kind === "finalize") S.artifacts = ev.artifacts || [];
        renderComposer();
      }
      break;
    case "turn_end":
      S.running = false;
      if (S.stream) S.transcript.push({ t: "agent", text: S.stream });
      S.stream = null;
      S.transcript.push({ t: "turn", status: ev.status });
      renderComposer(); renderPill(); renderTranscriptIfActive();
      break;
    case "error":
      S.transcript.push({ t: "notice", err: true, text: `✗ ${ev.message}` });
      renderTranscriptIfActive();
      break;
    case "bye":
      if (!S.finalized) disconnect();
      break;
  }
}

function connect() {
  const es = new EventSource("/events");
  es.onmessage = (e) => onEvent(JSON.parse(e.data));
  es.onerror = () => {
    es.close();
    if (!S.finalized) disconnect();
  };
}

function disconnect() {
  S.conn = "disconnected";
  S.running = false;
  $("banner").hidden = false;
  renderStatus(); renderComposer(); renderPill();
}

/* ================= actions ================= */

async function post(path, body) {
  try {
    await fetch(path, { method: "POST", body: JSON.stringify(body || {}) });
  } catch { /* surfaced via connection state */ }
}

function sendMessage(text) {
  if (!text.trim()) return;
  S.draft = "";
  post("/input", { text });
}

function respond(approved, feedback) {
  const req = S.permission;
  if (!req) return;
  S.permission = null;
  S.running = true;
  post("/permission", { id: req.id, approved, feedback: feedback || "" });
  renderComposer(); renderPill();
}

/* ================= regions ================= */

function renderStatus() {
  if (S.ready) {
    $("sb-model").textContent = S.ready.model || "—";
    const repo = S.ready.repo || "—";
    $("sb-repo").textContent = repo.split("/").slice(-2).join("/");
    $("sb-session").textContent = S.ready.run_id || "—";
  }
  const chip = $("sb-conn");
  const labels = {
    connecting: "Connecting…", connected: "Connected",
    disconnected: "Disconnected", complete: "Session complete",
  };
  chip.className = `chip conn ${S.conn}`;
  chip.lastElementChild.textContent = labels[S.conn];
}

function renderPill() {
  const pill = $("d-live");
  let mode = "connecting", label = "connecting";
  if (S.conn === "disconnected") { mode = "stale"; label = "stale"; }
  else if (S.conn === "complete") { mode = "live"; label = "final"; }
  else if (S.conn === "connected") {
    if (S.running) { mode = "updating"; label = "updating"; }
    else { mode = "live"; label = "live"; }
  }
  pill.className = `pill ${mode}`;
  pill.lastElementChild.textContent = label;
  $("d-cue").textContent =
    S.changed ? `last change · ${S.changed.kind} ${S.changed.id}` : "";
}

function state() { return (S.arch && S.arch.state) || null; }

function renderDiagram() {
  const st = state();
  const count = st ? Object.keys(st.components).length : 0;
  $("dempty").style.display = count ? "none" : "flex";
  if (!count) return;
  try {
    Diagram.render(st, {
      changedId: S.changed && S.changed.kind === "component" ? S.changed.id : null,
      selectedId: S.selected,
      drillId: S.drill,
    });
    $("derror").hidden = true;
    if (count !== S.lastCount || S.drillChanged) {
      Diagram.fit();
      S.lastCount = count;
      S.drillChanged = false;
    }
  } catch (err) {
    $("derror").hidden = false;
    $("derror-msg").textContent = String(err);
  }
  const crumb = $("d-crumb");
  crumb.textContent = S.drill ? `← system / ${S.drill}` : "";
}

function setDrill(id) {
  S.drill = id;
  S.drillChanged = true;
  renderDiagram();
}

function renderRail() {
  const st = state();
  const dl = $("rail-session");
  dl.replaceChildren(
    h("dt", {}, "model"), h("dd", {}, (S.ready && S.ready.model) || "—"),
    h("dt", {}, "session"), h("dd", {}, (S.ready && S.ready.run_id) || "—"),
    h("dt", {}, "phase"), h("dd", {}, (S.arch && S.arch.phase) || "—"),
  );
  const counts = tabCounts();
  const jump = $("rail-jump");
  jump.replaceChildren(
    h("a", { class: "active" }, h("span", {}, "Diagram")),
    ...[["components", "Components"], ["decisions", "Decisions"],
        ["questions", "Questions"], ["flows", "Flows"]].map(([key, label]) =>
      h("a", { onclick: () => switchTab(key) },
        h("span", {}, label), h("span", { class: "n" }, String(counts[key])))
    ),
  );
  const pending = st ? (st.obligations || []).filter((o) => o.status === "pending") : [];
  $("rail-obligations-block").hidden = pending.length === 0;
  $("rail-obligations").replaceChildren(
    ...pending.map((o, i) =>
      h("li", { class: i === 0 ? "head" : "" }, `${o.component_id} (${o.facet})`))
  );
}

/* ---- dock ---- */

function tabCounts() {
  const st = state();
  return {
    components: st ? Object.keys(st.components).length : 0,
    decisions: st ? st.decisions.length : 0,
    questions: st ? st.questions.length : 0,
    flows: st ? st.flows.length : 0,
  };
}

function switchTab(tab) {
  S.tab = tab;
  if (tab !== "components") S.detail = null;
  renderDock();
}

function renderTabs() {
  const counts = tabCounts();
  const defs = [
    ["transcript", "Transcript", null],
    ["components", "Components", counts.components],
    ["decisions", "Decisions", counts.decisions],
    ["questions", "Questions", counts.questions],
    ["flows", "Flows", counts.flows],
  ];
  $("dock-tabs").replaceChildren(
    ...defs.map(([key, label, n]) =>
      h("button", { class: key === S.tab ? "active" : "", onclick: () => switchTab(key) },
        label, n ? h("span", { class: "n" }, String(n)) : null))
  );
}

function renderDock() {
  renderTabs();
  const body = $("dock-body");
  if (S.tab === "transcript") return renderTranscript(body);
  if (S.tab === "components") {
    return S.detail ? renderComponentDetail(body, S.detail) : renderComponents(body);
  }
  if (S.tab === "decisions") return renderDecisions(body);
  if (S.tab === "questions") return renderQuestions(body);
  if (S.tab === "flows") return renderFlows(body);
}

/* transcript */

function transcriptNode(item) {
  if (item.t === "user") {
    return h("div", { class: "t-user" }, h("div", { class: "who" }, "you"),
      h("div", { class: "msg" }, item.text));
  }
  if (item.t === "agent") {
    return h("div", { class: "t-agent" }, h("div", { class: "who" }, "agent"),
      h("div", { class: "msg" }, item.text));
  }
  if (item.t === "notice") {
    return h("div", { class: "t-notice" + (item.err ? " err" : "") }, item.text);
  }
  return h("div", { class: "t-turn" }, `turn ${item.status}`);
}

function renderTranscript(body) {
  const wrap = h("div", { id: "transcript" }, ...S.transcript.map(transcriptNode));
  if (S.stream !== null) {
    const msg = h("div", { class: "msg" }, S.stream);
    msg.append(h("span", { class: "caret" }));
    wrap.append(h("div", { class: "t-agent", id: "streaming" },
      h("div", { class: "who" }, "agent"), msg));
  }
  body.replaceChildren(wrap);
  body.scrollTop = body.scrollHeight;
}

function renderTranscriptIfActive() {
  if (S.tab === "transcript") renderTranscript($("dock-body"));
}

function updateStream() {
  if (S.tab !== "transcript") return;
  const node = document.getElementById("streaming");
  if (!node) return renderTranscriptIfActive();
  const msg = node.querySelector(".msg");
  msg.firstChild.textContent = S.stream;
  const body = $("dock-body");
  body.scrollTop = body.scrollHeight;
}

/* components */

function componentState(id) {
  const st = state();
  if (S.changed && S.changed.kind === "component" && S.changed.id === id) return "changed";
  if (st && (st.obligations || []).some((o) => o.component_id === id && o.status === "pending")) {
    return "owes facet";
  }
  return "stable";
}

function connCounts(id) {
  const st = state();
  if (!st) return [0, 0];
  const out = st.connections.filter((c) => c.src === id).length;
  const inn = st.connections.filter((c) => c.dst === id).length;
  return [out, inn];
}

function renderComponents(body) {
  const st = state();
  if (!st || !Object.keys(st.components).length) {
    body.replaceChildren(h("p", { class: "muted" }, "No components yet."));
    return;
  }
  body.replaceChildren(h("ul", { class: "rowlist" },
    ...Object.values(st.components).map((c) => {
      const cs = componentState(c.id);
      const [out, inn] = connCounts(c.id);
      return h("li", { class: "click", onclick: () => { S.detail = c.id; S.selected = c.id; renderDock(); renderDiagram(); } },
        h("code", {}, c.id),
        h("span", { class: "tag" }, c.kind),
        h("span", { class: "tag" + (cs !== "stable" ? " ink" : "") }, cs),
        h("span", { class: "grow muted" }, c.responsibility),
        h("span", { class: "muted mono" }, `${out} out · ${inn} in`),
        h("span", { class: "muted" }, "›"));
    })));
}

function facetSection(c) {
  const f = c.facet;
  if (!f) return null;
  const sec = h("section", {});
  sec.append(h("div", { class: "label" }, `${f.facet_kind} facet`));
  const table = (heads, rows) => {
    const t = h("table", { class: "kvtable" });
    t.append(h("tr", {}, ...heads.map((x) => h("th", {}, x))));
    for (const r of rows) t.append(h("tr", {}, ...r.map((x) => h("td", {}, x))));
    return t;
  };
  if (f.facet_kind === "api") {
    sec.append(table(["method", "route", "auth", "errors"],
      f.endpoints.map((e) => [e.method, e.route, e.auth, (e.errors || []).join(", ")])));
  } else if (f.facet_kind === "store") {
    sec.append(table(["entity", "keys", "fields"],
      f.entities.map((e) => [e.name, e.keys, (e.fields || []).join(", ")])));
    for (const p of f.access_patterns || []) sec.append(h("div", { class: "muted" }, `access: ${p}`));
    if (f.retention) sec.append(h("div", { class: "muted" }, `retention: ${f.retention}`));
  } else if (f.facet_kind === "queue") {
    sec.append(table(["message", "delivery", "ordering", "schema"],
      f.messages.map((m) => [m.name, m.delivery, m.ordering, m.schema])));
  } else if (f.facet_kind === "llm") {
    sec.append(table(["task", "tier", "contract", "fallback"],
      f.tasks.map((t) => [t.name, t.model_tier, t.prompt_contract, t.fallback])));
  } else if (f.facet_kind === "service") {
    for (const i of f.interface || []) sec.append(h("div", {}, `exposes: ${i}`));
    for (const m of f.modules || []) sec.append(h("div", { class: "muted" }, `module ${m.name}: ${m.purpose}`));
  } else if (f.facet_kind === "infra") {
    sec.append(table(["unit", "components", "scaling"],
      f.units.map((u) => [u.name, (u.components || []).join(", "), u.scaling_policy])));
    if (f.state_locality) sec.append(h("div", { class: "muted" }, `state: ${f.state_locality}`));
  }
  const drillable = f.facet_kind === "store" || f.facet_kind === "infra" ||
    (f.facet_kind === "service" && f.modules);
  if (drillable) {
    sec.append(h("button", { onclick: () => setDrill(c.id), style: "margin-top:6px" },
      "View internals in diagram"));
  }
  return sec;
}

function renderComponentDetail(body, id) {
  const st = state();
  const c = st && st.components[id];
  if (!c) { S.detail = null; return renderComponents(body); }
  const wrap = h("div", { class: "detail" });
  wrap.append(h("button", { class: "back", onclick: () => { S.detail = null; S.selected = null; renderDock(); renderDiagram(); } },
    "← All components"));
  wrap.append(h("h3", {}, h("code", {}, c.id), h("span", { class: "tag" }, c.kind),
    h("span", { class: "tag" + (componentState(id) !== "stable" ? " ink" : "") }, componentState(id))));
  const purpose = h("section", {});
  purpose.append(h("div", { class: "label" }, "Purpose"), h("div", {}, c.responsibility));
  if (c.tech) purpose.append(h("div", { class: "muted" }, `tech: ${c.tech}`));
  if (c.data_owned) purpose.append(h("div", { class: "muted" }, `owns: ${c.data_owned}`));
  if (c.failure_notes) purpose.append(h("div", { class: "muted" }, `on failure: ${c.failure_notes}`));
  wrap.append(purpose);
  const fs = facetSection(c);
  if (fs) wrap.append(fs);

  const edges = st.connections.filter((x) => x.src === id || x.dst === id);
  const conns = h("section", {});
  conns.append(h("div", { class: "label" }, `Connections (${edges.length})`));
  for (const e of edges) {
    conns.append(h("div", {},
      h("span", { class: "tag" }, e.src === id ? "out" : "in"), " ",
      h("code", {}, `${e.src} → ${e.dst}`), ` ${e.label} (${e.kind})`));
  }
  wrap.append(conns);

  const related = st.decisions.filter((d) =>
    (d.topic + " " + d.rationale).toLowerCase().includes(id.toLowerCase()));
  if (related.length) {
    const sec = h("section", {});
    sec.append(h("div", { class: "label" }, "Related decisions"));
    for (const d of related) sec.append(h("div", {}, `${d.topic} → ${d.choice}`));
    wrap.append(sec);
  }
  const qs = st.questions.filter((q) => q.question.toLowerCase().includes(id.toLowerCase()));
  if (qs.length) {
    const sec = h("section", {});
    sec.append(h("div", { class: "label" }, "Open questions"));
    for (const q of qs) sec.append(h("div", {}, q.question));
    wrap.append(sec);
  }
  body.replaceChildren(wrap);
}

/* decisions / questions / flows */

function renderDecisions(body) {
  const st = state();
  if (!st || !st.decisions.length) {
    return body.replaceChildren(h("p", { class: "muted" }, "No decisions recorded yet."));
  }
  body.replaceChildren(h("ul", { class: "rowlist" }, ...st.decisions.map((d) =>
    h("li", {},
      h("span", {}, d.topic),
      h("span", { class: "tag ink" }, d.choice),
      d.status === "deferred" ? h("span", { class: "tag" }, "deferred") : null,
      h("span", { class: "grow muted" }, d.rationale),
      h("span", { class: "tag" }, d.category)))));
}

function renderQuestions(body) {
  const st = state();
  if (!st || !st.questions.length) {
    return body.replaceChildren(h("p", { class: "muted" }, "No open questions."));
  }
  body.replaceChildren(h("ul", { class: "rowlist" }, ...st.questions.map((q) =>
    h("li", {},
      h("code", {}, q.id),
      h("span", { class: "grow" }, q.question),
      q.blocking && !q.resolution ? h("span", { class: "tag ink" }, "blocking") : null,
      h("span", { class: "tag" }, q.resolution || "open"),
      q.answer ? h("span", { class: "muted grow" }, q.answer) : null))));
}

function renderFlows(body) {
  const st = state();
  if (!st || !st.flows.length) {
    return body.replaceChildren(h("p", { class: "muted" }, "No flows recorded yet."));
  }
  const wrap = h("div", {});
  for (const f of st.flows) {
    const sec = h("section", { style: "margin-bottom:12px" });
    sec.append(h("div", {}, h("b", {}, f.name), " ", h("span", { class: "tag" }, f.kind)));
    const ol = h("ol", { style: "margin:4px 0 0 22px" });
    for (const s of f.steps) {
      ol.append(h("li", {}, h("code", {}, `${s.src} → ${s.dst}`), ` — ${s.action}`,
        s.note ? h("span", { class: "muted" }, `  (${s.note})`) : null));
    }
    sec.append(ol);
    wrap.append(sec);
  }
  body.replaceChildren(wrap);
}

/* ---- composer ---- */

function renderComposer() {
  const bar = $("commandbar");
  if (S.finalized) {
    bar.replaceChildren(h("div", { class: "lockbar" }, "Session finalized — read-only"));
    return;
  }
  if (S.permission) {
    const isFinal = S.permission.kind === "finalize";
    const card = h("div", { class: "decision" });
    card.append(h("div", { class: "dtitle" },
      isFinal ? "Finalize architecture?" : "Approve the top-level design?"));
    if (S.permission.summary) card.append(h("div", { class: "dsummary" }, S.permission.summary));
    if (isFinal && S.artifacts.length) {
      card.append(h("div", { class: "muted mono" }, `writes: ${S.artifacts.join("  ·  ")}`));
    }
    const feedback = h("div", { class: "feedback" }, );
    const ta = h("textarea", { placeholder: "What should change?" });
    feedback.append(ta, h("button", { onclick: () => respond(false, ta.value) }, "Send feedback"));
    feedback.hidden = true;
    card.append(h("div", { class: "actions" },
      h("button", { class: "approve", onclick: () => respond(true) },
        isFinal ? "Finalize architecture" : "Approve top level"),
      h("button", { class: "changes", onclick: () => { feedback.hidden = !feedback.hidden; ta.focus(); } },
        "Request changes")), feedback);
    bar.replaceChildren(card);
    return;
  }
  if (S.conn === "connecting") {
    bar.replaceChildren(h("div", { class: "lockbar" }, "Connecting to session…"));
    return;
  }
  if (S.conn === "disconnected") {
    bar.replaceChildren(h("div", { class: "lockbar" }, "Disconnected — input locked ",
      h("button", { onclick: () => location.reload() }, "Reconnect")));
    return;
  }
  if (S.running) {
    bar.replaceChildren(h("div", { class: "running" },
      h("span", { class: "muted" }, "Agent is working — sending paused"),
      h("button", { class: "interrupt", onclick: () => post("/interrupt") }, "Interrupt")));
    return;
  }
  const ta = h("textarea", { placeholder: "Reply to the agent…", rows: "1" });
  ta.value = S.draft;
  ta.addEventListener("input", () => {
    S.draft = ta.value;
    ta.style.height = "auto";
    ta.style.height = Math.min(ta.scrollHeight, 132) + "px";
  });
  ta.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage(ta.value);
    }
  });
  const box = h("div", {});
  box.append(
    h("div", { class: "composer" }, ta,
      h("button", { class: "send", onclick: () => sendMessage(ta.value) }, "Send")),
    h("div", { class: "hint" }, "⏎ send · ⇧⏎ newline"),
  );
  bar.replaceChildren(box);
  ta.focus();
}

/* ---- overlay ---- */

function showOverlay() {
  $("overlay-paths").replaceChildren(
    ...(S.artifacts.length ? S.artifacts : ["(paths printed in the CLI)"])
      .map((p) => h("li", {}, p)));
  $("overlay").hidden = false;
}

/* ================= boot ================= */

function renderAll() {
  renderStatus(); renderPill(); renderDiagram(); renderRail(); renderDock(); renderComposer();
}

document.addEventListener("DOMContentLoaded", () => {
  Diagram.init($("dsvg"), (id) => {
    S.selected = id;
    S.detail = id;
    S.tab = "components";
    renderDock(); renderDiagram();
  });
  $("d-fit").addEventListener("click", () => Diagram.fit());
  new ResizeObserver(() => Diagram.fit()).observe($("dpane"));
  $("d-in").addEventListener("click", () => Diagram.zoomIn());
  $("d-out").addEventListener("click", () => Diagram.zoomOut());
  $("d-crumb").addEventListener("click", () => setDrill(null));
  $("banner-reconnect").addEventListener("click", () => location.reload());
  $("overlay-dismiss").addEventListener("click", () => { $("overlay").hidden = true; });
  renderAll();
  connect();
});
