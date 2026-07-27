/**
 * The cards that live inside a component dialog.
 *
 * Same principle as the system canvas: a card *is* the record — an entity card
 * shows its real keys and indexes, a message card its real delivery claim — so
 * there is nothing to click through to. Handles are present but invisible;
 * edges attach to them where a sub-diagram genuinely has edges to draw.
 */
import { Handle, Position, type NodeProps } from "@xyflow/react";
import type { DeployUnit, Entity, LlmTask, Module, QueueMessage } from "../types";

function Ports() {
  return (
    <>
      <Handle type="target" position={Position.Left} />
      <Handle type="source" position={Position.Right} />
    </>
  );
}

export function EntityNode({ data, selected }: NodeProps) {
  const e = data.entity as Entity;
  return (
    <div className="dnode entity" data-selected={selected}>
      <Ports />
      <div className="dnode-head">
        <span className="name">{e.name}</span>
        {e.keys && <span className="pill mono">key {e.keys}</span>}
      </div>
      {e.fields.length > 0 ? (
        <ul className="fields mono">
          {e.fields.map((f) => <li key={f}>{f}</li>)}
        </ul>
      ) : (
        <div className="faint tiny">no fields recorded</div>
      )}
      {e.indexes.length > 0 && (
        <div className="dnode-foot">
          {e.indexes.map((i) => <span key={i} className="pill mono">idx {i}</span>)}
        </div>
      )}
    </div>
  );
}

export function ModuleNode({ data, selected }: NodeProps) {
  const m = data.module as Module;
  return (
    <div className="dnode module" data-selected={selected}>
      <Ports />
      <div className="dnode-head"><span className="name">{m.name}</span></div>
      <p>{m.purpose || <span className="faint">no purpose recorded</span>}</p>
    </div>
  );
}

export function MessageNode({ data, selected }: NodeProps) {
  const m = data.message as QueueMessage;
  return (
    <div className="dnode message" data-selected={selected}>
      <Ports />
      <div className="dnode-head">
        <span className="name">{m.name}</span>
        {m.delivery && <span className="pill mono">{m.delivery}</span>}
      </div>
      {m.schema && <pre className="mono schema">{m.schema}</pre>}
      {m.ordering && <div className="tiny faint">ordering: {m.ordering}</div>}
    </div>
  );
}

export function DlqNode({ data, selected }: NodeProps) {
  return (
    <div className="dnode dlq" data-selected={selected}>
      <Ports />
      <div className="dnode-head"><span className="name">dead letters</span></div>
      <p>{String(data.policy || "")}</p>
    </div>
  );
}

export function UnitNode({ data, selected }: NodeProps) {
  const u = data.unit as DeployUnit;
  return (
    <div className="dnode unit" data-selected={selected}>
      <Ports />
      <div className="dnode-head">
        <span className="name">{u.name}</span>
        {u.region && <span className="pill mono">{u.region}</span>}
      </div>
      <div className="hosts">
        {u.components.length > 0
          ? u.components.map((c) => <span key={c} className="pill mono host">{c}</span>)
          : <span className="faint tiny">hosts nothing yet</span>}
      </div>
      {u.scaling_policy && <div className="tiny faint">scales: {u.scaling_policy}</div>}
    </div>
  );
}

/** One link in an llm task chain: prompt → context → guardrails → fallback. */
export function StepNode({ data, selected }: NodeProps) {
  return (
    <div className="dnode step" data-selected={selected}>
      <Ports />
      <div className="dnode-head"><span className="step-label">{String(data.label)}</span></div>
      <p>{String(data.text || "") || <span className="faint">not recorded</span>}</p>
    </div>
  );
}

/** A task's own header card — model tier, cost envelope, eval hook. */
export function TaskNode({ data, selected }: NodeProps) {
  const t = data.task as LlmTask;
  return (
    <div className="dnode task" data-selected={selected}>
      <Ports />
      <div className="dnode-head"><span className="name">{t.name}</span></div>
      <div className="dnode-foot">
        {t.model_tier && <span className="pill mono">{t.model_tier}</span>}
        {t.cost_envelope && <span className="pill mono">{t.cost_envelope}</span>}
        {t.eval_hook && <span className="pill mono">eval: {t.eval_hook}</span>}
      </div>
    </div>
  );
}

export const dialogNodeTypes = {
  entity: EntityNode,
  module: ModuleNode,
  message: MessageNode,
  dlq: DlqNode,
  unit: UnitNode,
  step: StepNode,
  task: TaskNode,
};
