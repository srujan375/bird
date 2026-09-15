// editor-bridge.js — injected by dom.py's wrap_editor as the last child of <body> in every artboard iframe.
// The iframe is a view; dom.py is the document. Selectors are element-index paths ("0>1>2", first index under
// <html>) matching dom.py's _selector_path; the bridge adds no nodes/attributes (the outline rides in an
// adopted stylesheet) so paths resolve identically server-side. All replies post to parent, "*". No deps.
(function () {
  "use strict";
  const UNSELECTABLE = { html: 1, head: 1, meta: 1, title: 1, style: 1, script: 1, link: 1, base: 1 };
  const OPS = ["set_style", "set_text", "insert", "delete", "duplicate", "move"];
  // what the inspector offers as editable fields; real property names, so each
  // one is a set_style op the harness applies without translation
  const FORM_PROPS = ["display", "color", "background-color", "font-size", "font-weight",
                      "padding", "margin", "border-radius", "text-align"];
  const elKids = (n) => Array.from(n.children);
  const post = (msg) => parent.postMessage(msg, "*");
  // The showcase view's flag: the page tells this frame it is the full-bleed
  // result, so the board gestures stand down — a full-fledged page scrolls
  // like one. Click-to-select stays: pointing still matters mid-polish.
  let showcase = false;

  function resolve(sel) {
    if (sel == null || !String(sel).trim()) throw new Error("no selector was sent");
    let cur = null; // null = the virtual root; its only element child is <html>
    for (const part of String(sel).trim().split(">")) {
      const idx = Number(part), kids = cur === null ? [document.documentElement] : elKids(cur);
      if (part === "" || !Number.isInteger(idx)) throw new Error("bad selector " + sel + ": " + JSON.stringify(part) + " is not an index");
      if (idx < 0 || idx >= kids.length) throw new Error("selector " + sel + " no longer resolves — the document changed under it");
      cur = kids[idx];
    }
    const tag = cur.tagName.toLowerCase(); if (UNSELECTABLE[tag]) throw new Error(tag + " is not editable");
    return cur;
  }

  // resolve() inverted: the element-index path of a live element, for click-to-select.
  function pathOf(el) {
    const parts = [];
    for (let cur = el; cur && cur !== document.documentElement; cur = cur.parentElement) {
      parts.push(String(elKids(cur.parentElement).indexOf(cur)));
    }
    parts.push("0"); // <html>: the only element child of the virtual root
    return parts.reverse().join(">");
  }

  // Outline by position, never by mutation: an adopted stylesheet + an nth-child chain selects the element without touching the DOM it points at.
  let sheet = null;
  function outline(sel) {
    if (!window.CSSStyleSheet || !document.adoptedStyleSheets) return; // old engine: edits still work
    if (!sheet) { sheet = new CSSStyleSheet(); document.adoptedStyleSheets = [sheet]; }
    let css = ":root"; const parts = sel.split(">");
    for (let i = 1; i < parts.length; i++) css += " > *:nth-child(" + (Number(parts[i]) + 1) + ")";
    sheet.replaceSync(css + "{outline:2px solid #6c8cff;outline-offset:1px}");
  }

  function selected(sel, el) {
    const r = el.getBoundingClientRect(), s = getComputedStyle(el), props = {};
    for (const p of FORM_PROPS) props[p] = s.getPropertyValue(p);
    // `leaf` is set_text's precondition, decided here rather than guessed at by
    // the panel: the same rule dom.py refuses on.
    post({ type: "selected", selector: sel, tag: el.tagName.toLowerCase(),
      leaf: !el.children.length, text: el.children.length ? "" : el.textContent,
      box: { x: r.x, y: r.y, width: r.width, height: r.height }, props: props });
  }

  // Click to select. A click in an artboard is the user pointing at something,
  // never navigation: an <a href> or a form in a mockup must not walk the frame
  // off the document the harness is holding. Capture phase, so the artboard's
  // own markup never sees it.
  document.addEventListener("click", (e) => {
    e.preventDefault();
    e.stopPropagation();
    let el = e.target;
    while (el && el.nodeType === 1 && UNSELECTABLE[el.tagName.toLowerCase()]) el = el.parentElement;
    if (!el || el.nodeType !== 1) return;
    const sel = pathOf(el);
    outline(sel);
    selected(sel, el);
  }, true);
  document.addEventListener("submit", (e) => e.preventDefault(), true);

  // The board's gestures, forwarded. With the pointer over an artboard the wheel and the
  // trackpad pinch land in this document, and a pinch nobody handles zooms the whole browser
  // page. An artboard sizes itself to its content and never scrolls, so there is nothing
  // here for a wheel to do but move the board; the page maps the point back through the
  // frame's own transform. Safari delivers pinch as gesture* events, everything else as
  // ctrl+wheel — both go out, both are stopped here.
  document.addEventListener("wheel", (e) => {
    if (showcase) return; // the document scrolls natively in showcase
    e.preventDefault();
    post({ type: "wheel", x: e.clientX, y: e.clientY, dx: e.deltaX, dy: e.deltaY, zoom: !!(e.ctrlKey || e.metaKey) });
  }, { passive: false });
  document.addEventListener("gesturestart", (e) => { if (showcase) return; e.preventDefault(); post({ type: "pinch", phase: "start", x: e.clientX, y: e.clientY, scale: 1 }); });
  document.addEventListener("gesturechange", (e) => { if (showcase) return; e.preventDefault(); post({ type: "pinch", phase: "change", x: e.clientX, y: e.clientY, scale: e.scale }); });
  document.addEventListener("gestureend", () => { if (showcase) return; post({ type: "pinch", phase: "end", x: 0, y: 0, scale: 1 }); });

  // Keys, forwarded. A click into an artboard puts keyboard focus in this
  // frame, and the page's own listener hears nothing from then on — the very
  // action that makes a selection would disable the key that clears it. A
  // small allowlist goes out as `key` messages: Escape, F (fit), digits and
  // Enter (the open question), and ⌘/ctrl+Z (undo, with shift for its twin).
  // A press inside one of the artboard's own fields stays there, Escape aside.
  const KEYS = /^(Escape|f|F|[1-9]|Enter|z|Z)$/;
  document.addEventListener("keydown", (e) => {
    if (!KEYS.test(e.key)) return;
    const mod = !!(e.metaKey || e.ctrlKey);
    if ((e.key === "z" || e.key === "Z") !== mod) return; // z only with a modifier; the rest only without
    const t = e.target, tag = t && t.tagName ? t.tagName.toLowerCase() : "";
    const typing = tag === "input" || tag === "textarea" || tag === "select" || (t && t.isContentEditable);
    if (typing && e.key !== "Escape") return;
    if (mod || e.key === "Escape") e.preventDefault();
    post({ type: "key", key: e.key, meta: !!e.metaKey, ctrl: !!e.ctrlKey, shift: !!e.shiftKey });
  }, true);

  const docHtml = () => (document.doctype ? "<!DOCTYPE " + document.doctype.name + ">\n" : "") + document.documentElement.outerHTML;

  // One dom.py op against the live iframe DOM; refusals mirror dom.py's, and the parent re-syncs its copy from the replied html.
  const EDIT = {
    set_style(el, p) {
      const props = p.props || {}; if (!Object.keys(props).length) throw new Error("set_style needs a non-empty props object");
      for (const k of Object.keys(props)) { if (props[k] == null || props[k] === "") el.style.removeProperty(k); else el.style.setProperty(k, props[k]); }
    },
    set_text(el, p) {
      if (el.children.length) throw new Error("set_text only edits elements with no child elements — edit the inner text node directly");
      el.textContent = p.text == null ? "" : String(p.text);
    },
    insert(el, p) {
      const host = p.parent ? resolve(p.parent) : document.body, html = String(p.html || "").trim();
      if (!html) throw new Error("insert needs html");
      const tpl = document.createElement("template"); tpl.innerHTML = html;
      if (tpl.content.children.length !== 1) throw new Error("insert html did not parse");
      const els = elKids(host); let at = p.position;
      if (at == null || at === "end") at = els.length;
      else if (!Number.isInteger(Number(at))) throw new Error("bad insert position " + JSON.stringify(at));
      else if ((at = Number(at)) < 0 || at > els.length) throw new Error("insert position " + at + " is out of range");
      Array.from(tpl.content.childNodes).forEach((n) => host.insertBefore(n, els[at] || null));
    },
    delete(el) { el.remove(); },
    duplicate(el) { el.parentElement.insertBefore(el.cloneNode(true), el.nextSibling); },
    move(el, p) {
      const np = p.new_parent ? resolve(p.new_parent) : document.body;
      if (el === np || el.contains(np)) throw new Error("cannot move an element into itself");
      let to = p.index == null ? null : Number(p.index);
      if (to != null && !Number.isInteger(to)) throw new Error("bad move index " + JSON.stringify(p.index));
      el.remove(); const sibs = elKids(np);
      np.insertBefore(el, sibs[to == null ? sibs.length : Math.max(0, Math.min(to, sibs.length))] || null);
    },
  };

  function applyEdit(msg) {
    const op = msg.op, p = msg.payload || {};
    if (!OPS.includes(op)) throw new Error("unknown op " + JSON.stringify(op) + " (expected one of: " + OPS.join(", ") + ")");
    const el = op === "insert" ? null : resolve(msg.selector);
    EDIT[op](el, p);
    post({ type: "edited", op: op, selector: msg.selector, html: docHtml() });
  }

  // The layers panel's element list. The browser normalizes markup (inserts <tbody>, …); dom.py's parser does not — paths under tables can drift.
  // `text` is what the element shows — its words, an image's alt, a field's placeholder — trimmed to a row's worth, so the panel can name a
  // row by what the person sees on the artboard rather than by its tag.
  function textOf(el) {
    const own = el.getAttribute("alt") || el.getAttribute("aria-label") || el.getAttribute("placeholder") || el.textContent || "";
    return own.replace(/\s+/g, " ").trim().slice(0, 80) || undefined;
  }
  function treeOf(el, sel) {
    const kids = [...el.children].flatMap((c, i) => {
      const t = c.tagName.toLowerCase();
      return t === "html" || !UNSELECTABLE[t] ? [treeOf(c, sel + ">" + i)] : []; // skips <head> and this script
    });
    return { tag: el.tagName.toLowerCase(), sel: sel, id: el.getAttribute("id") || undefined, cls: el.getAttribute("class") || undefined,
      text: textOf(el), children: kids };
  }

  // ---- capture ----

  // The critique loop's screenshot: the artboard rasterized to a png dataURL,
  // no deps, no page reach-in. The document is serialized into an SVG
  // <foreignObject> and drawn onto a canvas — it works because artboards are
  // self-contained (inline styles, an injected theme <style>, system font
  // stacks), so the rasterizer needs nothing from off-frame. The bridge
  // script serializes along but never renders (scripts do not run inside an
  // <img>-loaded SVG), and the selection outline lives in an adopted
  // stylesheet, which serialization does not carry — the capture is the clean
  // render. Safari's foreignObject quirks (blank draws, tainted canvas)
  // reject rather than hang: the harness times the request out into a skip
  // either way.
  const CAPTURE_WIDTH = 1200;

  function raster() {
    // The critic judges the END STATE: a hero mid-entrance at opacity 0 would
    // raster as a blank page and earn a fix-list of pure hallucination. Finish
    // every animation before serializing — infinite loops excepted, finish()
    // throws on those, and the frame an infinite loop is at is the honest one.
    try {
      document.getAnimations().forEach((a) => {
        try { a.finish(); } catch (err) { /* infinite or already finished */ }
      });
    } catch (err) { /* no Web Animations API: nothing to finish */ }
    const doc = document.documentElement;
    const w = Math.max(doc.scrollWidth, 1);
    // the same height the 'size' reply reports: scrollHeight floors at the
    // viewport, so a short page would capture a frame-sized void below it
    const h = Math.ceil(Math.max(document.body.scrollHeight, document.body.getBoundingClientRect().bottom)) || 1;
    const scale = Math.min(1, CAPTURE_WIDTH / w);
    const xml = new XMLSerializer().serializeToString(doc);
    const svg = "<svg xmlns='http://www.w3.org/2000/svg' width='" + w + "' height='" + h + "'>" +
      "<foreignObject width='100%' height='100%'>" + xml + "</foreignObject></svg>";
    return new Promise((resolve, reject) => {
      const img = new Image();
      img.onload = () => {
        try {
          const canvas = document.createElement("canvas");
          canvas.width = Math.max(1, Math.round(w * scale));
          canvas.height = Math.max(1, Math.round(h * scale));
          const ctx = canvas.getContext("2d");
          if (!ctx) throw new Error("no 2d drawing context");
          ctx.scale(scale, scale);
          ctx.drawImage(img, 0, 0);
          // throws on a tainted canvas — the rejection the harness reads
          const png = canvas.toDataURL("image/png");
          // a blank draw (a Safari quirk) is a valid png of nothing: judging
          // it would return a fix-list of pure hallucination, so refuse it
          if (png.length < 512) throw new Error("the raster came back empty");
          resolve(png);
        } catch (err) { reject(err); }
      };
      img.onerror = () => reject(new Error("the raster image did not load"));
      img.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(svg);
    });
  }

  window.addEventListener("message", (e) => {
    const msg = e.data; if (!msg || typeof msg !== "object") return;
    try {
      if (msg.type === "select") { const el = resolve(msg.selector); outline(msg.selector); selected(msg.selector, el); }
      else if (msg.type === "deselect") { if (sheet) sheet.replaceSync(""); }
      else if (msg.type === "edit") applyEdit(msg);
      else if (msg.type === "getTree") post({ type: "tree", tree: treeOf(document.documentElement, "0") });
      else if (msg.type === "showcase") showcase = true;
      // the frame sizes itself to the document, so nothing overflows the board
      // documentElement.scrollHeight floors at the viewport, so a short page would
      // report the frame height back at us and never shrink to fit its content
      else if (msg.type === "size") post({ type: "size", w: document.documentElement.scrollWidth,
        h: Math.ceil(Math.max(document.body.scrollHeight, document.body.getBoundingClientRect().bottom)) });
      else if (msg.type === "capture") {
        // async by nature: the reply is 'captured', not the synchronous
        // round trip the other commands use. The id rides along so the page
        // can match the answer to the request that asked for it.
        raster().then(
          (png) => post({ type: "captured", id: msg.id, png: png }),
          (err) => post({ type: "error", command: "capture", id: msg.id,
            message: String((err && err.message) || err) }),
        );
      }
    } catch (err) {
      // a quiet command is the panel re-selecting after a reload: the path may
      // simply not exist any more, and that is not something to report
      if (!msg.quiet) post({ type: "error", command: msg.type, message: String((err && err.message) || err) });
    }
  });
})();