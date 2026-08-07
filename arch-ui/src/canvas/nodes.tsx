/**
 * Node cards. A node *is* the contract sheet — that is the whole reason for
 * rendering from structured state rather than a diagram image.
 */
import { useEffect, useState } from "react";
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

/**
 * The `data-in` flip, §3.
 *
 * Starts false so the element is painted once in its "from" state, then flips
 * true on the next frame and the CSS transition covers the gap. Two rAFs, not
 * one: a single frame is not reliably enough for the browser to have committed
 * the first paint, and if it has not, the flip coalesces into the initial
 * render and no transition runs at all.
 *
 * `born` short-circuits it — something already on screen when the page loaded
 * never had an arrival to animate.
 */
function useEntrance(born: boolean): boolean {
  const [inn, setInn] = useState(born);
  useEffect(() => {
    if (born) return;
    let raf2 = 0;
    const raf1 = requestAnimationFrame(() => {
      raf2 = requestAnimationFrame(() => setInn(true));
    });
    return () => { cancelAnimationFrame(raf1); cancelAnimationFrame(raf2); };
  }, [born]);
  return inn;
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
  /** Open questions targeting this component — asked and still unanswered. */
  questions: string[];
  owes: boolean;
  changed: boolean;
  /** True while another flow is hovered or playing and this card is not on it. */
  dim: boolean;
  /** True while this card is on the flow being hovered or played (§7). */
  lit: boolean;
  /** Already on screen when the page loaded — no arrival to animate (§3). */
  born: boolean;
}

/**
 * The card, handover §6.
 *
 * Three strips: a coloured band carrying the kind and the open affordance, a
 * body carrying the name and what the thing is for, and a footer carrying
 * everything that is *about* the card rather than in it — tech, facet size,
 * thinness, unanswered questions, objections.
 *
 * The split matters more than it looks. Before this, all of that sat in one
 * undifferentiated column and the kind — the first thing anyone reads a
 * diagram by — was the quietest line on it.
 */
export function ComponentNode({ data, selected }: NodeProps) {
  const { component: c, gaps, concerns, questions, owes, changed, dim, lit, born } =
    data as ComponentNodeData;
  const inn = useEntrance(born);
  const open = useCanvas((s) => s.openComponentDialog);
  const facet = facetSummary(c);
  const worst = concerns.find((x) => x.severity === "blocker") ?? concerns[0];
  const title = [
    gaps.length ? `Thin:\n· ${gaps.join("\n· ")}` : "",
    concerns.length ? `Concerns:\n· ${concerns.map((x) => `[${x.severity}] ${x.claim}`).join("\n· ")}` : "",
    questions.length ? `Asked, unanswered:\n· ${questions.join("\n· ")}` : "",
  ].filter(Boolean).join("\n\n");

  return (
    <div
      className="node"
      data-kind={c.kind}
      data-selected={selected}
      data-changed={changed}
      data-existing={c.existing}
      data-dim={dim}
      data-lit={lit}
      data-in={inn}
      title={title || undefined}
    >
      <Anchors />
      <div className="hb">
        <span>{c.kind}{c.existing ? " · existing" : ""}</span>
        <span className="spacer" />
        {owes && <span className="owe-dot" title="owes a facet" />}
        {/* internals open in their own dialog — never by growing the card,
            which would shove neighbours the user placed by hand */}
        {!c.existing && (
          <button
            className="open-btn"
            title="open its internals (E)"
            onClick={(e) => { e.stopPropagation(); open(c.id); }}
          >
            ⤢
          </button>
        )}
      </div>

      <div className="bd">
        <div className="name">{c.name}</div>
        {c.responsibility && <div className="resp">{c.responsibility}</div>}
      </div>

      <div className="ft">
        {c.tech && <span className="pill">{c.tech}</span>}
        {facet && (
          <button className="pill facet" onClick={(e) => { e.stopPropagation(); open(c.id); }}>
            ⤢ {facet}
          </button>
        )}
        {gaps.length > 0 && <span className="pill thin">thin · {gaps.length}</span>}
        {questions.length > 0 && (
          <span className="pill asked" title={questions.join("\n")}>asked · {questions.length}</span>
        )}
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
  born?: boolean;
}

export function SketchNodeCard({ data, selected }: NodeProps) {
  const { node, changed, concerns, born } = data as SketchNodeData;
  const inn = useEntrance(born ?? true);
  const worst = concerns.find((x) => x.severity === "blocker") ?? concerns[0];
  return (
    <div
      className="node sketch"
      data-selected={selected}
      data-changed={changed}
      data-in={inn}
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
