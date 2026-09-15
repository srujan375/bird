# bird — project instructions

bird is a multi-harness coding agent for small open models: a Python package
in `src/bird/` (the agent) plus three TypeScript workspaces — `tui/` (terminal
UI), `arch-ui/` and `design-ui/` (browser workbenches). Tests live in `tests/`
and mirror the package (`tests/test_<module>.py`).

## Layout

- `src/bird/cli.py` — every subcommand: bare `bird` (the lead), `chat`, `code`,
  `arch`, `design`, `serve`, `setup`.
- `src/bird/engine/` — `runner.py` is the ReAct loop: tool-call validation,
  retries, stuck guards, nudges, the pinned plan tracker, and the system prompt.
  `compactor.py` compacts at 90% of the context window. `session.py` writes the
  session log.
- `src/bird/harnesses/` — one package per harness: `lead/` (front door, dispatches
  the others), `code/` (the builder: instructions + toolset), `arch/`
  (architecture workbench), `design/` (visual workbench). `registry.py` is the
  ONLY place a harness name maps to its wiring; `handoff.py` is the arch → code
  seam. Each `instructions.md` is a model's system prompt — write it as prose
  for a model, not docs for a person.
- `src/bird/tools/` — the shared toolbox: read/edit/write/delete, bash, grep/glob,
  kg_query, web, plan, skill, done. Tools take no constructor deps; everything
  rides on `ToolContext` in `tools/base.py`.
- `src/bird/context/kg.py` — the knowledge graph (`kg_query`, the repo map in the
  system prompt, blast radius for plans). `context/store.py` — per-session
  findings that flow between harness forks.
- `src/bird/llm/` — model registry (`models.json` aliases: default, architect,
  designer, compactor, vision, kg), Ollama/OpenRouter discovery, the
  OpenAI-compatible wire.
- `src/bird/permissions.py` — the broker protocol and tool gating.
  `serve.py` — the JSON-lines session pump the TUI and browser pages drive.
  `http_transport.py` — SSE + static serving for the workbenches.
- `src/bird/mcp/` — MCP client, tool bridge, catalog. `skills.py` — skill
  discovery (`.bird/skills/`).
- `.bird/sessions/<run>/events.jsonl` — session logs. Latency comes from `mono`,
  never `ts` (machine sleep inflates `ts`).

## Running checks — use these exact forms; they pass the bash allowlist

- Whole Python suite: `.venv/bin/python -m pytest tests -q` (~1180 tests, ~12s).
  One file: `.venv/bin/python -m pytest tests/test_runner.py -q`.
- Plain `pytest` and `python` do not have the deps, and `uv` is not installed.
  Always `.venv/bin/python -m ...`.
- Syntax-check one file: `.venv/bin/python -m py_compile src/bird/x.py`. There is
  no ruff/mypy config in this repo.
- `tui/`: `cd tui && npm run check` (tsc). `npm run test` runs the tsx smoke scripts.
- `arch-ui/`, `design-ui/`: `cd arch-ui && npm run check` (tsc + vitest).
  `npm run build` writes into `src/bird/harnesses/<arch|design>/static/`, which
  is COMMITTED on purpose. Commit `index.html` and `assets/*` together or
  `tests/test_packaging.py` fails.
- `python -c "..."` and `python -m <module>` outside the test/lint allowlist are
  rejected. Write a script file if you need to poke at something.

## Conventions

- Construct every harness through `harnesses/registry.build_runner`; the engine
  never names a harness. New harness tuning goes in a `HarnessDef`, not in cli.py.
- Every mutating tool is gated by the permission broker and `bash` is an
  allowlist (`tools/bash.py`). Do not widen the allowlist to get a task done.
- Any new non-`.py` runtime file (instructions, static assets, themes) must be
  listed under package-data in `pyproject.toml`, or wheels ship without it.
- Add or update the test next to the code you change. Tests build synthetic
  graphs and sessions in `tmp_path`; nothing touches the network.
- Comments explain WHY — the failure that motivated a guard — not what. Keep
  that style.

## Things that look wrong and are not

- The working tree is normally dirty with in-progress work across many files.
  Never stash, checkout, reset or revert files you did not change.
- `motion-primitives/` is vendored upstream code; it tops the repo map because
  every vendored component imports its `cn()`. `graphify-out/`, `.bird/`,
  `node_modules/`, `*.egg-info` are generated.
- `src/bird/harnesses/*/static/assets/*` are hashed build outputs; never
  hand-edit them.
- `tests/test_runner.py` fakes the KG with a stub. A change to `digest()` or
  `query()` output needs a real-graph test in `tests/test_kg.py`.
