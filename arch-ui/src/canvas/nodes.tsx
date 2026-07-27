/**
 * Node cards. A node *is* the contract sheet — that is the whole reason for
 * rendering from structured state rather than a diagram image.
 */
import { Handle, Position, type NodeProps } from "@xyflow/react";
import { useCanvas } from "../store/canvas";
import type { Component, Concern, SketchNode } from "../types";

/**
 * Four anchors, not two. Forward edges run left→right along the layering;
 * feedback edges leave and re-enter underneath, so a loop back to the entry
 * point reads as a loop instead of a stray line across the diagram.
 */
export const HANDLE = { in: "in", out: "out", loopIn: "loop-in", loopOut: "loop-out" } as const;

function Anchors() {
  return (
    <>
      <Handle id={HANDLE.in} type="target" position={Position.Left} />
      <Handle id={HANDLE.out} type="source" position={Position.Right} />
      <Handle id={HANDLE.loopIn} type="target" position={Position.Bottom} style={{ left: "35%" }} />
      <Handle id={HANDLE.loopOut} type="source" position={Position.Bottom} style={{ left: "65%" }} />
    </>
  );
}

function facetSummary(c: Component): string | null {
  const f = c.facet;
  if (!f) return null;
  switch (f.facet_kind) {
    case "api": return `${f.endpoints.length} endpoint${f.endpoints.length === 1 ? "" : "s"}`;
    case "store": return `${f.entities.length} entit${f.entities.length === 1 ? "y" : "ies"}`;
    case "queue": return `${f.messages.length} message${f.messages.length === 1 ? "" : "s"}`;
    case "service": return f.modules?.length ? `${f.modules.length} modules` : `${f.interface.length} exposed`;
    case "llm": return `${f.tasks.length} task${f.tasks.length === 1 ? "" : "s"}`;
    case "infra": return `${f.units.length} unit${f.units.length === 1 ? "" : "s"}`;
    default: return null;
  }
}

export interface ComponentNodeData extends Record<string, unknown> {
  component: Component;
  gaps: string[];
  concerns: Concern[];
  owes: boolean;
  changed: boolean;
}

export function ComponentNode({ data, selected }: NodeProps) {
  const { component: c, gaps, concerns, owes, changed } = data as ComponentNodeData;
  const open = useCanvas((s) => s.openComponentDialog);
  const facet = facetSummary(c);
  const worst = concerns.find((x) => x.severity === "blocker") ?? concerns[0];
  const title = [
    gaps.length ? `Thin:\n· ${gaps.join("\n· ")}` : "",
    concerns.length ? `Concerns:\n· ${concerns.map((x) => `[${x.severity}] ${x.claim}`).join("\n· ")}` : "",
  ].filter(Boolean).join("\n\n");

  return (
    <div
      className="node"
      data-selected={selected}
      data-changed={changed}
      data-existing={c.existing}
      title={title || undefined}
    >
      <Anchors />
      {owes && <span className="owe-dot" title="owes a facet" />}
      {/* internals open in their own dialog — never by growing the card, which
          would shove neighbours the user placed by hand */}
      <button
        className="open-btn"
        title="open its internals (E)"
        onClick={(e) => { e.stopPropagation(); open(c.id); }}
      >
        ⤢
      </button>
      <div className="kind">{c.kind}{c.existing ? " · existing" : ""}</div>
      <div className="name">{c.name}</div>
      {c.responsibility && <div className="resp">{c.responsibility}</div>}
      <div className="row">
        {c.tech && <span className="pill">{c.tech}</span>}
        {facet && (
          <button className="pill facet" onClick={(e) => { e.stopPropagation(); open(c.id); }}>
            ⤢ {facet}
          </button>
        )}
        {gaps.length > 0 && <span className="pill thin">thin · {gaps.length}</span>}
        {worst && (
          <span className="pill" title={worst.claim}>
            <span className={`severity-dot ${worst.severity}`} /> {concerns.length}
          </span>
        )}
      </div>
    </div>
  );
}

export interface SketchNodeData extends Record<string, unknown> {
  node: SketchNode;
  changed: boolean;
  concerns: Concern[];
}

export function SketchNodeCard({ data, selected }: NodeProps) {
  const { node, changed, concerns } = data as SketchNodeData;
  const worst = concerns.find((x) => x.severity === "blocker") ?? concerns[0];
  return (
    <div
      className="node sketch"
      data-selected={selected}
      data-changed={changed}
      title={node.detail || node.note || undefined}
    >
      <Anchors />
      {node.kind && node.kind !== "component" && <div className="kind">{node.kind}</div>}
      <div className="name">{node.label || node.id}</div>
      {node.note && <div className="resp">{node.note}</div>}
      <div className="row">
        {node.depth !== "stub" && <span className="depth">◆ {node.depth}</span>}
        {worst && <span className={`severity-dot ${worst.severity}`} title={worst.claim} />}
      </div>
    </div>
  );
}

export const nodeTypes = { component: ComponentNode, sketch: SketchNodeCard };
