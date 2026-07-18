# mha — multi-harness agent

A coding agent for **small open models (9–20B)** whose differentiator is the
context engine: a knowledge graph (graphify) + turn-based context injection
instead of grep-style exploration. See `PLAN.md` for the full decision record.

## Install

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
```

Requires Ollama running locally (`ollama serve`) for local models;
`OPENROUTER_API_KEY` for OpenRouter models.

## Use

```bash
mha                       # interactive mode — full-screen TUI when installed
                          # (`cd tui && npm install` once), else the plain REPL
mha chat --plain          # force the plain REPL
mha code "fix the failing test in tests/test_pricing.py"
mha code "task" --no-kg   # control arm: no kg_query tool
mha kg status|build|update|query "question"
mha serve                 # JSON-lines bridge over stdio (used by the TUI)
```

Interactive slash commands: `/help /model [spec] /kg … /tools /compact /clear
/session /quit`. `/model` swaps models mid-conversation — history is
provider-neutral, so ollama ↔ openrouter handoff just works.

The knowledge graph builds automatically in the background on first run
(`.mha/kg/<branch>/graphify-out/`); until it's ready, `kg_query` tells the
model to fall back to bash search, and the runner injects a notice when the
graph comes online. Sessions log to `.mha/sessions/<run-id>/events.jsonl`.

## Demo fixture

`~/Workspace/Personal/test_repo` is a small shop project with a planted bug
(`git checkout -- src/shop/pricing.py` there to re-plant it after a run).

## Tests

```bash
.venv/bin/pytest -q
```
