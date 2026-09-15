import { Fragment, useState } from "react";
import { sendOp } from "../board/edits";
import { postToFrame } from "../board/frames";
import { useUi } from "../board/ui";
import { useSession } from "../wire/session";
import type { TreeNode, WireArtboard, WireVersion } from "../wire/types";

/** Who made a version, off the note mutate.apply writes ("<actor>: <op>"). */
function authorOf(v: WireVersion): { by: "ai" | "user" | ""; op: string } {
  const bits = String(v.note || v.kind || "").split(":");
  if (bits.length < 2) return { by: "", op: (v.note || v.kind || "").trim() };
  const who = bits[0].trim();
  return { by: who === "ai" ? "ai" : who === "user" ? "user" : "", op: bits.slice(1).join(":").trim() };
}

function Section({ label, extra, children }: { label: string; extra?: React.ReactNode; children: React.ReactNode }) {
  return (
    <div className="sect">
      <div className="sect-head"><span className="lbl">{label}</span>{extra}</div>
      {children}
    </div>
  );
}

function Empty({ children }: { children: React.ReactNode }) {
  return <div className="rail-empty">{children}</div>;
}

/* ── layers ─────────────────────────────────────────────────────────── */

/** What an element is called for a person who is not thinking in tags. */
const KIND: Record<string, string> = {
  header: "Header", footer: "Footer", main: "Main", nav: "Navigation", section: "Section",
  article: "Article", aside: "Aside", form: "Form", ul: "List", ol: "List", li: "Item",
  table: "Table", tr: "Row", td: "Cell", th: "Cell", h1: "Heading", h2: "Heading", h3: "Heading",
  h4: "Heading", h5: "Heading", h6: "Heading", p: "Paragraph", a: "Link", button: "Button",
  img: "Image", svg: "Icon", input: "Field", textarea: "Field", select: "Field", label: "Label",
  hr: "Rule", blockquote: "Quote", figure: "Figure", video: "Video", audio: "Audio",
  span: "Text", div: "Group", strong: "Text", em: "Text", b: "Text", i: "Text", small: "Text",
  code: "Code", pre: "Code", br: "Break",
};
const kindOf = (tag: string) => KIND[tag] ?? tag;

/** Inline wrappers: a paragraph holding a <strong> is still one line of text. */
const INLINE = new Set(["span", "strong", "em", "b", "i", "small", "code", "a", "br", "sup", "sub", "mark"]);

const plural = (name: string, n: number) => (n === 1 || name.endsWith("s") ? name : name + "s");

/** A container's row, in the words of what it holds: "3 links", "2 groups · 1 heading". */
function summary(node: TreeNode): string {
  const counts = new Map<string, number>();
  for (const k of node.children) {
    const name = kindOf(k.tag).toLowerCase();
    counts.set(name, (counts.get(name) ?? 0) + 1);
  }
  const parts = [...counts].map(([name, n]) => `${n} ${plural(name, n)}`);
  return parts.length > 2 ? `${node.children.length} items` : parts.join(" · ");
}

/** The row's name: the words on the artboard when there are any, the kind
 *  of thing when there are not. A container names itself by its words only
 *  when it is really one line of text dressed in inline tags. */
function labelOf(node: TreeNode): string {
  const branch = node.children.length > 0;
  if (!branch) return node.text || kindOf(node.tag);
  if (node.text && node.children.every((k) => INLINE.has(k.tag))) return node.text;
  return kindOf(node.tag);
}

function Layer({ node, depth, selected, open, onToggle, onPick }: {
  node: TreeNode; depth: number; selected: string | null;
  open: Record<string, boolean>; onToggle: (sel: string, next: boolean) => void; onPick: (sel: string) => void;
}) {
  const branch = node.children.length > 0;
  const on = selected === node.sel;
  /* the top two levels open; deeper ones fold, unless the selection is inside */
  const expanded = branch && (open[node.sel] ?? (depth < 1 || Boolean(selected?.startsWith(node.sel + ">"))));
  const tag = node.tag + (node.id ? "#" + node.id : "");
  return (
    <>
      <div className={"layer" + (on ? " on" : "") + (branch ? " branch" : "")}
           style={{ "--depth": depth } as React.CSSProperties} data-od-id={"layer-" + node.sel}>
        {branch ? (
          <button type="button" className="fold" aria-expanded={expanded}
                  aria-label={(expanded ? "Fold " : "Unfold ") + labelOf(node)}
                  onClick={() => onToggle(node.sel, !expanded)}>▸</button>
        ) : <span className="fold-space" />}
        <button type="button" className="pick" onClick={() => onPick(node.sel)}
                title={`${tag} · ${node.sel}`}>
          <span className="nm">{labelOf(node)}</span>
          {branch ? <span className="sum">{summary(node)}</span> : null}
        </button>
        <span className="tag mono">{tag}</span>
      </div>
      {expanded ? node.children.map((k) => (
        <Layer key={k.sel} node={k} depth={depth + 1} selected={selected} open={open} onToggle={onToggle} onPick={onPick} />
      )) : null}
    </>
  );
}

/** The page starts at the body: html and body are the paper, not the design. */
function rowsOf(tree: TreeNode): TreeNode[] {
  const body = tree.tag === "body" ? tree : tree.children.find((k) => k.tag === "body");
  return body ? body.children : tree.children;
}

function Layers({ focus, tree }: { focus: WireArtboard | null; tree: TreeNode | null }) {
  const ui = useUi();
  const [open, setOpen] = useState<Record<string, boolean>>({});
  if (!focus) {
    return (
      <Section label="Layers">
        <Empty>Click into any frame on the canvas to inspect and edit its elements.</Empty>
      </Section>
    );
  }
  const selected = ui.selection?.artboard === focus.id ? ui.selection.selector : null;
  const rows = tree ? rowsOf(tree) : [];
  return (
    <Section label="Layers" extra={<span className="lbl light">{focus.title}</span>}>
      {rows.length ? (
        <div className="layers" role="tree" aria-label={"elements of " + focus.title}>
          {rows.map((k) => (
            <Layer key={k.sel} node={k} depth={0} selected={selected} open={open}
                   onToggle={(sel, next) => setOpen((o) => ({ ...o, [sel]: next }))}
                   onPick={(sel) => postToFrame(focus.id, { type: "select", selector: sel })} />
          ))}
        </div>
      ) : <Empty>Click any element in {focus.title} to select it.</Empty>}
    </Section>
  );
}

/* ── inspector ──────────────────────────────────────────────────────── */

/** A field that commits on Enter or blur, never on every keystroke: every
 *  commit is a version on the shared stack. */
function Field({ value, placeholder, disabled, onCommit }: {
  value: string; placeholder?: string; disabled?: boolean; onCommit: (v: string) => void;
}) {
  const [draft, setDraft] = useState(value);
  const [was, setWas] = useState(value);
  if (value !== was) { setWas(value); setDraft(value); }
  return (
    <input
      className="fld"
      value={draft}
      placeholder={placeholder}
      disabled={disabled}
      onChange={(e) => setDraft(e.target.value)}
      onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); e.currentTarget.blur(); } }}
      onBlur={() => { if (draft !== value) onCommit(draft); }}
    />
  );
}

function Inspector() {
  const { selection: sel } = useUi();
  const [prop, setProp] = useState("");
  if (!sel) return null;
  const parts = sel.selector.split(">");
  const parent = parts.slice(0, -1).join(">");
  const index = Number(parts[parts.length - 1]);
  return (
    <Section label="Inspector">
      <div className="insp-head" title={sel.selector}>
        <span className="kind">{kindOf(sel.tag)}</span>
        <span className="tag mono">{sel.tag}</span>
        <span className="leaf mono">{sel.leaf ? "leaf" : "has children"}</span>
      </div>
      <div className="kv">
        <span className="k">text</span>
        <Field value={sel.text} disabled={!sel.leaf}
               placeholder={sel.leaf ? "" : "set_text only edits elements with no child elements"}
               onCommit={(text) => sendOp({ op: "set_text", selector: sel.selector, text })} />
      </div>
      <div className="kv">
        {Object.keys(sel.props).map((p) => (
          <Fragment key={p}>
            <span className="k">{p}</span>
            <Field value={sel.props[p]}
                   onCommit={(v) => sendOp({ op: "set_style", selector: sel.selector, props: { [p]: v } })} />
          </Fragment>
        ))}
        <input className="fld" placeholder="property" value={prop} onChange={(e) => setProp(e.target.value)}
               aria-label="a css property to set" />
        <Field value="" placeholder="value" onCommit={(v) => {
          const name = prop.trim();
          if (!name) return;
          sendOp({ op: "set_style", selector: sel.selector, props: { [name]: v } });
          setProp("");
        }} />
      </div>
      <div className="insp-actions">
        <button type="button" onClick={() => sendOp({ op: "duplicate", selector: sel.selector })}>Duplicate</button>
        <button type="button" onClick={() => sendOp({ op: "delete", selector: sel.selector })}>Delete</button>
        {parent && index > 0
          ? <button type="button" className="icon" title="Move up"
                    onClick={() => sendOp({ op: "move", selector: sel.selector, new_parent: parent, index: index - 1 })}>↑</button>
          : null}
        {parent
          ? <button type="button" className="icon" title="Move down"
                    onClick={() => sendOp({ op: "move", selector: sel.selector, new_parent: parent, index: index + 1 })}>↓</button>
          : null}
      </div>
    </Section>
  );
}

/* ── history ────────────────────────────────────────────────────────── */

function History({ ab, label = "History", current = true }: { ab: WireArtboard; label?: string; current?: boolean }) {
  if (!ab.versions.length) return null;
  return (
    <Section label={label} extra={current ? <span className="lbl light">one stack</span> : undefined}>
      <div className="history">
        {[...ab.versions].reverse().map((v) => {
          const { by, op } = authorOf(v);
          return (
            <div className={"ver" + (current && v.id === ab.current ? " cur" : "")} data-by={by} key={v.id}>
              <span className="id mono">{v.id}</span>
              <span className="op mono">{op}</span>
              {by ? <span className="who mono">{by === "ai" ? "designer" : "you"}</span> : null}
            </div>
          );
        })}
      </div>
    </Section>
  );
}

function Handoff({ ab }: { ab: WireArtboard }) {
  const { ready } = useSession();
  const path = ready ? `.bird/sessions/${ready.run_id}/bundle/DESIGN.md` : "bundle/DESIGN.md";
  const edits = ab.versions.filter((v) => authorOf(v).by).length;
  return (
    <>
      <Section label="Handoff">
        <div className="handoff">
          <div className="file">
            <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.3"
                 strokeLinejoin="round" aria-hidden="true"><path d="M4 1.8h5l3 3v9.4H4z" /><path d="M9 1.8v3.2h3" /></svg>
            <span className="mono">DESIGN.md</span>
          </div>
          <div className="rule" />
          <div className="facts mono">
            <span title={path}>{path}</span>
            <span>the artboard, clean — no editor script</span>
            <span>the brief it came from</span>
            <span>{ab.versions.length} version{ab.versions.length === 1 ? "" : "s"}, {edits} edit{edits === 1 ? "" : "s"}, who made each</span>
          </div>
        </div>
      </Section>
      <History ab={ab} label="Edit history" current={false} />
      <Section label="Next">
        <div className="next">
          <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5"
               strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M2.5 8h10" /><path d="M9 4.5L12.5 8 9 11.5" /></svg>
          <span><b>code</b> builds from this.</span>
        </div>
      </Section>
      <p className="rail-note">Every version is kept. Reopen the session to keep working.</p>
    </>
  );
}

/* ── the rail ───────────────────────────────────────────────────────── */

/** Whether the rail has anything to say: a selection to inspect, or a
 *  finished design to hand off. Otherwise the board takes the space. */
export function railOpen(status: string, hasSelection: boolean): boolean {
  return status === "finalized" || hasSelection;
}

/** The rail is the inspector's shadow: it is on the page exactly when there
 *  is a selection to inspect — layers, the element, the artboard's history —
 *  and, once the design is finalized, the handoff. */
export function Rail() {
  const session = useSession();
  const ui = useUi();
  const focus = session.artboards.find((a) => a.id === ui.focus) ?? null;
  const chosen = session.artboards.find((a) => a.id === session.finalizedArtboard) ?? null;

  if (session.status === "finalized" && chosen) {
    return <aside className="rail" id="rail" data-od-id="rail"><Handoff ab={chosen} /></aside>;
  }
  const selected = ui.selection && focus && ui.selection.artboard === focus.id ? focus : null;
  if (!selected) return <aside className="rail" id="rail" data-od-id="rail" aria-hidden="true" />;
  return (
    <aside className="rail" id="rail" data-od-id="rail">
      <Layers focus={selected} tree={ui.trees[selected.id] ?? null} />
      <Inspector />
      <History ab={selected} />
    </aside>
  );
}
