/* mha arch — "Paper direction" client over the arch harness protocol.
   One SSE connection in; POST /input, /permission, /interrupt out. The server
   replays late joiners (ready + latest arch_state + buffered transcript), so a
   refresh rebuilds everything. The event contract is unchanged from A3 — only
   the surfaces are new: header · hero canvas · right drawer · component card ·
   finalize modal · bottom chat. */
"use strict";

const S = {
  conn: "connecting",          // connecting | connected | disconnected | complete
  ready: null,
  arch: null,                  // latest arch_state event
  changed: null,               // {kind, id} from the latest arch_state
  transcript: [],              // {t:"user"|"agent"|"notice"|"turn", ...}
  stream: null,                // in-flight agent text
  running: false,
  turnError: null,             // message for the error banner
  lastUserText: "",
  permission: null,            // pending toplevel_approval / finalize request
  artifacts: [],
  finalized: false,
  draft: "",

  drawerOpen: false,
  drawerSection: "decisions",  // decisions | questions | flows
  decisionId: null,            // options detail open in the drawer

  selected: null,              // component scoped into the chat (also highlighted in the diagram)
  drill: null,                 // component whose facet fills the hero
  drillChanged: false,
  lastCount: -1,
  chatH: 300,                  // resizable bottom-chat height (px)
};

const $ = (id) => document.getElementById(id);

function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "html") node.innerHTML = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v === true) node.setAttribute(k, "");
    else if (v !== false && v != null) node.setAttribute(k, v);
  }
  for (const c of children) if (c != null && c !== false) node.append(c);
  return node;
}

function state() { return (S.arch && S.arch.state) || null; }

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
    arg = a.id || a.component_id || (a.src && `${a.src} → ${a.dst}`) || a.topic ||
      a.path || a.question || a.query || a.url || a.name || "";
  } catch { /* leave arg empty */ }
  const glyph = NOTICE_GLYPHS[call.name] || "·";
  return `${glyph} ${call.name}${arg ? " " + String(arg).slice(0, 72) : ""}`;
}

function onHarnessEvent(ev) {
  const { event, data } = ev;
  if (event === "run_start") {
    const task = data.task || "";
    if (task) { S.transcript.push({ t: "user", text: task }); S.lastUserText = task; }
    S.running = true;
    S.stream = "";
    S.turnError = null;
    renderChat(); paint(); renderHeader();
  } else if (event === "assistant_delta") {
    if (S.stream === null) S.stream = "";
    S.stream += data.text || "";
    updateStream();
  } else if (event === "assistant") {
    if (data.content) S.transcript.push({ t: "agent", text: data.content });
    S.stream = S.running ? "" : null;
    for (const call of data.tool_calls || []) S.transcript.push({ t: "notice", text: noticeFor(call) });
    renderChat();
  } else if (event === "tool_result" && data.is_error) {
    S.transcript.push({ t: "notice", err: true, text: `✗ ${data.name} — see the agent's next step` });
    renderChat();
  } else if (event === "abort") {
    S.transcript.push({ t: "notice", err: true, text: `✗ aborted: ${data.reason || ""}` });
    renderChat();
  }
}

function onEvent(ev) {
  switch (ev.type) {
    case "ready":
      S.ready = ev;
      if (S.conn !== "complete") S.conn = "connected";
      paint(); renderHeader(); renderChat();
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
      }
      renderAll();
      break;
    case "permission_request":
      if (ev.kind === "toplevel_approval" || ev.kind === "finalize") {
        S.permission = ev;
        if (ev.kind === "finalize") S.artifacts = ev.artifacts || [];
        renderChat();
      }
      break;
    case "turn_end":
      S.running = false;
      if (S.stream) S.transcript.push({ t: "agent", text: S.stream });
      S.stream = null;
      S.transcript.push({
        t: "turn", status: ev.status,
        message: ev.status === "error" ? (S.turnError || "the turn failed") : null,
      });
      paint(); renderChat(); renderHeader();
      break;
    case "error":
      S.turnError = ev.message || "the turn failed";
      S.transcript.push({ t: "notice", err: true, text: `✗ ${ev.message || "error"}` });
      renderChat();
      break;
    case "bye":
      if (!S.finalized) disconnect();
      break;
  }
}

function connect() {
  const es = new EventSource("/events");
  es.onmessage = (e) => onEvent(JSON.parse(e.data));
  es.onerror = () => { es.close(); if (!S.finalized) disconnect(); };
}

function disconnect() {
  S.conn = "disconnected";
  S.running = false;
  paint(); renderChat(); renderHeader();
}

/* ================= actions ================= */

async function post(path, body) {
  try { await fetch(path, { method: "POST", body: JSON.stringify(body || {}) }); }
  catch { /* surfaced via connection state */ }
}

function sendMessage(text) {
  if (!text.trim()) return;
  if (S.permission) { respond(false, text.trim()); return; }  // reply during a gate = refine (request changes)
  let out = text.trim();
  const st = state();                       // scope the message to the selected component, if any
  if (S.selected && st && st.components[S.selected]) {
    const c = st.components[S.selected];
    out = `About ${c.name} (${c.id}): ${out}`;
  }
  S.draft = "";
  S.lastUserText = out;
  post("/input", { text: out });
}

function respond(approved, feedback) {
  const req = S.permission;
  if (!req) return;
  S.permission = null;
  S.running = true;
  S.draft = "";
  post("/permission", { id: req.id, approved, feedback: feedback || "" });
  paint(); renderChat();
}

/* ================= header ================= */

function sessionTitle() {
  const st = state();
  if (st && st.brief && st.brief.goal) return st.brief.goal;
  const u = S.transcript.find((x) => x.t === "user");
  if (u && u.text) return u.text;
  return "Architecture session";
}

function renderHeader() {
  $("brand").textContent = sessionTitle();
  const r = S.ready || {};
  $("m-model").textContent = `architect · ${r.model || "—"}`;
  const repo = r.repo || "";
  $("m-repo").textContent = repo ? repo.split("/").slice(-2).join("/") : "—";
  $("m-session").textContent = r.run_id ? `sess ${r.run_id}` : "—";
  const labels = { connecting: "Connecting…", connected: "Live", disconnected: "Disconnected", complete: "Complete" };
  $("conn-label").textContent = labels[S.conn];
}

function paint() {
  const app = $("app");
  app.dataset.conn = S.conn;
  app.dataset.run = S.running ? "running" : "idle";
  app.dataset.drawer = S.drawerOpen ? "open" : "closed";
  $("conn-lost").hidden = S.conn !== "disconnected";
  $("ro-badge").hidden = !S.finalized;
}

/* ================= diagram ================= */

function oweSet() {
  const st = state(), s = new Set();
  if (st) for (const o of st.obligations || []) if (o.status === "pending") s.add(o.component_id);
  return s;
}

function renderDiagram() {
  const st = state();
  const count = st ? Object.keys(st.components).length : 0;
  const drilling = !!(S.drill && st && st.components[S.drill] && st.components[S.drill].facet);
  $("empty").hidden = count > 0 || drilling;
  if (!st) { $("crumb").hidden = true; return; }
  try {
    // Always render (an empty state clears the viewport — no stale nodes left behind).
    Diagram.render(st, { changed: S.changed, selectedId: S.selected, drillId: S.drill, oweSet: oweSet() });
    $("render-error").hidden = true;
    if ((count > 0 || drilling) && (count !== S.lastCount || S.drillChanged)) {
      Diagram.fit(); S.lastCount = count; S.drillChanged = false;
    }
  } catch (err) {
    $("render-error").hidden = false;
    $("render-error-msg").textContent = String(err);
  }
  if (drilling) {
    $("crumb").hidden = false;
    $("crumb-name").textContent = st.components[S.drill].name;
  } else {
    $("crumb").hidden = true;
  }
}

function setDrill(id) {
  S.drill = id;
  S.drillChanged = true;
  renderAll();
}

/* ================= right drawer ================= */

function qStatus(q) {
  if (q.resolution === "answered" || q.answer) return "answered";
  if (q.blocking) return "blocking";
  return "open";
}

function drawerCounts() {
  const st = state();
  return {
    decisions: st ? st.decisions.length : 0,
    questions: st ? st.questions.length : 0,
    flows: st ? st.flows.length : 0,
  };
}

function renderDrawerTab() {
  const st = state();
  $("drawer-tab-count").textContent = String(st ? st.decisions.length : 0);
  const blocking = st && st.questions.some((q) => qStatus(q) === "blocking" && !q.resolution);
  $("drawer-tab-flag").hidden = !blocking;
}

function openDrawer(section) {
  S.drawerOpen = true;
  if (section) S.drawerSection = section;
  S.decisionId = null;
  renderAll();
}
function closeDrawer() { S.drawerOpen = false; paint(); }

function drawerDecisions() {
  const st = state();
  if (!st || !st.decisions.length) return h("div", { class: "drawer-empty" }, "No decisions recorded yet.");
  const wrap = h("div", { class: "drawer-body-inner" });
  const box = h("div", { style: "display:flex;flex-direction:column;gap:12px" });
  for (const d of st.decisions) {
    box.append(h("div", { class: "pcard click", onclick: () => { S.decisionId = d.id; renderDrawer(); } },
      h("div", { class: "plabel" }, d.category),
      h("div", { class: "ptitle" }, `${d.topic} → ${d.choice}`),
      d.status === "deferred" ? h("span", { class: "tag" }, "deferred") : null,
      d.rationale ? h("div", { class: "psub" }, d.rationale) : null));
  }
  wrap.append(box);
  return wrap;
}

function drawerDecisionDetail() {
  const st = state();
  const d = st && st.decisions.find((x) => x.id === S.decisionId);
  if (!d) { S.decisionId = null; return drawerDecisions(); }
  const wrap = h("div", { style: "display:flex;flex-direction:column;gap:12px" });
  wrap.append(h("button", { class: "detail-back", onclick: () => { S.decisionId = null; renderDrawer(); } },
    h("span", {}, "←"), h("span", {}, "Decisions")));
  wrap.append(h("div", { class: "plabel" }, d.category));
  wrap.append(h("div", { style: "font-family:var(--serif);font-size:16px;font-weight:600;color:var(--ink)" }, d.topic));
  const row = h("div", { class: "opt-row" });
  for (const o of d.options || []) {
    const chosen = o.name === d.choice;
    const opt = h("div", { class: "opt" + (chosen ? " chosen" : " dim") });
    opt.append(h("div", { class: "opt-name" },
      h("span", {}, o.name),
      chosen ? h("span", { class: "opt-chosen" }, "chosen") : null));
    for (const p of o.pros || []) opt.append(h("div", { class: "pro" }, `＋ ${p}`));
    for (const c of o.cons || []) opt.append(h("div", { class: "con" }, `− ${c}`));
    row.append(opt);
  }
  if ((d.options || []).length) wrap.append(row);
  wrap.append(h("div", { class: "plabel", style: "margin-top:2px" }, "Rationale"));
  wrap.append(h("div", { style: "font-size:12.5px;color:var(--ink);line-height:1.55" }, d.rationale || "—"));
  return wrap;
}

function drawerQuestions() {
  const st = state();
  if (!st || !st.questions.length) return h("div", { class: "drawer-empty" }, "No open questions.");
  const box = h("div", { style: "display:flex;flex-direction:column;gap:12px" });
  const LABELS = { blocking: "blocking · unanswered", answered: "answered", open: "non-blocking" };
  for (const q of st.questions) {
    const s = qStatus(q);
    const card = h("div", { class: "pcard" + (s === "answered" ? " answered" : "") });
    card.append(h("div", { class: `qdot ${s}` }, LABELS[s]));
    card.append(h("div", { class: "ptitle", style: "font-weight:500" }, q.question));
    if (s === "blocking") card.append(h("div", { class: "pfoot" }, "Gates finalize until answered."));
    else if (q.answer) card.append(h("div", { class: "psub" }, q.answer));
    box.append(card);
  }
  return box;
}

function drawerFlows() {
  const st = state();
  if (!st || !st.flows.length) return h("div", { class: "drawer-empty" }, "No flows recorded yet.");
  const box = h("div", { style: "display:flex;flex-direction:column;gap:16px" });
  for (const f of st.flows) {
    const flow = h("div", { class: "flow" });
    flow.append(h("div", { class: "fhead" },
      h("span", { class: "fname" }, f.name),
      h("span", { class: "tag" }, f.kind)));
    const ol = h("ol", { class: "fsteps" });
    (f.steps || []).forEach((s, i) => {
      ol.append(h("li", {},
        h("span", { class: "faint" }, `${i + 1}. `),
        h("code", {}, `${s.src} → ${s.dst}`), ` — ${s.action}`,
        s.note ? h("span", { class: "faint" }, `  (${s.note})`) : null));
    });
    flow.append(ol);
    box.append(flow);
  }
  return box;
}

function renderDrawer() {
  const d = $("drawer");
  const counts = drawerCounts();
  const titles = { decisions: "Decisions", questions: "Open Questions", flows: "Flows" };
  const head = h("div", { class: "drawer-head" },
    h("div", { class: "dh-title" }, S.decisionId ? "Decision" : titles[S.drawerSection]),
    h("button", { class: "dh-close", onclick: closeDrawer }, "✕"));
  const seg = h("div", { class: "seg" },
    ...[["decisions", "Decisions"], ["questions", "Questions"], ["flows", "Flows"]].map(([k, label]) =>
      h("button", {
        class: (S.drawerSection === k && !S.decisionId) ? "active" : "",
        onclick: () => { S.drawerSection = k; S.decisionId = null; renderDrawer(); },
      }, label, h("span", { class: "n" }, String(counts[k])))));
  let body;
  if (S.drawerSection === "decisions") body = S.decisionId ? drawerDecisionDetail() : drawerDecisions();
  else if (S.drawerSection === "questions") body = drawerQuestions();
  else body = drawerFlows();
  d.replaceChildren(head, seg, h("div", { class: "drawer-body" }, body));
}

/* ================= component selection (scopes the chat) ================= */

function focusComposer() {
  const ta = document.querySelector('#composer textarea:not([disabled])');
  if (!ta) return;
  ta.focus();
  const n = ta.value.length;
  try { ta.setSelectionRange(n, n); } catch { /* ignore */ }
  ta.dispatchEvent(new Event("input"));
}

function canSendNow() {
  return S.conn === "connected" && !S.running && !S.permission && !S.finalized;
}

function isDrillable(c) {
  const f = c && c.facet;
  return !!f && (f.facet_kind === "store" || f.facet_kind === "infra" ||
    (f.facet_kind === "service" && (f.modules || []).length));
}

function selectComp(id) {
  // Click a node to scope the chat to it; click the same node again to clear.
  S.selected = (S.selected === id) ? null : id;
  renderDiagram();
  renderComposer();
  if (S.selected) focusComposer();
}

function clearSelection() {
  S.selected = null;
  renderDiagram();
  renderComposer();
}

function expandComp(c) {
  // A clear, complete instruction — sent straight to the agent (not scope-prefixed).
  const msg = `Please expand the "${c.name}" (${c.id}) component and detail its ${c.kind} internals.`;
  if (canSendNow()) { S.lastUserText = msg; post("/input", { text: msg }); }
  else { S.draft = msg; renderComposer(); focusComposer(); }
}

function scopeChip(c) {
  const chip = h("div", { class: "scope-chip" });
  chip.append(h("span", { class: "sc-label" }, "Asking about"));
  chip.append(h("span", { class: "sc-name" }, h("span", { class: "sc-kind" }, c.kind), c.name));
  if (isDrillable(c)) chip.append(h("button", { class: "sc-act", onclick: () => setDrill(c.id) }, "View internals"));
  if (!c.facet) chip.append(h("button", { class: "sc-act", onclick: () => expandComp(c) }, "Expand"));
  chip.append(h("button", { class: "sc-clear", title: "Clear selection", onclick: clearSelection }, "✕"));
  return chip;
}

/* ================= approval / finalize gate (non-blocking banner) ================= */
/* Renders above the composer, so the diagram stays fully visible. The agent's turn
   is still open — replying refines (routes as feedback); the button confirms. */

function gateBar() {
  const isFinal = S.permission.kind === "finalize";
  const bar = h("div", { class: "gate" });
  bar.append(h("div", { class: "gate-text" },
    h("b", {}, isFinal ? "Ready to finalize" : "Top-level design ready for approval"),
    h("span", { class: "gate-sub" }, isFinal
      ? " — writes the handoff bundle and ends the session."
      : " — approve to move on to expanding components, or reply below to refine.")));
  bar.append(h("button", { class: "gate-btn" + (isFinal ? " finalize" : ""), onclick: () => respond(true) },
    isFinal ? "Finalize" : "Approve"));
  return bar;
}

/* ================= bottom chat ================= */

function logNode(item) {
  if (item.t === "user") {
    return h("div", { class: "turn you" },
      h("div", { class: "avatar you" }, "you"),
      h("div", { class: "bubble" }, h("div", { class: "msg" }, item.text)));
  }
  if (item.t === "agent") {
    return h("div", { class: "turn" },
      h("div", { class: "avatar" }, "A"),
      h("div", { class: "bubble" }, h("div", { class: "msg" }, item.text)));
  }
  if (item.t === "notice") {
    return h("div", { class: "notice" + (item.err ? " err" : "") }, item.text);
  }
  // turn boundary
  if (item.status === "error") {
    return h("div", { class: "errbanner" },
      h("span", {}, `Turn failed — ${item.message}. `),
      h("span", { class: "retry", onclick: () => S.lastUserText && post("/input", { text: S.lastUserText }) }, "Retry"));
  }
  if (item.status === "interrupted") {
    return h("div", { class: "divider" }, h("span", {}, "turn interrupted"));
  }
  return null; // completed turns need no marker
}

function renderLog() {
  const log = $("log");
  const nodes = S.transcript.map(logNode).filter(Boolean);
  if (S.stream !== null) {
    const msg = h("div", { class: "msg" }, S.stream);
    msg.append(h("span", { class: "caret" }, "▍"));
    nodes.push(h("div", { class: "turn", id: "streaming" },
      h("div", { class: "avatar" }, "A"),
      h("div", { class: "bubble" }, msg)));
  }
  log.replaceChildren(...nodes);
  log.scrollTop = log.scrollHeight;
}

function updateStream() {
  const node = $("streaming");
  if (!node) return renderLog();
  const msg = node.querySelector(".msg");
  msg.firstChild.textContent = S.stream;
  const log = $("log");
  log.scrollTop = log.scrollHeight;
}

function composerField(placeholder) {
  const ta = h("textarea", { placeholder, rows: "1" });
  ta.value = S.draft;
  ta.addEventListener("input", () => {
    S.draft = ta.value;
    ta.style.height = "auto";
    ta.style.height = Math.min(ta.scrollHeight, 132) + "px";
  });
  ta.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendMessage(ta.value); }
  });
  return ta;
}

function renderComposer() {
  const bar = $("composer");

  if (S.finalized) {
    const paths = S.artifacts.length ? S.artifacts : null;
    const body = h("div", { class: "c-body" });
    if (paths) {
      body.append("Handoff bundle written to ");
      paths.forEach((p, i) => {
        body.append(h("code", {}, p));
        if (i < paths.length - 2) body.append(", ");
        else if (i === paths.length - 2) body.append(" and ");
      });
      body.append(". Next: run ", h("code", {}, "mha code"), " to start building against this design.");
    } else {
      body.append("The handoff bundle was written (paths printed in the CLI). Next: run ", h("code", {}, "mha code"), ".");
    }
    bar.replaceChildren(h("div", { class: "complete" },
      h("div", { class: "c-title" }, "Session complete"), body));
    return;
  }

  const inner = h("div", { class: "composer-inner" });

  if (S.permission) {
    const isFinal = S.permission.kind === "finalize";
    inner.append(gateBar());
    const ta = composerField(isFinal ? "Reply to refine — or Finalize above…" : "Reply to refine the design — or Approve above…");
    inner.append(
      h("div", { class: "field" }, ta, h("button", { class: "send", onclick: () => sendMessage(ta.value) }, "Send")),
      h("div", { class: "hint" }, isFinal ? "⏎ reply to refine · Finalize ends the session" : "⏎ reply to refine · Approve to continue"));
    bar.replaceChildren(inner);
    ta.focus();
    return;
  }
  if (S.conn === "connecting") {
    inner.append(h("div", { class: "locknote" }, h("span", { class: "spinner" }), "Connecting to the session…"));
    bar.replaceChildren(inner);
    return;
  }
  if (S.conn === "disconnected") {
    inner.append(h("div", { class: "locknote" }, "Disconnected — ",
      h("span", { class: "retry", style: "text-decoration:underline;cursor:pointer;color:var(--accent)", onclick: () => location.reload() }, "reconnect")));
    bar.replaceChildren(inner);
    return;
  }
  if (S.running) {
    const field = h("div", { class: "field locked" },
      h("textarea", { rows: "1", disabled: true, placeholder: "Sending disabled while the agent works…" }),
      h("button", { class: "interrupt", onclick: () => post("/interrupt") }, "Interrupt"));
    inner.append(field);
    bar.replaceChildren(inner);
    return;
  }

  const st = state();
  const selC = S.selected && st && st.components[S.selected];
  if (selC) inner.append(scopeChip(selC));
  const ta = composerField(selC ? `Ask about ${selC.name}…` : "Message the agent…");
  inner.append(
    h("div", { class: "field" }, ta, h("button", { class: "send", onclick: () => sendMessage(ta.value) }, "Send")),
    h("div", { class: "hint" }, selC ? "⏎ send · scoped to this component" : "⏎ send · ⇧⏎ newline"));
  bar.replaceChildren(inner);
  ta.focus();
}

function renderChat() { renderLog(); renderComposer(); }

/* ---- resizable chat panel (drag the top edge, VSCode-terminal style) ---- */

function clampChatH(px) {
  const min = 108;                                   // handle + composer
  const max = Math.max(min, window.innerHeight - 62 - 140); // keep header + a usable stage
  return Math.round(Math.max(min, Math.min(px, max)));
}

function applyChatH() {
  $("chat").style.height = S.chatH + "px";
  try { Diagram.fit(); } catch { /* keep last-good */ }
}

function initChatResize() {
  const stored = parseInt(localStorage.getItem("mha_arch_chat_h") || "", 10);
  S.chatH = clampChatH(Number.isFinite(stored) ? stored : Math.round(window.innerHeight * 0.32));
  applyChatH();

  const handle = $("chat-resize");
  let dragging = false, startY = 0, startH = 0;
  handle.addEventListener("pointerdown", (e) => {
    dragging = true; startY = e.clientY; startH = S.chatH;
    try { handle.setPointerCapture(e.pointerId); } catch { /* non-capturable pointer */ }
    $("app").classList.add("resizing");
    e.preventDefault();
  });
  handle.addEventListener("pointermove", (e) => {
    if (!dragging) return;
    S.chatH = clampChatH(startH - (e.clientY - startY));  // drag up → taller
    $("chat").style.height = S.chatH + "px";              // fit() rides the ResizeObserver
  });
  const end = () => {
    if (!dragging) return;
    dragging = false;
    $("app").classList.remove("resizing");
    try { localStorage.setItem("mha_arch_chat_h", String(S.chatH)); } catch { /* private mode */ }
    try { Diagram.fit(); } catch { /* keep last-good */ }
  };
  handle.addEventListener("pointerup", end);
  handle.addEventListener("pointercancel", end);
  window.addEventListener("resize", () => { S.chatH = clampChatH(S.chatH); applyChatH(); });
}

/* ================= boot ================= */

function renderAll() {
  paint();
  renderHeader();
  renderDiagram();
  renderDrawerTab();
  if (S.drawerOpen) renderDrawer();
  renderChat();
}

document.addEventListener("DOMContentLoaded", () => {
  Diagram.init($("dsvg"), (id) => selectComp(id), (id) => setDrill(id));
  $("z-fit").addEventListener("click", () => Diagram.fit());
  $("z-in").addEventListener("click", () => Diagram.zoomIn());
  $("z-out").addEventListener("click", () => Diagram.zoomOut());
  $("crumb").addEventListener("click", () => setDrill(null));
  $("drawer-tab").addEventListener("click", () => openDrawer());
  new ResizeObserver(() => Diagram.fit()).observe($("canvas"));
  initChatResize();
  renderAll();
  connect();
});
