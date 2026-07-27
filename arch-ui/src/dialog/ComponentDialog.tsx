/**
 * A component's internals, on their own surface.
 *
 * Two rules from the design handover drive the whole shape of this file:
 *
 *  1. **The system graph never reflows.** Node positions are user-owned, so
 *     growing a node in place would shove hand-placed neighbours. The dialog
 *     floats over a dimmed canvas instead and the graph behind it does not move
 *     by a pixel.
 *  2. **One shell, every facet kind.** The frame — header, tabs, port chips,
 *     footer — is constant; `facet.facet_kind` picks the body. A component with
 *     no facet yet opens the same dialog in an empty state, so "nothing here
 *     yet" is a place you can stand rather than a dead click.
 *
 * Everything renders straight from `arch_state`, so a dialog left open while
 * the architect runs `expand` fills in underneath the user without closing.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ChangeEvent, KeyboardEvent, PointerEvent, ReactNode } from "react";
import {
  Background,
  BackgroundVariant,
  Controls,
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
} from "@xyflow/react";

import { editComponent, sendInput, useSession } from "../store/session";
import { useCanvas } from "../store/canvas";
import { facetGraph, ports, type SubGraph } from "./graphs";
import { dialogNodeTypes } from "./nodes";
import type { Component, Concern, Endpoint } from "../types";

const MIN_W = 640;
const MIN_H = 470;

/** Tabs whose body is a React Flow surface rather than a document. */
const CANVAS_TABS = new Set(["entities", "modules", "messages", "units", "tasks"]);

interface Tab { id: string; label: string; count?: number }

function tabsFor(comp: Component): Tab[] {
  const f = comp.facet;
  const tabs: Tab[] = [];
  if (!f) {
    // a black box still opens: "nothing here yet" is a place to stand, with
    // the action that fills it, not a dead click on the canvas
    tabs.push({ id: "internals", label: "Internals" });
  } else {
    switch (f.facet_kind) {
      case "store":
        tabs.push(
          { id: "entities", label: "Entities", count: f.entities.length },
          { id: "access", label: "Access patterns", count: f.access_patterns.length },
          { id: "retention", label: "Retention" },
        );
        break;
      case "service":
        tabs.push(
          { id: "modules", label: "Modules", count: f.modules?.length ?? 0 },
          { id: "interface", label: "Interface", count: f.interface.length },
        );
        break;
      case "queue":
        tabs.push({ id: "messages", label: "Messages", count: f.messages.length });
        break;
      case "api":
        tabs.push({ id: "endpoints", label: "Endpoints", count: f.endpoints.length });
        break;
      case "infra":
        tabs.push(
          { id: "units", label: "Deployment", count: f.units.length },
          { id: "locality", label: "State locality" },
        );
        break;
      case "llm":
        tabs.push({ id: "tasks", label: "Tasks", count: f.tasks.length });
        break;
    }
  }
  tabs.push({ id: "contract", label: "Contract" });
  return tabs;
}

function summarize(comp: Component): string {
  const f = comp.facet;
  if (!f) return "no internals yet";
  switch (f.facet_kind) {
    case "api": return `${f.endpoints.length} endpoint${f.endpoints.length === 1 ? "" : "s"}`;
    case "store": return `${f.entities.length} entit${f.entities.length === 1 ? "y" : "ies"}`;
    case "queue": return `${f.messages.length} message${f.messages.length === 1 ? "" : "s"}`;
    case "service": return `${f.modules?.length ?? 0} modules · ${f.interface.length} exposed`;
    case "llm": return `${f.tasks.length} task${f.tasks.length === 1 ? "" : "s"}`;
    case "infra": return `${f.units.length} deploy unit${f.units.length === 1 ? "" : "s"}`;
    default: return "";
  }
}

// ---------------------------------------------------------------- editing

function Editable({ value, onSave, mode = "line", placeholder, disabled }: {
  value: string;
  onSave: (next: string) => Promise<boolean>;
  mode?: "line" | "text" | "title";
  placeholder?: string;
  disabled?: boolean;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value);
  const [saving, setSaving] = useState(false);

  useEffect(() => { if (!editing) setDraft(value); }, [value, editing]);

  const commit = async () => {
    setEditing(false);
    if (draft === value) return;
    setSaving(true);
    // a refusal rolls the store back; `value` then flows in and resets the draft
    await onSave(draft);
    setSaving(false);
  };

  if (disabled) {
    return <span className={`ed ${mode}`}>{value || <i className="faint">{placeholder}</i>}</span>;
  }
  if (!editing) {
    return (
      <span
        className={`ed ${mode} editable`}
        role="button"
        tabIndex={0}
        title="click to edit"
        onClick={() => setEditing(true)}
        onKeyDown={(e) => { if (e.key === "Enter") setEditing(true); }}
      >
        {value || <i className="faint">{placeholder}</i>}
        {saving && <span className="faint tiny"> saving…</span>}
      </span>
    );
  }
  const shared = {
    autoFocus: true,
    value: draft,
    onBlur: commit,
    onChange: (e: ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => setDraft(e.target.value),
    onKeyDown: (e: KeyboardEvent) => {
      if (e.key === "Escape") { e.stopPropagation(); setDraft(value); setEditing(false); }
      if (e.key === "Enter" && (mode !== "text" || (e.metaKey || e.ctrlKey))) {
        e.preventDefault();
        void commit();
      }
    },
  };
  return mode === "text"
    ? <textarea className="ed-input text" rows={3} {...shared} />
    : <input className={`ed-input ${mode}`} {...shared} />;
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="dfield">
      <div className="section-label">{label}</div>
      <div>{children}</div>
    </div>
  );
}

// ---------------------------------------------------------------- bodies

/** Never zoom out past legibility: a wide chain that runs off the frame and
 *  can be panned beats one that fits and cannot be read. */
const FIT = { padding: 0.18, minZoom: 0.72, maxZoom: 1.1 } as const;

function FacetCanvasInner({ graph, comp }: { graph: SubGraph; comp: Component }) {
  const { fitView } = useReactFlow();
  const fitted = useRef("");
  const signature = `${comp.id}:${graph.nodes.length}`;

  // fit once per (component, node-count): a mid-turn arrival gets framed, a
  // re-render for anything else leaves the user's pan and zoom alone
  useEffect(() => {
    if (graph.nodes.length === 0 || fitted.current === signature) return;
    fitted.current = signature;
    const t = setTimeout(() => fitView({ ...FIT, duration: 220 }), 40);
    return () => clearTimeout(t);
  }, [signature, graph.nodes.length, fitView]);

  return (
    <ReactFlow
      nodes={graph.nodes}
      edges={graph.edges}
      nodeTypes={dialogNodeTypes}
      nodesConnectable={false}
      elementsSelectable
      proOptions={{ hideAttribution: true }}
      minZoom={0.3}
      maxZoom={2.2}
      fitView
      fitViewOptions={FIT}
    >
      <Background variant={BackgroundVariant.Dots} gap={16} size={1} color="var(--hairline)" />
      <Controls showInteractive={false} position="bottom-right" />
    </ReactFlow>
  );
}

function FacetCanvas({ graph, comp, empty }: { graph: SubGraph; comp: Component; empty: string }) {
  if (graph.nodes.length === 0) return <div className="dialog-empty"><p>{empty}</p></div>;
  return (
    <div className="dialog-canvas">
      <ReactFlowProvider>
        <FacetCanvasInner graph={graph} comp={comp} />
      </ReactFlowProvider>
      {graph.note && <div className="canvas-note">{graph.note}</div>}
    </div>
  );
}

function Lines({ items, empty }: { items: string[]; empty: string }) {
  if (items.length === 0) return <div className="dialog-empty"><p>{empty}</p></div>;
  return (
    <div className="dialog-scroll">
      <ul className="plain">{items.map((x, i) => <li key={`${x}:${i}`}>{x}</li>)}</ul>
    </div>
  );
}

function EndpointTable({ endpoints }: { endpoints: Endpoint[] }) {
  if (endpoints.length === 0) {
    return <div className="dialog-empty"><p>No endpoints recorded yet.</p></div>;
  }
  const byRoute = new Map<string, Endpoint[]>();
  for (const e of endpoints) {
    if (!byRoute.has(e.route)) byRoute.set(e.route, []);
    byRoute.get(e.route)!.push(e);
  }
  return (
    <div className="dialog-scroll">
      {[...byRoute].map(([route, list]) => (
        <div key={route} className="route-group">
          <div className="route mono">{route}</div>
          <table className="endpoints">
            <tbody>
              {list.map((e, i) => (
                <tr key={`${e.method}:${i}`}>
                  <td className="mono method">{e.method}</td>
                  <td>
                    <div className="io mono">
                      <span title="request">→ {e.request || "—"}</span>
                      <span title="response">← {e.response || "—"}</span>
                    </div>
                    <div className="tiny faint">
                      auth: {e.auth || "unstated"}
                      {e.idempotency ? ` · idempotency: ${e.idempotency}` : ""}
                      {e.pagination ? ` · pages: ${e.pagination}` : ""}
                    </div>
                    {e.errors.length > 0 && (
                      <div className="tiny">
                        {e.errors.map((x) => <span key={x} className="pill mono">{x}</span>)}
                      </div>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}
    </div>
  );
}

function Contract({ comp, gaps, concerns, owed, editable }: {
  comp: Component;
  gaps: string[];
  concerns: Concern[];
  owed: string | null;
  editable: boolean;
}) {
  const save = (patch: Record<string, unknown>) => editComponent(comp.id, patch);
  return (
    <div className="dialog-scroll contract">
      <Field label="responsibility">
        <Editable
          value={comp.responsibility}
          mode="text"
          placeholder="one sentence on what it does"
          disabled={!editable}
          onSave={(responsibility) => save({ responsibility })}
        />
      </Field>
      <div className="two-up">
        <Field label="tech">
          <Editable value={comp.tech ?? ""} placeholder="unstated" disabled={!editable}
                    onSave={(tech) => save({ tech })} />
        </Field>
        {(comp.kind === "store" || comp.kind === "cache" || comp.data_owned) && (
          <Field label="data owned">
            <Editable value={comp.data_owned ?? ""} placeholder="what data does it own?"
                      disabled={!editable} onSave={(data_owned) => save({ data_owned })} />
          </Field>
        )}
      </div>
      <Field label="serves (trace)">
        <Editable
          value={comp.trace.join("\n")}
          mode="text"
          placeholder="which brief goals does it serve? one per line"
          disabled={!editable}
          onSave={(text) => save({ trace: text.split("\n").map((s) => s.trim()).filter(Boolean) })}
        />
      </Field>
      <Field label="when it fails">
        <Editable value={comp.failure_notes ?? ""} mode="text" placeholder="not recorded"
                  disabled={!editable} onSave={(failure_notes) => save({ failure_notes })} />
      </Field>

      {owed && (
        <div className="dnote">
          <b>Owes depth.</b> {owed}
        </div>
      )}
      {gaps.length > 0 && (
        <Field label={`thin — ${gaps.length}, none required`}>
          <ul className="plain faint">{gaps.map((g) => <li key={g}>{g}</li>)}</ul>
        </Field>
      )}
      {concerns.length > 0 && (
        <Field label="objections against this component">
          {concerns.map((c) => (
            <div key={c.id} className="card" data-severity={c.severity} data-settled={c.status !== "open"}>
              <div className="head">
                <span className="tag">{c.severity}</span>
                <span className="tag">{c.status === "open" ? c.source : c.status}</span>
              </div>
              <p style={{ color: "var(--ink)" }}>{c.claim}</p>
              {c.alternative && <p>instead: {c.alternative}</p>}
              {c.resolution && <p className="faint">→ {c.resolution}</p>}
            </div>
          ))}
        </Field>
      )}
      <p className="faint tiny">
        id <code className="mono">{comp.id}</code> is immutable — renaming changes the name only,
        so every connection, flow and bundle reference keeps pointing at the same thing.
      </p>
    </div>
  );
}

function EmptyFacet({ comp, owed, canAsk }: { comp: Component; owed: string | null; canAsk: boolean }) {
  const notOurs = comp.kind === "external";
  return (
    <div className="dialog-empty">
      <div>
        <h4>Nothing recorded inside {comp.name} yet</h4>
        <p>{comp.responsibility || "No responsibility recorded either — worth asking for."}</p>
        {owed && <p className="dnote"><b>Owes depth.</b> {owed}</p>}
        {notOurs ? (
          <p className="faint">
            It is an external system — its internals are not ours to design, only its contract.
          </p>
        ) : (
          <button
            className="primary"
            disabled={!canAsk}
            onClick={() => sendInput(
              `Expand ${comp.id} — record its internals: the contract a builder would need, ` +
              `not a description of the code.`,
            )}
          >
            Expand this component
          </button>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- the shell

export function ComponentDialog() {
  const openId = useCanvas((s) => s.openComponent);
  const show = useCanvas((s) => s.openComponentDialog); // show(null) closes
  const tab = useCanvas((s) => s.dialogTab);
  const setTab = useCanvas((s) => s.setDialogTab);
  const size = useCanvas((s) => s.dialogSize);
  const setSize = useCanvas((s) => s.setDialogSize);
  const full = useCanvas((s) => s.dialogFull);
  const setFull = useCanvas((s) => s.setDialogFull);

  const arch = useSession((s) => s.arch);
  const gapsBySubject = useSession((s) => s.gaps);
  const finalized = useSession((s) => s.finalized);
  const running = useSession((s) => s.running);
  const conn = useSession((s) => s.conn);

  const comp = openId ? arch?.components[openId] : undefined;

  // Escape closes. Bound while open only, so it stays free the rest of the time.
  useEffect(() => {
    if (!comp) return;
    const onKey = (e: globalThis.KeyboardEvent) => { if (e.key === "Escape") show(null); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [comp, show]);

  const graph = useMemo(
    () => (comp ? facetGraph(comp, arch?.components ?? {}) : { nodes: [], edges: [] }),
    [comp, arch?.components],
  );
  const wired = useMemo(
    () => (comp ? ports(comp, arch?.connections ?? [], arch?.components ?? {})
                : { inbound: [], outbound: [] }),
    [comp, arch?.connections, arch?.components],
  );

  const onResize = useCallback((e: PointerEvent) => {
    e.preventDefault();
    const startX = e.clientX, startY = e.clientY;
    const from = { ...size };
    // doubled: the dialog is centred, so it grows from both edges at once
    const move = (ev: globalThis.PointerEvent) => setSize({
      w: Math.max(MIN_W, from.w + (ev.clientX - startX) * 2),
      h: Math.max(MIN_H, from.h + (ev.clientY - startY) * 2),
    });
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  }, [size, setSize]);

  if (!comp || !arch) return null;

  const tabs = tabsFor(comp);
  const active = tabs.some((t) => t.id === tab) ? tab : tabs[0].id;
  const editable = !finalized && conn !== "disconnected";
  const obligation = arch.obligations.find(
    (o) => o.component_id === comp.id && o.status === "pending",
  );
  const owed = obligation ? obligation.reason : null;
  const concerns = arch.concerns.filter((c) => c.target === comp.id);
  const openConcerns = concerns.filter((c) => c.status === "open").length;

  const body = () => {
    switch (active) {
      case "internals":
        return <EmptyFacet comp={comp} owed={owed} canAsk={editable && !running} />;
      case "entities":
        return <FacetCanvas graph={graph} comp={comp} empty="No entities recorded yet." />;
      case "modules":
        return <FacetCanvas graph={graph} comp={comp}
                            empty="No modules recorded — a service's internals are usually the code harness's job." />;
      case "messages":
        return <FacetCanvas graph={graph} comp={comp} empty="No message contracts recorded yet." />;
      case "units":
        return <FacetCanvas graph={graph} comp={comp} empty="No deployment units recorded yet." />;
      case "tasks":
        return <FacetCanvas graph={graph} comp={comp} empty="No llm tasks recorded yet." />;
      case "endpoints":
        return <EndpointTable endpoints={comp.facet?.facet_kind === "api" ? comp.facet.endpoints : []} />;
      case "access":
        return <Lines items={comp.facet?.facet_kind === "store" ? comp.facet.access_patterns : []}
                      empty="No access patterns recorded — the queries are what the schema is for." />;
      case "retention":
        return (
          <div className="dialog-scroll contract">
            <Field label="retention">
              {comp.facet?.facet_kind === "store" && comp.facet.retention
                ? comp.facet.retention
                : <i className="faint">not recorded — the data grows without bound until someone says otherwise</i>}
            </Field>
            <Field label="migration risk">
              {comp.facet?.facet_kind === "store" && comp.facet.migration_risk
                ? comp.facet.migration_risk
                : <i className="faint">not recorded</i>}
            </Field>
          </div>
        );
      case "interface":
        return <Lines items={comp.facet?.facet_kind === "service" ? comp.facet.interface : []}
                      empty="Nothing recorded about what it exposes to its siblings." />;
      case "locality":
        return (
          <div className="dialog-scroll contract">
            <Field label="state locality">
              {comp.facet?.facet_kind === "infra" && comp.facet.state_locality
                ? comp.facet.state_locality
                : <i className="faint">not recorded</i>}
            </Field>
          </div>
        );
      default:
        return (
          <Contract
            comp={comp}
            gaps={gapsBySubject[comp.id] ?? []}
            concerns={concerns}
            owed={owed}
            editable={editable}
          />
        );
    }
  };

  return (
    <div className="dialog-backdrop" onPointerDown={(e) => { if (e.target === e.currentTarget) show(null); }}>
      <div
        className="dialog"
        data-full={full}
        style={full ? undefined : { width: size.w, height: size.h }}
        role="dialog"
        aria-label={`${comp.name} internals`}
      >
        <header className="dialog-head">
          <span className="kind mono">{comp.kind}</span>
          <h3>
            <Editable value={comp.name} mode="title" disabled={!editable}
                      onSave={(name) => editComponent(comp.id, { name })} />
          </h3>
          <span className="chip">{summarize(comp)}</span>
          {openConcerns > 0 && (
            <span className="chip danger">{openConcerns} open objection{openConcerns === 1 ? "" : "s"}</span>
          )}
          <span className="spacer" />
          <button className="ghost" onClick={() => setFull(!full)}>
            {full ? "Dock" : "Full canvas"}
          </button>
          <button className="ghost" onClick={() => show(null)} aria-label="close">✕</button>
        </header>

        <nav className="dialog-tabs">
          {tabs.map((t) => (
            <button key={t.id} data-on={t.id === active} onClick={() => setTab(t.id)}>
              {t.label}
              {t.count !== undefined && <span className="count">{t.count}</span>}
            </button>
          ))}
        </nav>

        <div className="dialog-body">
          {body()}

          {/* the real neighbours, pinned to the frame — internals stay anchored
              to the system graph the dialog is floating over. Clicking one walks
              there without going back to the canvas first. */}
          <div className="ports left">
            {wired.inbound.map((p, i) => (
              <button key={`${p.id}:${i}`} className="port" title={`${p.name} — ${p.label} (${p.kind})`}
                      onClick={() => show(p.id)}>
                {p.name}<span className="faint"> · {p.label}</span>
              </button>
            ))}
          </div>
          <div className="ports right">
            {wired.outbound.map((p, i) => (
              <button key={`${p.id}:${i}`} className="port" title={`${p.name} — ${p.label} (${p.kind})`}
                      onClick={() => show(p.id)}>
                {p.name}<span className="faint"> · {p.label}</span>
              </button>
            ))}
          </div>
        </div>

        <footer className="dialog-foot">
          <span className="faint tiny">
            ⎋ close{CANVAS_TABS.has(active) ? " · drag to rearrange · pan and zoom are its own" : ""}
            {" · the system graph behind this has not moved"}
          </span>
          <span className="spacer" />
          {!editable && <span className="chip">read-only</span>}
        </footer>

        {!full && <div className="dialog-grip" onPointerDown={onResize} title="drag to resize" />}
      </div>
    </div>
  );
}
