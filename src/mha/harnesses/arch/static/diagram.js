/* Hand-built SVG diagram for the Workbench hero pane.
   Layered top-down layout over the structured arch state — no Mermaid on the
   page; every node is a live DOM element (click-to-select, NEW pulse, pan/zoom). */
"use strict";

const Diagram = (() => {
  const NS = "http://www.w3.org/2000/svg";
  const NODE_W = 156, NODE_H = 52, GAP_X = 46, GAP_Y = 78, PAD = 30;

  let svg, viewport;                       // <svg> and the pan/zoom <g>
  let scale = 1, tx = 0, ty = 0;
  let contentBox = { w: 0, h: 0 };
  let onSelect = null;

  function el(tag, attrs = {}, ...children) {
    const node = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
    for (const c of children) node.append(c);
    return node;
  }

  function text(content, x, y, cls = "") {
    const t = el("text", { x, y, class: cls });
    t.textContent = content;
    return t;
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
    const g = el("g", {
      class: "node" + (comp.existing ? " existing" : "") +
        (flags.selected ? " selected" : "") + (flags.isNew ? " new" : ""),
      transform: `translate(${p.x},${p.y})`,
      "data-id": comp.id,
    });
    if (flags.isNew) {
      const ring = el("rect", {
        class: "pulse", x: -4, y: -4, width: NODE_W + 8, height: NODE_H + 8,
        rx: 10, style: "transform-origin: center; transform-box: fill-box;",
      });
      g.append(ring);
    }
    g.append(el("rect", { width: NODE_W, height: NODE_H, rx: 8 }));
    const name = comp.name.length > 20 ? comp.name.slice(0, 19) + "…" : comp.name;
    g.append(text(name, NODE_W / 2, 22, "name"));
    g.append(text(comp.kind, NODE_W / 2, 38, "kind"));
    g.querySelectorAll("text").forEach((t) => {
      t.setAttribute("text-anchor", "middle");
      t.setAttribute("font-size", t.classList.contains("kind") ? "10" : "12");
    });
    if (flags.isNew) {
      const badge = el("g", { class: "badge", transform: `translate(${NODE_W - 30},-8)` });
      badge.append(el("rect", { width: 34, height: 15, rx: 7 }));
      badge.append(text("NEW", 17, 11));
      badge.querySelector("text").setAttribute("text-anchor", "middle");
      g.append(badge);
    }
    g.addEventListener("click", (e) => {
      e.stopPropagation();
      if (onSelect) onSelect(comp.id);
    });
    return g;
  }

  function edgeGroup(conn, pos) {
    const a = pos[conn.src], b = pos[conn.dst];
    if (!a || !b) return null;
    let x1, y1, x2, y2;
    if (b.y > a.y) {              // downward: bottom-center -> top-center
      x1 = a.x + NODE_W / 2; y1 = a.y + NODE_H;
      x2 = b.x + NODE_W / 2; y2 = b.y - 5;
    } else if (b.y < a.y) {       // upward: top-center -> bottom-center
      x1 = a.x + NODE_W / 2; y1 = a.y;
      x2 = b.x + NODE_W / 2; y2 = b.y + NODE_H + 5;
    } else {                      // same rank: side to side
      const leftToRight = b.x > a.x;
      x1 = a.x + (leftToRight ? NODE_W : 0); y1 = a.y + NODE_H / 2;
      x2 = b.x + (leftToRight ? -5 : NODE_W + 5); y2 = b.y + NODE_H / 2;
    }
    const g = el("g", { class: `edge ${conn.kind || "sync"}` });
    g.append(el("line", { x1, y1, x2, y2, "marker-end": "url(#arrow)" }));
    let label = conn.label || "";
    if (conn.kind === "async" && conn.mechanism) label += ` · ${conn.mechanism}`;
    if (label) {
      const mx = (x1 + x2) / 2, my = (y1 + y2) / 2 - 3;
      const w = label.length * 6 + 8;
      g.append(el("rect", { class: "lbl-bg", x: mx - w / 2, y: my - 10, width: w, height: 14 }));
      const t = text(label, mx, my + 1);
      t.setAttribute("text-anchor", "middle");
      g.append(t);
    }
    return g;
  }

  function arrowDefs() {
    const marker = el("marker", {
      id: "arrow", viewBox: "0 0 10 10", refX: 9, refY: 5,
      markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse",
    });
    marker.append(el("path", { d: "M 0 0 L 10 5 L 0 10 z", fill: "var(--fg)" }));
    return el("defs", {}, marker);
  }

  /* ---- drill-in views (facet-natural diagrams) ---- */
  function grid(items, itemW, itemH, gapX, gapY, perRow) {
    return items.map((item, i) => ({
      item,
      x: PAD + (i % perRow) * (itemW + gapX),
      y: PAD + Math.floor(i / perRow) * (itemH + gapY),
    }));
  }

  function storeView(facet) {
    const g = el("g");
    const perRow = 3, W = 190;
    let maxH = 0;
    const cells = grid(facet.entities || [], W, 1, 34, 34, perRow);
    for (const { item: ent, x } of cells) {
      const fields = ent.fields || [];
      const h = 26 + Math.max(fields.length, 1) * 16 + 8;
      maxH = Math.max(maxH, h);
    }
    cells.forEach((cell, i) => {
      cell.y = PAD + Math.floor(i / perRow) * (maxH + 34);
    });
    for (const { item: ent, x, y } of cells) {
      const fields = ent.fields || [];
      const h = 26 + Math.max(fields.length, 1) * 16 + 8;
      const eg = el("g", { class: "entity", transform: `translate(${x},${y})` });
      eg.append(el("rect", { class: "box", width: W, height: h, rx: 6 }));
      eg.append(el("rect", { class: "hdr", width: W, height: 24, rx: 6 }));
      eg.append(text(`${ent.name}  (${ent.keys})`, 8, 16, "title"));
      fields.forEach((f, i) => eg.append(text(f, 12, 42 + i * 16)));
      g.append(eg);
    }
    const rows = Math.ceil(cells.length / perRow) || 1;
    contentBox = { w: perRow * (190 + 34) + PAD, h: rows * (maxH + 34) + PAD };
    return g;
  }

  function infraView(facet) {
    const g = el("g");
    let y = PAD, maxW = 0;
    for (const unit of facet.units || []) {
      const members = unit.components || [];
      const w = Math.max(240, members.length * 120 + 30);
      const ug = el("g", { class: "unit", transform: `translate(${PAD},${y})` });
      ug.append(el("rect", { width: w, height: 84, rx: 8 }));
      ug.append(text(`${unit.name} — ${unit.scaling_policy}`, 10, 18, "utitle"));
      members.forEach((cid, i) => {
        const mg = el("g", { class: "node", transform: `translate(${14 + i * 120},32)` });
        mg.append(el("rect", { width: 106, height: 36, rx: 6 }));
        const t = text(cid, 53, 22);
        t.setAttribute("text-anchor", "middle");
        t.setAttribute("font-size", "11");
        mg.append(t);
        ug.append(mg);
      });
      g.append(ug);
      maxW = Math.max(maxW, w);
      y += 104;
    }
    contentBox = { w: maxW + PAD * 2, h: y + PAD };
    return g;
  }

  function serviceView(comp, facet) {
    const g = el("g");
    const mods = facet.modules || [];
    const cells = grid(mods, 180, 46, 26, 26, 3);
    for (const { item: m, x, y } of cells) {
      const mg = el("g", { class: "node", transform: `translate(${x},${y})` });
      mg.append(el("rect", { width: 180, height: 46, rx: 8 }));
      const t1 = text(m.name, 90, 19); t1.setAttribute("text-anchor", "middle");
      const t2 = text(m.purpose, 90, 35, "kind"); t2.setAttribute("text-anchor", "middle");
      t2.setAttribute("font-size", "10"); t1.setAttribute("font-size", "12");
      mg.append(t1, t2);
      g.append(mg);
    }
    const rows = Math.ceil(mods.length / 3) || 1;
    contentBox = { w: 3 * 206 + PAD * 2, h: rows * 72 + PAD * 2 };
    return g;
  }

  /* ---- transform / interactions ---- */
  function applyTransform() {
    viewport.setAttribute("transform", `translate(${tx},${ty}) scale(${scale})`);
  }

  function fit() {
    const box = svg.getBoundingClientRect();
    if (!contentBox.w || !box.width) { scale = 1; tx = ty = 0; applyTransform(); return; }
    scale = Math.min(box.width / contentBox.w, box.height / contentBox.h, 1.4);
    tx = (box.width - contentBox.w * scale) / 2;
    ty = Math.max((box.height - contentBox.h * scale) / 2, 6);
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
    let dragging = false, sx = 0, sy = 0;
    svg.addEventListener("pointerdown", (e) => {
      dragging = true; sx = e.clientX - tx; sy = e.clientY - ty;
      svg.setPointerCapture(e.pointerId);
    });
    svg.addEventListener("pointermove", (e) => {
      if (!dragging) return;
      tx = e.clientX - sx; ty = e.clientY - sy;
      applyTransform();
    });
    svg.addEventListener("pointerup", () => { dragging = false; });
  }

  /* ---- public API ---- */
  function init(svgEl, selectCb) {
    svg = svgEl;
    onSelect = selectCb;
    viewport = el("g");
    svg.append(arrowDefs(), viewport);
    initPan();
  }

  function render(state, opts = {}) {
    // opts: {changedId, selectedId, drillId} — throws on failure; caller keeps last-good
    const comps = state.components || {};
    const next = el("g");
    if (opts.drillId && comps[opts.drillId] && comps[opts.drillId].facet) {
      const comp = comps[opts.drillId];
      const facet = comp.facet;
      let view = null;
      if (facet.facet_kind === "store") view = storeView(facet);
      else if (facet.facet_kind === "infra") view = infraView(facet);
      else if (facet.facet_kind === "service" && facet.modules) view = serviceView(comp, facet);
      if (view) next.append(view);
    } else {
      const pos = positions(comps, state.connections || []);
      for (const conn of state.connections || []) {
        const e = edgeGroup(conn, pos);
        if (e) next.append(e);
      }
      for (const id of Object.keys(comps)) {
        next.append(nodeGroup(comps[id], pos[id], {
          selected: id === opts.selectedId,
          isNew: id === opts.changedId,
        }));
      }
    }
    viewport.replaceChildren(next);
    return Object.keys(comps).length;
  }

  return { init, render, fit, zoomIn: () => zoom(1.2), zoomOut: () => zoom(1 / 1.2) };
})();
