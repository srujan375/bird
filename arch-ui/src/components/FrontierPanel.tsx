import { useState } from "react";
import { refusal, closeBranch, useSession } from "../wire/session";

/**
 * What is still open, and how to close a branch on purpose.
 *
 * The harness walks the design tree and this is the frontier it walks: the
 * fork if one is open, the boxes worth asking about next, how many more
 * there are, and what the user closed. Nobody in a plain interview can see
 * how much tree is left, which is how sessions run to two hundred
 * questions; here the user can, and can say "good enough" or "not this
 * session" on a box — the one thing that ends a branch without the
 * architect's say-so. It is a list, not a checklist: the gestures show on
 * the row you point at and nowhere else.
 */
export function FrontierPanel() {
  const { arch, frontier, handedOff } = useSession();
  const [hover, setHover] = useState<string | null>(null);
  if (!arch || !frontier || !Object.keys(arch.nodes).length || handedOff) return null;
  const { fork, open, more, closed } = frontier;

  const act = async (op: "settle" | "out_of_scope" | "reopen", id: string) => {
    const err = await closeBranch(op, id);
    if (err) refusal(err);
  };

  return (
    <aside className="frontier" aria-label="What is still open" data-od-id="frontier">
      <div className="fr-head">
        <span><b>Open</b> · {fork ? "the fork" : open.length}</span>
        {more > 0 ? <span className="fr-more">+{more} more</span> : null}
      </div>
      {fork ? (
        <div className="fr-fork">
          <span className="k">still open</span>
          {fork.approaches.map((a, i) => (
            <span key={a}>{i > 0 ? " vs " : ""}<b>{a}</b></span>
          ))}
          <br />Everything downstream waits on which one wins.
        </div>
      ) : null}
      {open.map((row) => (
        <div
          key={row.id}
          className="fr-row"
          {...(hover === row.id ? { "data-hover": "1" } : {})}
          onPointerEnter={() => setHover(row.id)}
          onPointerLeave={() => setHover((h) => (h === row.id ? null : h))}
          data-od-id={"frontier-" + row.id}
        >
          <span className="fr-label">{row.label}</span>
          <span className="node-kind mono">{row.kind}</span>
          <span className="fr-why">{row.why}</span>
          {hover === row.id ? (
            <span className="fr-acts">
              <button type="button" className="link-btn" onClick={() => void act("settle", row.id)}>Good enough</button>
              <button type="button" className="link-btn" onClick={() => void act("out_of_scope", row.id)}>Out of scope</button>
            </span>
          ) : null}
        </div>
      ))}
      {!fork && !open.length ? (
        <p className="fr-empty">Nothing left unelaborated. Say you’re done, or find the thing you both skipped.</p>
      ) : null}
      {closed.length ? (
        <div className="fr-closed">
          {closed.map((c) => (
            <div key={c.id} className="fr-row fr-row-closed">
              <span className="fr-label">{c.label}</span>
              <span className="node-mark">{c.how === "settled" ? "good enough" : "out of scope"}</span>
              <span className="fr-acts"><button type="button" className="link-btn" onClick={() => void act("reopen", c.id)}>Reopen</button></span>
            </div>
          ))}
        </div>
      ) : null}
    </aside>
  );
}
