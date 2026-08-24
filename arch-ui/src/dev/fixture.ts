/**
 * Dev-only: render a saved `arch_state.json` without a live harness.
 *
 *   npm run dev  →  http://localhost:5173/?fixture=mcp
 *
 * Drop any harness `arch_state.json` into `arch-ui/dev-fixtures/` (gitignored)
 * and name it in the query string. Nothing here ships — `main.tsx` only reaches
 * for this module under `import.meta.env.DEV`.
 */
import { applyEvent } from "../wire/session";
import type { ArchState } from "../wire/types";

export async function loadFixture(name: string): Promise<void> {
  const state = (await fetch(`/dev-fixtures/${name}.json`).then((r) => r.json())) as ArchState;
  applyEvent({
    type: "ready",
    model: "fixture:" + name,
    kg: false,
    kg_ready: false,
    run_id: name,
    repo: "fixture",
    skills: [],
  });
  applyEvent({
    type: "arch_state",
    status: state.handed_off ? "handed_off" : "open",
    state,
    renders: { board: "" },
    noticing: [],
    changed: null,
    replayed: true,
  });
}
