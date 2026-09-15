/**
 * Dev-only: a scripted design session with no harness behind it.
 *
 *   npm run dev  →  http://localhost:5173/?fixture=demo
 *                   …&pace=60 slows the typing (ms per fragment; default 14)
 *
 * It plays what a real session sends — the intake already answered, a turn
 * that writes three artboards as tool-call fragments (the live build), the
 * state pushes that land them, then a second turn that rebuilds one — with
 * the timing of a model typing. Nothing here ships: `main.tsx` only reaches
 * for this module under `import.meta.env.DEV`.
 */
import BRIDGE from "../../../src/bird/harnesses/design/editor-bridge.js?raw";
import { resetDrafts } from "../wire/drafts";
import { slug } from "../wire/partial";
import { applyEvent } from "../wire/session";
import type { DesignStateEvent, Incoming, WireArtboard } from "../wire/types";
import { DARK, DARK_WARMER, EPISODE, WARM } from "./samples";

const wait = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));
const PACE = Math.max(0, Number(new URLSearchParams(location.search).get("pace")) || 14);
const step = (ev: Incoming) => applyEvent(ev);

/** The bridge rides inside every stored version, as write_version does it. */
const wrap = (html: string) =>
  html.replace(/<\/body\s*>/i, `<script data-bird-editor>${BRIDGE}</script></body>`);

const TOKENS = `:root{--accent:#5e6ad2;--bg:#fff;--fg:#0f0f10;--muted:#6b6f76;--font-display:-apple-system,BlinkMacSystemFont,"SF Pro Display",system-ui,sans-serif;}`;

const PROMPT = "A landing page for a podcast about building software in the open.";

const intake = [
  {
    type: "picker" as const, variant: "option-cards" as const, id: "fidelity",
    prompt: "Should I design this as a wireframe or high-fidelity?", hint: "Pick one",
    summaryLabel: "Direction", confirm: { template: "Design in {v}", empty: "Pick a direction" },
    options: [
      { value: "wireframe", label: "Wireframe", description: "Greyscale structure. Fast to iterate." },
      { value: "high-fidelity", label: "High-fidelity", description: "Real theme, colour and type.", rec: true },
    ],
    answered: true, value: "high-fidelity", label: "High-fidelity",
  },
  {
    type: "picker" as const, variant: "option-cards" as const, id: "design-system",
    prompt: "Which design system should I work in?", hint: "Pick one",
    summaryLabel: "Design system", confirm: { template: "Design in {v}", empty: "Pick a design system" },
    options: [
      { value: "linear-app", label: "Linear", description: "Project management. Ultra-minimal, precise, purple accent." },
      { value: "notion", label: "Notion", description: "Docs. Warm neutrals, serif headings." },
      { value: "editorial", label: "Editorial", description: "Magazine. Big type, generous whitespace." },
      { value: "designer", label: "You choose", description: "I'll match the brief, and show you the rest.", rec: true },
    ],
    answered: true, value: "linear-app", label: "Linear",
  },
];

function push(status: DesignStateEvent["status"], artboards: WireArtboard[], html: Record<string, string>,
              extra: Partial<DesignStateEvent> = {}) {
  step({
    type: "design_state", status, changed: null, prompt: PROMPT, theme: "linear-app", theme_css: TOKENS,
    themes: ["editorial", "linear-app", "notion", "wireframe"], artboards, html,
    ask: null, intake, intake_locked: true, ...extra,
  });
}

async function stream(text: string) {
  for (const word of text.split(/(?<= )/)) {
    step({ type: "harness_event", event: "assistant_delta", data: { text: word } });
    await wait(28);
  }
}

/** A tool call's arguments, as the wire delivers them: the name on the first
 *  fragment only, then pieces of the arguments_json. */
async function writeCall(index: number, args: Record<string, string>, pace = PACE) {
  const json = JSON.stringify(args);
  let first = true;
  for (let i = 0; i < json.length; i += 56) {
    step({ type: "harness_event", event: "tool_call_delta",
           data: { index, name: first ? "design_create" : "", text: json.slice(i, i + 56) } as never });
    first = false;
    await wait(pace);
  }
}

const version = (id: string, kind: string, note: string) => ({ id, kind, note });

export async function loadFixture(name: string): Promise<void> {
  resetDrafts();
  step({ type: "ready", model: "fixture:" + name, kg: false, kg_ready: false, run_id: "demo", repo: "fixture", skills: [] });
  push("generating", [], {}, { replayed: true });
  await wait(700);

  /* act one: the brief goes out and three artboards are written live */
  step({ type: "harness_event", event: "run_start",
         data: { task: `Direction: High-fidelity. Design system: linear-app. Brief: ${PROMPT}` } });
  await wait(600);
  /* the plan goes first; its critique comes back as a card in the thread */
  step({ type: "harness_event", event: "tool_call_delta",
         data: { index: 0, name: "design_plan", text: '{"plan":"' } as never });
  await wait(900);
  step({ type: "harness_event", event: "assistant", data: {
    content: "",
    tool_calls: [{ name: "design_plan", arguments_json: JSON.stringify({ plan: "Direction: dark editorial hero, one accent." }) }],
  } });
  await wait(700);
  step({ type: "harness_event", event: "tool_result", data: { name: "design_plan", is_error: false, details: {
    ok: true, plan: "stored",
    critique: "- **Palette use.** Name the surface steps: base, raised, sunken — the plan says \"dark\" and nothing else.\n- **Type scale.** Give the hero a number; \"big\" is not a size.\n- **Signature.** One element the page is remembered by. There is none yet.",
  } } });
  await wait(400);
  await stream("Two directions for the hero that differ where it matters — one editorial and dark, one warm and slow — plus the episode page the hero hands off to. ");
  await wait(300);
  const calls = [
    { title: "Hero — dark", html: DARK },
    { title: "Hero — warm", html: WARM },
    { title: "Episode", html: EPISODE },
  ];
  for (let i = 0; i < calls.length; i++) await writeCall(i, calls[i]);
  step({ type: "harness_event", event: "assistant", data: {
    content: "Two directions for the hero that differ where it matters — one editorial and dark, one warm and slow — plus the episode page the hero hands off to.",
    tool_calls: calls.map((c) => ({ name: "design_create", arguments_json: JSON.stringify(c) })),
  } });
  const boards: WireArtboard[] = [];
  const html: Record<string, string> = {};
  const critiques: Record<string, { version: string; kind: string; text: string }> = {};
  for (const c of calls) {
    await wait(500);
    const id = slug(c.title);
    boards.push({ id, title: c.title, current: "v1", versions: [version("v1", "generated", "generated")], finalized: false });
    html[id] = wrap(c.html);
    push("ready", [...boards], { ...html }, { selected: boards[0].id, changed: { kind: "artboard", id }, critiques: { ...critiques } });
    /* the critic's look: the capture is asked for on the same push, waits
       for the frame to load, and the note lands on the frame */
    step({ type: "capture_request", id: "cap-" + id, artboard: id, deadline: 8 });
    await wait(1400);
    const note = `- **Hero CTA.** Two buttons of equal weight in \`${id}\` — make one primary.\n- **Rhythm.** The episode rows and the hero share a left edge; offset one.`;
    critiques[id] = { version: "v1", kind: "render", text: note };
    push("ready", [...boards], { ...html }, { selected: boards[0].id, changed: null, critiques: { ...critiques } });
    step({ type: "harness_event", event: "tool_result",
           data: { name: "design_create", is_error: false, details: { ok: true, artboard: id, version: "v1", critique: note } } });
  }
  await wait(400);
  await stream("Say which direction reads right and I'll refine it — or click straight into an element on the board to edit it yourself.");
  step({ type: "harness_event", event: "assistant", data: {
    content: "Say which direction reads right and I'll refine it — or click straight into an element on the board to edit it yourself.",
  } });
  step({ type: "turn_end", status: "reply" });

  if (name === "demo-still") return;

  /* two hand edits the designer has not seen yet: the composer offers to
     send them on their own */
  await wait(1500);
  push("ready", [...boards], { ...html }, { selected: boards[0].id, changed: null, critiques: { ...critiques }, pending_user_edits: 2 });

  /* act two: a rebuild, drawn in the artboard's own slot */
  await wait(3000);
  push("ready", [...boards], { ...html }, { selected: boards[0].id, changed: null, critiques: { ...critiques }, pending_user_edits: 0 });
  step({ type: "harness_event", event: "run_start", data: { task: "Warmer type on the dark one — keep the palette." } });
  await wait(700);
  await stream("Rewriting **Hero — dark** with a serif headline — you'll see it change on the board as I go. ");
  await writeCall(0, { artboard: "hero-dark", html: DARK_WARMER });
  step({ type: "harness_event", event: "assistant", data: {
    content: "Rewriting **Hero — dark** with a serif headline — you'll see it change on the board as I go.",
    tool_calls: [{ name: "design_create", arguments_json: JSON.stringify({ artboard: "hero-dark", html: DARK_WARMER }) }],
  } });
  await wait(500);
  boards[0] = { ...boards[0], current: "v2", versions: [...boards[0].versions, version("v2", "refined", "rebuilt")] };
  html["hero-dark"] = wrap(DARK_WARMER);
  push("ready", [...boards], { ...html }, { selected: "hero-dark", changed: { kind: "artboard", id: "hero-dark" } });
  step({ type: "harness_event", event: "tool_result",
         data: { name: "design_create", is_error: false, details: { ok: true, artboard: "hero-dark", version: "v2" } } });
  await wait(300);
  await stream("Done — the headline is Georgia at 66px now, everything else as it was. Undo takes it back if it reads too soft.");
  step({ type: "harness_event", event: "assistant", data: {
    content: "Done — the headline is Georgia at 66px now, everything else as it was. Undo takes it back if it reads too soft.",
  } });
  step({ type: "turn_end", status: "reply" });
}
