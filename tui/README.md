# bird-tui

Terminal UI for **bird**, implementing the "Claude Native" direction (part b) of
the Open Design canvas: charcoal + clay palette, one mono face, rounded
`╭╮╰╯` panels, dots spinner, keyboard-first permission cards.

Built on [`@mariozechner/pi-tui`](https://www.npmjs.com/package/@mariozechner/pi-tui)
(pi's TUI library) — line-based retained mode with differential rendering, so
the transcript lives in your terminal's native scrollback.

## Run

```bash
bird                               # the default interactive surface once
                                  # `npm install` has run in this directory
bird chat --plain                  # old line-based REPL instead
# or directly:
cd tui && npm start -- --repo /path/to/repo
npm start -- --demo               # scripted demo, no venv/ollama needed
```

The TUI spawns `bird serve` (JSON-lines over stdio) for the real harness:
model turns, tool calls, and the knowledge-graph context engine. Python is
resolved from `$BIRD_PYTHON`, then the bird source venv, then the target repo's
venv, then `python3`.

## Interactions

- Type a message, `⏎` to send, `⇧⏎` newline (`⌥⏎` in terminals that don't
  report shift)
- `/` opens the command drawer (bird's real REPL commands, executed by the
  harness); `↑↓` + `⏎` to pick, `esc` to close
- `esc` interrupts a running turn (takes effect at the next harness step —
  a single in-flight LLM call can't be cancelled)
- Permission cards appear for `edit`/`write` tool calls with a real diff:
  `Y`/`⏎` approve, `N`/`esc` deny. Bash stays ungated because bird allowlists
  it to read-only commands (decision #10).
- `Ctrl+C` or `/quit` exits

## Verify

```bash
npm run check                                    # typecheck
cd .. && .venv/bin/pytest tests/test_serve.py    # bridge protocol tests
```
