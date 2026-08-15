# bird: a multi-harness coding agent

A coding agent built for **small open models**. Instead of one giant prompt and a
grep loop, `bird` gives the model a **knowledge graph of your repo** to ask
questions of, and splits the work across **specialized harnesses**: a
conversational lead, an architect that designs on a live canvas, and a coder that
edits the repo, all running on one shared engine.

It runs against local Ollama models, Ollama Cloud, or anything on OpenRouter, and
you can swap the model mid-conversation without losing the conversation.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/multi-harness-dark.png">
  <img alt="bird: lead talks, routes and dispatches to arch (designs the system on a live canvas) and code (edits, tests, verifies the repo); arch hands a bundle to code" src="assets/multi-harness-light.png" width="900">
</picture>

*one engine · one toolbox · knowledge graph · permissions · skills*

---

## Why we built it

**Small models don't fail at writing code. They fail at finding it.**

Give a 9-20B model a repo and a `grep` tool and it will spend most of its context
window wandering: search, read the wrong file, search again, read a 600-line
module for one function. By the time it knows where to make the change, the
window is full and the plan is gone. The bottleneck isn't reasoning. It's
*context acquisition*.

So the bet behind `bird` is: **stop making the model explore, and answer it
instead.**

- **A knowledge graph, not a grep loop.** The repo is extracted into a graph
  (`graphify`) using pure AST, with no LLM and no API keys. `kg_query` answers
  "where is login handled?" with the node, the file, the line, and the
  neighbourhood around it. One call instead of nine.
- **Deterministic where it can be.** Query expansion is tokenize → split
  camelCase → singularize → fuzzy-match against the graph's own vocabulary →
  IDF-rank. No LLM in the retrieval path, so it's reproducible, free, and works
  offline. A miss returns the nearest real vocabulary terms so a small model can
  correct itself.
- **Provable, not asserted.** Every run can be repeated with `--no-kg`, a
  control arm with the graph tool removed and nothing else changed. If the
  context engine doesn't help, the numbers say so.
- **The engine does the babysitting.** Small models loop, restate plans, respond
  with prose when a tool call was needed, and forget to stop. Those are engine
  features, not prompt pleading: validation retries with helpful errors,
  same-call loop detection, explore-without-acting nudges, a pinned plan tracker,
  automatic compaction at 90% of the window, and an explicit `done` tool.
- **One mode per job.** Designing a system, deciding what to build, and editing
  files are different jobs with different tools and different failure modes.
  Cramming them into one prompt makes all three worse, so each is a *harness*
  with its own instructions, toolset and engine tuning, sharing one runner.
- **Nothing edits your repo without you.** Every mutating tool is gated at
  construction time, so gating covers the CLI, the TUI, *and* any sub-session a
  harness dispatches. `bash` is allowlisted to read-only search, tests, linters
  and git reads. Anything else is refused loudly and logged.

---

## Install

```bash
git clone <this repo> && cd bird
python3 -m venv .venv && .venv/bin/pip3 install -e '.[dev]'
```

That gives you `.venv/bin/bird`, which runs from any directory without
activating anything, because its shebang is absolute. To get `bird` on your `PATH`
everywhere, either symlink it (`ln -s "$PWD/.venv/bin/bird" ~/.local/bin/bird`)
or install with [pipx](https://pipx.pypa.io), which manages the venv for you:

```bash
pipx install .              # from a clone
pipx install git+<this repo>
```

Don't `pip3 install` without a venv: on Homebrew or system Python that fails
with `externally-managed-environment` (PEP 668), and `--break-system-packages`
"fixes" it by writing bird's dependencies into an interpreter your OS owns.

Requires Python 3.11+, plus at least one model source:

| Source | Setup |
|---|---|
| **Ollama Cloud** *(what the shipped `models.json` points at)* | `OLLAMA_API_KEY` |
| **OpenRouter** | `OPENROUTER_API_KEY` |
| **Local Ollama** | `ollama serve`, then set the `ollama` provider's `native_url` to `http://localhost:11434` in `models.json`. Missing models are pulled on demand |

Put keys in a `.env` at the repo root. It's loaded automatically.

Optional frontends (both work without them, both are one `npm install`):

```bash
cd tui      && npm install    # full-screen terminal UI
cd arch-ui  && npm install    # only needed to rebuild the architecture canvas
```

---

## Quick start

```bash
cd /path/to/your/project

bird                                       # talk to the lead, the front door
bird lead "add rate limiting to the API"   # one-shot: it routes and builds
bird code "fix the failing test in tests/test_pricing.py"
bird arch "design a job queue for this service"   # opens the canvas
```

The first run builds the knowledge graph **in the background**, so the harness
starts immediately. Until the graph is ready, `kg_query` tells the model to fallyy
back to bash search, and the runner injects a notice the moment the graph comes
online.

---

## The harnesses

Three ship today; two more are in progress.

### `lead`, the front door *(bare `bird`)*

The conversational layer. It answers questions, explores the repo, researches the
web, and decides each turn whether to reply or to **dispatch**.

It deliberately has **no edit, write, or bash tools**. Every code change routes
through a dispatched `code` session, so "the agent quietly changed something
while answering a question" is structurally impossible.

| The ask | What it does |
|---|---|
| A question, explanation, or exploration | reads / queries / researches, then answers in plain text |
| A new feature or non-trivial structural work | `architect` → (user approves) → `code` |
| A localized change or bug fix | `code` directly |

### `arch`, architecture on a live canvas *(`bird arch`)*

Opens a browser page and designs *with* you. The model mutates typed state; the
page renders it. The conversation is the work; the tools are the memory.

- **Two layers, both always open.** A loose **sketch** layer (`variant`, `node`,
  `link`, `splice`, `depth`) for napkin thinking, and a **design** layer
  (`component`, `connect`, `flow`, `decide`, `expand`) for the thing that gets
  built. `promote` seeds one from the other; going back to sketching afterwards
  is normal.
- **Disagreement is first-class.** `concern` records an objection with a
  severity, against the design, against a decision, or against *your*
  instruction. Open blockers are shown at the finalize gate instead of silently
  blocking work.
- **A background critic.** A second model reviews the design on its own thread
  each turn and files concerns of its own. The architect answers them, acts on
  them, or overrules them with a reason.
- **Nothing is phase-locked.** Tools refuse only what is *broken* (an edge to a
  component that doesn't exist). Thin design comes back as advice, not a refusal.
  Post-approval structural edits record an amendment, because the audit trail
  was the part worth keeping, not the lock.
- **Two human gates, and only two.** Top-level approval, then finalize. `done` is
  those gates and nothing else.
- **A real handoff.** Finalize writes `bundle/architecture.md` + `.json`, *and*
  seeds the knowledge graph with a node per component, entity, endpoint and
  module, so turn one of `bird code` can query a system that doesn't exist yet.

```bash
bird arch "design a multi-tenant billing service"
bird code "build it" --from-arch latest      # or let the lead do both
```

### `code`, the builder *(`bird code`, `bird chat`)*

The ReAct coding loop: read → plan → edit → verify.

- `kg_query` is the primary search tool for the whole session; the engine nudges
  it back when it drifts into `grep`.
- For anything multi-step it calls `plan` once, and the engine **pins a live
  tracker** into the conversation showing the current step, its files, and
  related files pulled from the graph. `done` is blocked while steps are open.
- `bash` is category-allowlisted: read-only search, test runners, linters, git
  reads. Everything else is rejected with a message naming what *is* allowed.
- Edits and writes are gated. You approve them with a real diff.

### Next up: `design` and `research`

Two more harnesses are on the way: `design` (product intent, user flows and
interface direction, feeding into `arch`) and `research` (multi-source
investigation that lands its findings in the knowledge graph). See
[Coming soon](#coming-soon).

---

## Features

**Context engine**
- Per-branch knowledge graph at `.bird/kg/<branch>/`, so switching branches never
  corrupts it. Content-hashed extraction cache shared across branches.
- Code via pure AST extraction (no LLM, no keys). Docs, papers and images
  additionally go through semantic extraction when a backend is available.
- Deterministic query expansion + IDF ranking; BFS for "what is this", DFS for
  "how does X reach Y".
- Background build, incremental update, staleness detection.

**Models**
- Provider-neutral message history. `/model` swaps mid-conversation, and
  Ollama ↔ OpenRouter handoff just works.
- Interactive picker fed by live discovery: `models.json`, the Ollama daemon
  you're pointed at, and the OpenRouter catalog, merged and deduped. Unreachable
  or unconfigured sources are skipped with a note, not an error.
- Named roles in `models.json`: `default`, `architect`, `judge`, `compactor`,
  `vision`, `kg`, so the critic and the compactor are never the model under test.
- `kg` names the model that reads docs/papers into the graph. It wants context
  (chunks are packed to 60k tokens) and clean JSON, not brilliance. Code never
  reaches it, so a cheap mid-tier model is the right choice here.

**Safety & control**
- Permission brokers: interactive UI, console prompt, auto-approve (`--yes`), and
  deny-with-a-reason (the default when nobody can be asked, because a gate no
  one can answer must never default to yes).
- Gating attaches at runner construction, so dispatched sub-sessions inherit it.
- Allowlisted bash; every rejection logged as a session event.

**Sessions**
- Everything logged to `.bird/sessions/<run-id>/events.jsonl`.
- `/sessions` to browse, `/continue` to resume, `/rename` to label,
  `/reload` to respawn on freshly-loaded code *without losing the conversation*.
- Two-stage compaction at 90% of the window: stub old tool results (free), then
  summarize with the pinned compactor model. Offline falls back to stubs.

**Skills**
- Markdown procedures with front-matter, loaded on demand via the `skill` tool or
  a `/<skill-name>` slash command. Only the one-line index sits in the system
  prompt; the body loads when it's relevant.
- Discovery order: project `.bird/skills/` → user `~/.bird/skills/` → built-in.

**Research**
- `web_search` (DuckDuckGo, no API key) and `web_fetch` (HTML → markdown, cached
  15 min). Both excluded from eval control arms, because a network-free eval
  shouldn't depend on them.

**Interfaces**
- Full-screen **TUI** (pi-tui, "Claude Native" palette) with keyboard-first
  permission cards showing real diffs. It's the default when installed.
- Plain REPL fallback (`--plain`), works anywhere.
- **Architecture Workbench** in the browser (React + `@xyflow/react` + Zustand)
  with a clickable, editable canvas, a five-tab rail, and localStorage-persisted
  viewport. Ships pre-built, so no Node is required to use it.
- `bird serve`: JSON-lines over stdio, for embedding anywhere.

---

## Commands

```bash
bird                          # interactive lead (TUI if installed, else REPL)
bird lead "task"              # one-shot lead: routes and builds
bird chat                     # interactive code harness
bird code "task"              # one-shot code harness
bird code "task" --from-arch latest   # seed from a finalized architecture
bird arch ["what to design"]  # architecture session in the browser
bird kg status|build|update|query "question"
bird serve [--harness code|lead]      # JSON-lines bridge over stdio
```

**Flags**, shared by `code` / `chat` / `lead` / `serve` / `arch`:
`--repo <path>` · `--model <alias|provider:model>` · `--max-turns N` ·
`--resume <run-id>` · `--models-json <path>` · `--no-kg` (control arm: drop the
graph tool). Then `-y/--yes` (auto-approve, for unattended `code`/`lead` runs) ·
`--tui` / `--plain` (interactive surface) · `--from-arch <run-id|latest>` ·
`--no-open` (arch, headless). `kg` takes only `--repo` and `--budget`.

**Slash commands** (REPL and TUI):

```
/help  /model [spec|filter]  /kg status|build|update|query  /tools  /skills
/compact  /clear  /reload  /session  /sessions [filter]  /continue <id>
/rename <name>  /quit          plus /<skill-name> for any loaded skill
```

---

## Configuration

**`models.json`** holds providers, per-model context windows and capabilities,
and the role aliases. It ships inside the package at `src/bird/models.json`, so a
checkout and an installed wheel read the same file. Override per-run with
`--models-json`, which is also the way to keep your own config when bird is
installed somewhere you'd rather not edit.

**Environment**

| Variable | Effect |
|---|---|
| `OPENROUTER_API_KEY` | enables OpenRouter models + catalog discovery |
| `OLLAMA_API_KEY` | enables Ollama Cloud |
| `BIRD_KG_BACKEND` | override the `kg` alias with a raw `graphify.llm` backend, which then uses graphify's own env vars for URL and key; `none` disables semantic extraction entirely |
| `BIRD_KG_MODEL` | model for that extraction; wins over the `kg` alias's model, and applies to either path |
| `BIRD_PYTHON` | which Python the TUI spawns `bird serve` with |
| `BIRD_URL` | dev only; the live `bird arch` port that `arch-ui`'s Vite dev server proxies to |

**On disk** (inside the target repo)

```
.bird/
  kg/<branch-slug>/graphify-out/     the knowledge graph
  sessions/<run-id>/                 events.jsonl, artifacts/, bundle/
  skills/*.md                        project-local skills
graphify-out/cache/                  content-hashed extraction cache
```

---

## Layout

```
src/bird/
  cli.py            entry point, every subcommand
  engine/           runner (ReAct loop, guards, nudges), compactor, session log
  context/kg.py     the knowledge graph
  harnesses/
    registry.py     name → HarnessDef → built Runner (the one construction path)
    lead/           the front door: routing policy + dispatch tools
    arch/           state, tools, sketch, critic, renderers, bundle, KG seed
    code/           the coding loop's instructions + toolset
    handoff.py      the arch → code seam
  tools/            shared toolbox: read/edit/write, bash, kg_query, web, plan,
                    skill, done
  llm/              registry, discovery, ollama, openai-compat wire, validation
  permissions.py    broker protocol, tool gating, the four brokers
  skills.py         skill discovery and loading
  serve.py          JSON-lines session pump
  http_transport.py SSE + replay buffer + static serving (stdlib only)

tui/        terminal UI (TypeScript, pi-tui)
arch-ui/    architecture Workbench (React + Vite), built into harnesses/arch/static/
docs/       arch-state-schema.md · arch-ui-features.md · arch-remaining-work.md
```

The engine knows no harness by name. `harnesses/registry.py` is the only module
that maps a name to its wiring, which is why the CLI and the lead's dispatch
tools build runners the same way, and why a dispatched sub-session is born with
the same permission gate as its parent.

---

## Development

```bash
.venv/bin/pytest -q                  # 462 tests
cd arch-ui && npm run check          # tsc + 42 vitest tests
cd tui     && npm run check          # tsc

python3 scripts/arch_devserver.py    # the real arch stack, scripted model;
                                     # for working on the Workbench page
```

The Workbench build output in `src/bird/harnesses/arch/static/` **is committed on
purpose**, so `bird arch` works for someone who has never installed Node. Rebuild
(`cd arch-ui && npm run build`) and commit `index.html` and `assets/*` together;
`tests/test_packaging.py` fails if they drift apart.

To try the agent end to end, run it from inside any repository you have, or
point it at one with `--repo` (a subcommand flag: `bird lead --repo
/path/to/project`). A small project with a deliberately planted bug makes the
best manual smoke test. Run `bird code "fix the failing test"` and watch which
tools it reaches for.

---

## Status

The engine, the three harnesses, the context engine, permissions, skills,
sessions and both frontends are built and tested. What's left, mostly Workbench
polish, is tracked in `docs/arch-remaining-work.md`. The architecture state
contract lives in `docs/arch-state-schema.md`; the page's behaviour in
`docs/arch-ui-features.md`.

### Coming soon

Two more harnesses are in progress. Both slot into the same registry as `lead`,
`arch` and `code`, with the same engine, the same toolbox and the same
permission gate, so they gain the knowledge graph, session resume, model swapping
and skills for free.

- **`design`**, the product and interface layer that sits *before* architecture.
  Where `arch` answers "what are the components and how do they talk", `design`
  answers "what is this for, who uses it, and what should it feel like": product
  intent, user flows, interface structure, and the visual direction, handed to
  `arch` the same way `arch` hands a finalized bundle to `code`.
- **`research`**, a depth-first investigation harness. Multi-source sweeps,
  source-by-source reading, and a synthesis pass that lands its findings in the
  knowledge graph so later `arch` and `code` sessions can query the research
  instead of re-reading it.

Contributions and issues are welcome. The harness registry
(`src/bird/harnesses/registry.py`) is the extension point if you want to build
your own.
