import { useEffect, useMemo, useState } from "react";
import { Canvas, useTidyUp } from "./canvas/Canvas";
import { ComponentDialog } from "./dialog/ComponentDialog";
import { Rail } from "./rail/Rail";
import { useCanvas } from "./store/canvas";
import { promoteVariant, useSession } from "./store/session";
import { useTheme } from "./theme";
import type { Layer, Variant } from "./types";

/** `E` opens the selected component's internals — the one keyboard shortcut
 *  the design handover asks for, and the reason a node click only selects. */
function useOpenShortcut(): void {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "e" && e.key !== "E") return;
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      const el = e.target as HTMLElement | null;
      if (el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.isContentEditable)) return;
      const { selected, openComponentDialog } = useCanvas.getState();
      if (selected?.startsWith("design:")) openComponentDialog(selected.slice("design:".length));
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
}

/**
 * Which layer to draw. The user's choice wins; otherwise follow the session —
 * the design once something is promoted, the sketch before that. Both layers
 * stay reachable at all times, because the harness keeps both live.
 */
function useLayer(): { layer: Layer; variant: Variant | null; variants: Variant[] } {
  const arch = useSession((s) => s.arch);
  const chosenLayer = useCanvas((s) => s.layer);
  const chosenVariant = useCanvas((s) => s.variant);

  return useMemo(() => {
    const book = arch?.sketchbook;
    const variants = Object.values(book?.variants ?? {});
    const hasDesign = Object.keys(arch?.components ?? {}).length > 0;
    const hasSketch = variants.some((v) => Object.keys(v.nodes).length > 0);

    let layer: Layer = chosenLayer ?? (hasDesign ? "design" : "sketch");
    if (layer === "sketch" && !hasSketch && hasDesign) layer = "design";
    if (layer === "design" && !hasDesign && hasSketch) layer = "sketch";

    const wanted =
      variants.find((v) => v.id === chosenVariant) ??
      variants.find((v) => v.id === book?.active) ??
      variants.find((v) => v.status === "chosen") ??
      variants.find((v) => Object.keys(v.nodes).length > 0) ??
      null;

    return { layer, variant: wanted, variants };
  }, [arch, chosenLayer, chosenVariant]);
}

function TopBar({ layer, variants }: { layer: Layer; variants: Variant[] }) {
  const arch = useSession((s) => s.arch);
  const ready = useSession((s) => s.ready);
  const conn = useSession((s) => s.conn);
  const finalized = useSession((s) => s.finalized);
  const transcript = useSession((s) => s.transcript);
  const setLayer = useCanvas((s) => s.setLayer);
  const theme = useTheme((s) => s.theme);
  const toggleTheme = useTheme((s) => s.toggle);
  const { layer: current, variant } = useLayer();
  const tidy = useTidyUp(current, variant);

  const goal =
    arch?.brief.goal ||
    transcript.find((t) => t.t === "user")?.text ||
    "Architecture session";
  const repo = ready?.repo ? ready.repo.split("/").slice(-2).join("/") : "—";
  const hasDesign = Object.keys(arch?.components ?? {}).length > 0;
  const hasSketch = variants.some((v) => Object.keys(v.nodes).length > 0);
  const connLabel =
    conn === "complete" ? "Session complete" :
    conn === "connected" ? "Live" :
    conn === "connecting" ? "Connecting…" : "Disconnected";

  return (
    <header className="topbar">
      <span className="goal" title={goal}>{goal}</span>

      {(hasSketch || hasDesign) && (
        <div className="segmented" role="group" aria-label="canvas layer">
          <button data-on={layer === "sketch"} onClick={() => setLayer("sketch")} disabled={!hasSketch}>
            Sketch
          </button>
          <button data-on={layer === "design"} onClick={() => setLayer("design")} disabled={!hasDesign}>
            Design
          </button>
        </div>
      )}

      <div className="meta">
        {arch && <span className="chip">{arch.phase}</span>}
        <button
          className="ghost theme-toggle"
          onClick={toggleTheme}
          aria-label={theme === "dark" ? "switch to light" : "switch to dark"}
          title={theme === "dark" ? "light mode" : "dark mode"}
        >
          {theme === "dark" ? "☀" : "☾"}
        </button>
        {!finalized && <button className="ghost" onClick={tidy}>Tidy up</button>}
        {finalized && <span className="chip">read-only</span>}
        <span className="mono">{ready?.model ?? "—"}</span>
        <span className="mono" title={ready?.repo}>{repo}</span>
        <span><span className={`dot ${conn}`} /> {connLabel}</span>
      </div>
    </header>
  );
}

/**
 * Taking a sketch forward without asking the architect to do it.
 *
 * Promoting over a shape that is already seeded throws that shape away, so
 * that case asks a second time — inline, because a browser modal would freeze
 * the page's own event stream.
 */
function PromoteVariant({ variant }: { variant: Variant }) {
  const arch = useSession((s) => s.arch);
  const finalized = useSession((s) => s.finalized);
  const conn = useSession((s) => s.conn);
  const [armed, setArmed] = useState(false);
  const [busy, setBusy] = useState(false);

  if (!arch || finalized || conn === "disconnected") return null;
  if (Object.keys(variant.nodes).length === 0) return null;

  const seededElsewhere = Object.values(arch.components).some(
    (c) => c.origin.startsWith("sketch:") && !c.origin.startsWith(`sketch:${variant.id}:`),
  );
  const alreadyMine =
    variant.status === "chosen" &&
    Object.values(arch.components).some((c) => c.origin.startsWith(`sketch:${variant.id}:`));

  const go = async () => {
    if (seededElsewhere && !armed) { setArmed(true); return; }
    setBusy(true);
    await promoteVariant(variant.id, seededElsewhere);
    setBusy(false);
    setArmed(false);
  };

  return (
    <button className="variant-tab promote" disabled={busy} onClick={go}
            title="seed the design from this sketch — the same thing `promote` does">
      {busy ? "promoting…"
        : armed ? "replace the current design?"
        : alreadyMine ? "re-promote"
        : seededElsewhere ? "switch to this shape"
        : "use this shape"}
    </button>
  );
}

function VariantTabs({ variants, activeId }: { variants: Variant[]; activeId: string | null }) {
  const setVariant = useCanvas((s) => s.setVariant);
  const active = variants.find((v) => v.id === activeId);
  if (variants.length === 0) return null;
  return (
    <div className="variant-tabs">
      {variants.map((v) => (
        <button
          key={v.id}
          className="variant-tab"
          data-on={v.id === activeId}
          data-status={v.status}
          title={v.rejected_reason ? `not taken: ${v.rejected_reason}` : v.summary}
          onClick={() => setVariant(v.id)}
        >
          {v.status === "chosen" ? "✓ " : ""}{v.name}
          <span className="count faint"> {Object.keys(v.nodes).length}n</span>
        </button>
      ))}
      {active && <PromoteVariant variant={active} />}
    </div>
  );
}

function EmptyState({ layer }: { layer: Layer }) {
  return (
    <div className="empty-state">
      <div>
        <h3>{layer === "sketch" ? "Nothing sketched yet" : "Nothing promoted yet"}</h3>
        <p>
          {layer === "sketch"
            ? "The architect opens with a rough shape you can react to — boxes will appear here as it sketches."
            : "The sketch becomes a design when a shape is promoted. Until then, the Sketch layer is where the thinking is."}
        </p>
      </div>
    </div>
  );
}

export default function App() {
  const conn = useSession((s) => s.conn);
  const arch = useSession((s) => s.arch);
  const runId = useSession((s) => s.ready?.run_id);
  const restore = useCanvas((s) => s.restore);
  const { layer, variant, variants } = useLayer();
  useOpenShortcut();

  // the overlay is per run id, so a refresh lands back on the same viewport
  useEffect(() => {
    if (runId) restore(runId);
  }, [runId, restore]);

  const count =
    layer === "sketch"
      ? Object.keys(variant?.nodes ?? {}).length
      : Object.keys(arch?.components ?? {}).length;

  return (
    <div className="app">
      <TopBar layer={layer} variants={variants} />

      <main className="stage">
        <Canvas layer={layer} variant={variant} />

        <div className="canvas-overlay tl">
          {layer === "sketch" ? (
            <VariantTabs variants={variants} activeId={variant?.id ?? null} />
          ) : (
            <span className="chip">
              {count} component{count === 1 ? "" : "s"}
            </span>
          )}
        </div>

        <div className="canvas-overlay bl">
          <span className="hint-strip">
            drag to place · a placed node stays put · <b>E</b> or ⤢ opens a component · Tidy up to re-flow
          </span>
        </div>

        {count === 0 && conn !== "disconnected" && <EmptyState layer={layer} />}

        {conn === "connecting" && !arch && (
          <div className="veil">
            <div className="box">
              <h3>Connecting…</h3>
              <p>The first turn is already running on the server.</p>
            </div>
          </div>
        )}

        {conn === "disconnected" && (
          <div className="veil">
            <div className="box">
              <h3>Disconnected</h3>
              <p>
                The session ended or the server went away. The canvas is still here to read —
                pan and zoom still work.
              </p>
            </div>
          </div>
        )}

        {/* over the canvas, never in it: opening a component must not reflow
            the system graph behind it */}
        <ComponentDialog />
      </main>

      <Rail />
    </div>
  );
}
