/* Hand-built SVG diagram for the paper canvas.
   Layered top-down layout over the structured arch state — no Mermaid on the
   page. Every node is a live paper card (click-to-open, changed-halo, pan/zoom);
   store/infra/service facets drill into ER / deployment / module views. */
"use strict";

const Diagram = (() => {
  const NS = "http://www.w3.org/2000/svg";
  const NODE_W = 190, NODE_H = 76, GAP_X = 54, GAP_Y = 88, PAD = 40;

  let svg, viewport;                       // <svg> and the pan/zoom <g>
  let scale = 1, tx = 0, ty = 0;
  let contentBox = { w: 0, h: 0 };
  let onSelect = null, onDrill = null;

  function el(tag, attrs = {}, ...children) {
    const node = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
    for (const c of children) node.append(c);
    return node;
  }
  function text(content, x, y, cls = "", anchor = "start") {
    const t = el("text", { x, y, class: cls, "text-anchor": anchor });
    t.textContent = content;
    return t;
  }
  function trunc(s, n) { s = String(s || ""); return s.length > n ? s.slice(0, n - 1) + "…" : s; }

  // What a component's expanded facet contains — surfaced as a badge on the node.
  function facetInfo(facet) {
    if (!facet) return null;
    const k = facet.facet_kind;
    const n = (a) => (facet[a] || []).length;
    const label = {
      api: `${n("endpoints")} endpoints`,
      store: `${n("entities")} entities`,
      queue: `${n("messages")} messages`,
      service: n("modules") ? `${n("modules")} modules` : `${n("interface")} exposed`,
      llm: `${n("tasks")} tasks`,
      infra: `${n("units")} units`,
    }[k] || "expanded";
    const drillable = k === "store" || k === "infra" || (k === "service" && n("modules") > 0);
    return { label, drillable };
  }

  /* ---- layout: longest-path ranks, cycle-tolerant ---- */
  function ranks(ids, connections) {
    const rank = Object.fromEntries(ids.map((id) => [id, 0]));
    const edges = connections.filter((c) => rank[c.src] !== undefined && rank[c.dst] !== undefined);
    for (let i = 0; i < Math.min(ids.length, 12); i++) {
      let moved = false;
      for (const e of edges) {
        if (e.src !== e.dst && rank[e.dst] < rank[e.src] + 1 && rank[e.dst] < ids.length) {
          rank[e.dst] = rank[e.src] + 1;
          moved = true;
        }
      }
      if (!moved) break;
    }
    return rank;
  }

  function positions(components, connections) {
    const ids = Object.keys(components);
    const rk = ranks(ids, connections);
    const rows = new Map();
    for (const id of ids) {
      if (!rows.has(rk[id])) rows.set(rk[id], []);
      rows.get(rk[id]).push(id);
    }
    const ordered = [...rows.keys()].sort((a, b) => a - b);
    const maxRow = Math.max(...ordered.map((r) => rows.get(r).length));
    const fullW = maxRow * NODE_W + (maxRow - 1) * GAP_X;
    const pos = {};
    ordered.forEach((r, ri) => {
      const row = rows.get(r);
      const rowW = row.length * NODE_W + (row.length - 1) * GAP_X;
      row.forEach((id, ci) => {
        pos[id] = {
          x: PAD + (fullW - rowW) / 2 + ci * (NODE_W + GAP_X),
          y: PAD + ri * (NODE_H + GAP_Y),
        };
      });
    });
    contentBox = {
      w: fullW + PAD * 2,
      h: ordered.length * (NODE_H + GAP_Y) - GAP_Y + PAD * 2,
    };
    return pos;
  }

  /* ---- pieces ---- */
  function nodeGroup(comp, p, flags) {
    const cls = "node" + (comp.existing ? " existing" : "") +
      (flags.selected ? " selected" : "") + (flags.changed ? " changed" : "");
    const fi = facetInfo(comp.facet);
    const g = el("g", { class: cls + (fi ? " expanded" : ""), transform: `translate(${p.x},${p.y})`, "data-id": comp.id });
    if (flags.changed) {
      g.append(el("rect", { class: "n-halo", x: -4, y: -4, width: NODE_W + 8, height: NODE_H + 8, rx: 13 }));
      g.append(el("rect", { class: "pulse", x: -4, y: -4, width: NODE_W + 8, height: NODE_H + 8, rx: 13 }));
    }
    if (fi) g.append(el("rect", { class: "n-depth", x: 5, y: 5, width: NODE_W, height: NODE_H, rx: 10 }));
    g.append(el("rect", { class: "n-box", width: NODE_W, height: NODE_H, rx: 10 }));
    g.append(text(comp.kind, 15, 22, "n-kind"));
    g.append(text(trunc(comp.name, fi ? 18 : 22), 15, 43, "n-name"));
    if (comp.responsibility) g.append(text(trunc(comp.responsibility, 30), 15, 61, "n-sub"));
    if (fi) {
      const txt = (fi.drillable ? "⤢ " : "") + fi.label;
      const w = txt.length * 5.6 + 14;
      const pill = el("g", { class: "n-facet" + (fi.drillable ? " drill" : ""),
        transform: `translate(${NODE_W - w - 8},8)` });
      pill.append(el("rect", { width: w, height: 17, rx: 8 }));
      pill.append(text(txt, 8, 12, "n-facet-t"));
      pill.append(el("title", {}, document.createTextNode(
        fi.drillable ? `${fi.label} — click to view internals` : fi.label)));
      if (fi.drillable) pill.addEventListener("click", (e) => { e.stopPropagation(); if (onDrill) onDrill(comp.id); });
      g.append(pill);
    } else if (flags.owes) {
      const dot = el("circle", { class: "n-owe", cx: NODE_W - 13, cy: 13, r: 3.5 });
      dot.append(el("title", {}, document.createTextNode("owes a facet")));
      g.append(dot);
    }
    g.addEventListener("click", (e) => { e.stopPropagation(); if (onSelect) onSelect(comp.id); });
    return g;
  }

  function edgeGroup(conn, pos, changed) {
    const a = pos[conn.src], b = pos[conn.dst];
    if (!a || !b) return null;
    let x1, y1, x2, y2;
    if (b.y > a.y) {              // downward
      x1 = a.x + NODE_W / 2; y1 = a.y + NODE_H;
      x2 = b.x + NODE_W / 2; y2 = b.y - 6;
    } else if (b.y < a.y) {       // upward
      x1 = a.x + NODE_W / 2; y1 = a.y;
      x2 = b.x + NODE_W / 2; y2 = b.y + NODE_H + 6;
    } else {                      // same rank
      const ltr = b.x > a.x;
      x1 = a.x + (ltr ? NODE_W : 0); y1 = a.y + NODE_H / 2;
      x2 = b.x + (ltr ? -6 : NODE_W + 6); y2 = b.y + NODE_H / 2;
    }
    const g = el("g", { class: `edge ${conn.kind || "sync"}${changed ? " changed" : ""}` });
    g.append(el("line", { x1, y1, x2, y2, "marker-end": `url(#${changed ? "arrow-hl" : "arrow"})` }));
    let label = conn.label || "";
    if (conn.kind === "async" && conn.mechanism) label += ` · ${conn.mechanism}`;
    if (label) {
      const mx = (x1 + x2) / 2, my = (y1 + y2) / 2 - 3;
      const w = label.length * 6 + 10;
      g.append(el("rect", { class: "lbl-bg", x: mx - w / 2, y: my - 10, width: w, height: 15, rx: 3 }));
      g.append(text(label, mx, my + 1, "lbl", "middle"));
    }
    return g;
  }

  function marker(id, color) {
    const m = el("marker", { id, viewBox: "0 0 8 8", refX: 6, refY: 3,
      markerWidth: 8, markerHeight: 8, orient: "auto" });
    m.append(el("path", { d: "M0,0 L6,3 L0,6 Z", fill: color }));
    return m;
  }
  function defs() {
    return el("defs", {},
      marker("arrow", "var(--edge)"),
      marker("arrow-hl", "var(--hl)"));
  }

  /* ---- drill-in views (facet-natural diagrams) ---- */
  function grid(items, itemW, gapX, perRow) {
    return items.map((item, i) => ({ item, col: i % perRow, row: Math.floor(i / perRow), x: PAD + (i % perRow) * (itemW + gapX) }));
  }

  function storeView(facet) {
    const g = el("g");
    const perRow = 3, W = 200;
    const ents = facet.entities || [];
    const cells = grid(ents, W, 40, perRow);
    let maxH = 0;
    for (const ent of ents) maxH = Math.max(maxH, 28 + Math.max((ent.fields || []).length, 1) * 17 + 8);
    cells.forEach((c) => { c.y = PAD + c.row * (maxH + 40); });
    for (const { item: ent, x, y } of cells) {
      const fields = ent.fields || [];
      const h = 28 + Math.max(fields.length, 1) * 17 + 8;
      const eg = el("g", { class: "entity", transform: `translate(${x},${y})` });
      eg.append(el("rect", { class: "e-box", width: W, height: h, rx: 8 }));
      eg.append(el("rect", { class: "e-hdr", width: W, height: 26, rx: 8 }));
      eg.append(text(`${ent.name}  ·  ${ent.keys}`, 10, 18, "e-title"));
      fields.forEach((f, i) => eg.append(text(trunc(f, 26), 12, 46 + i * 17, "e-field")));
      g.append(eg);
    }
    const rows = Math.ceil(cells.length / perRow) || 1;
    contentBox = { w: perRow * (W + 40) + PAD, h: rows * (maxH + 40) + PAD };
    return g;
  }

  function infraView(facet) {
    const g = el("g");
    let y = PAD, maxW = 0;
    for (const unit of facet.units || []) {
      const members = unit.components || [];
      const w = Math.max(260, members.length * 128 + 32);
      const ug = el("g", { class: "unit", transform: `translate(${PAD},${y})` });
      ug.append(el("rect", { class: "u-box", width: w, height: 92, rx: 10 }));
      ug.append(text(`${unit.name}  —  ${unit.scaling_policy || "scaling n/a"}`, 12, 20, "u-title"));
      members.forEach((cid, i) => {
        const mg = el("g", { transform: `translate(${16 + i * 128},34)` });
        mg.append(el("rect", { class: "m-box", width: 112, height: 40, rx: 7 }));
        mg.append(text(trunc(cid, 15), 56, 24, "m-name", "middle"));
        ug.append(mg);
      });
      g.append(ug);
      maxW = Math.max(maxW, w);
      y += 112;
    }
    contentBox = { w: maxW + PAD * 2, h: y + PAD };
    return g;
  }

  function serviceView(facet) {
    const g = el("g");
    const mods = facet.modules || [];
    const cells = grid(mods, 190, 30, 3);
    cells.forEach((c) => { c.y = PAD + c.row * 76; });
    for (const { item: m, x, y } of cells) {
      const mg = el("g", { class: "mod", transform: `translate(${x},${y})` });
      mg.append(el("rect", { class: "m-box", width: 190, height: 54, rx: 9 }));
      mg.append(text(trunc(m.name, 24), 14, 24, "m-name"));
      mg.append(text(trunc(m.purpose, 30), 14, 42, "m-purpose"));
      g.append(mg);
    }
    const rows = Math.ceil(mods.length / 3) || 1;
    contentBox = { w: 3 * 220 + PAD * 2, h: rows * 76 + PAD * 2 };
    return g;
  }

  /* ---- transform / interactions ---- */
  function applyTransform() {
    viewport.setAttribute("transform", `translate(${tx},${ty}) scale(${scale})`);
  }
  function fit() {
    const box = svg.getBoundingClientRect();
    if (!contentBox.w || !box.width) { scale = 1; tx = ty = 0; applyTransform(); return; }
    const topInset = 52, pad = 20;   // leave room for the canvas toolbar; never clip
    const availW = box.width - pad * 2, availH = box.height - topInset - pad;
    scale = Math.min(availW / contentBox.w, availH / contentBox.h, 1.35);
    tx = (box.width - contentBox.w * scale) / 2;
    ty = topInset + Math.max((availH - contentBox.h * scale) / 2, 0);
    applyTransform();
  }
  function zoom(factor) {
    const box = svg.getBoundingClientRect();
    const cx = box.width / 2, cy = box.height / 2;
    const next = Math.min(Math.max(scale * factor, 0.25), 3);
    tx = cx - ((cx - tx) / scale) * next;
    ty = cy - ((cy - ty) / scale) * next;
    scale = next;
    applyTransform();
  }
  function initPan() {
    // Capture the pointer only once a real drag starts — otherwise a captured
    // pointer redirects the click away from the node and selection never fires.
    let dragging = false, moved = false, downX = 0, downY = 0, sx = 0, sy = 0;
    svg.addEventListener("pointerdown", (e) => {
      dragging = true; moved = false; downX = e.clientX; downY = e.clientY;
      sx = e.clientX - tx; sy = e.clientY - ty;
    });
    svg.addEventListener("pointermove", (e) => {
      if (!dragging) return;
      if (!moved) {
        if (Math.abs(e.clientX - downX) + Math.abs(e.clientY - downY) < 4) return;
        moved = true;                                     // real drag → pan
        try { svg.setPointerCapture(e.pointerId); } catch { /* ignore */ }
      }
      tx = e.clientX - sx; ty = e.clientY - sy;
      applyTransform();
    });
    const stop = () => { dragging = false; moved = false; };
    svg.addEventListener("pointerup", stop);
    svg.addEventListener("pointercancel", stop);
    svg.addEventListener("wheel", (e) => { e.preventDefault(); zoom(e.deltaY < 0 ? 1.1 : 1 / 1.1); }, { passive: false });
  }

  /* ---- public API ---- */
  function init(svgEl, selectCb, drillCb) {
    svg = svgEl;
    onSelect = selectCb;
    onDrill = drillCb;
    viewport = el("g");
    svg.append(defs(), viewport);
    initPan();
  }

  function edgeChanged(conn, changed) {
    if (!changed) return false;
    if (changed.kind === "component") return conn.src === changed.id || conn.dst === changed.id;
    if (changed.kind === "connect") {
      const cid = String(changed.id || "").toLowerCase();
      return cid.includes(String(conn.src).toLowerCase()) && cid.includes(String(conn.dst).toLowerCase());
    }
    return false;
  }

  function render(state, opts = {}) {
    // opts: {changed:{kind,id}, selectedId, drillId, oweSet} — throws on failure; caller keeps last good.
    const comps = state.components || {};
    const next = el("g");
    const drill = opts.drillId && comps[opts.drillId] && comps[opts.drillId].facet ? comps[opts.drillId] : null;
    if (drill) {
      const f = drill.facet;
      let view = null;
      if (f.facet_kind === "store") view = storeView(f);
      else if (f.facet_kind === "infra") view = infraView(f);
      else if (f.facet_kind === "service" && f.modules) view = serviceView(f);
      if (view) next.append(view);
    } else if (Object.keys(comps).length) {
      const pos = positions(comps, state.connections || []);
      for (const conn of state.connections || []) {
        const e = edgeGroup(conn, pos, edgeChanged(conn, opts.changed));
        if (e) next.append(e);
      }
      const owe = opts.oweSet || new Set();
      const changedId = opts.changed && opts.changed.kind === "component" ? opts.changed.id : null;
      for (const id of Object.keys(comps)) {
        next.append(nodeGroup(comps[id], pos[id], {
          selected: id === opts.selectedId,
          changed: id === changedId,
          owes: owe.has(id),
        }));
      }
    }
    viewport.replaceChildren(next);
    return Object.keys(comps).length;
  }

  return { init, render, fit, zoomIn: () => zoom(1.2), zoomOut: () => zoom(1 / 1.2) };
})();
