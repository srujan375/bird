---
name: arch
description: Design a system or feature with the user on bird's architecture Workbench, a browser board, before building it. Use when the user wants to design, architect, sketch, or plan a system, service, feature, pipeline, or data flow, or types /bird:arch. Not for changes whose shape is already obvious.
---

# Design on the board, then build

The Workbench is bird's arch harness on a browser page: boxes and wires,
rival approaches side by side, decisions recorded with the reason the losers
lost, and a picker for the questions the architect needs the user to settle.
The user designs there **with** an architect, which is a separate Claude Code
session that bird spawns. Your job is to dispatch it, wait, and build from
what comes back. Do not design the system yourself in the terminal; that is
what the board is for.

## 1. Compose the task

One paragraph, in plain words: what is being designed, and every constraint
you already know from this conversation: scale, stack, what already exists in
the repo, what the user has ruled out, what they care about. The architect
starts from this text plus the repository itself. It has no other memory of
this conversation, so anything you leave out it will have to ask again.

## 2. Launch it

From the repository root, run this **in the background** (it runs until the
user is done designing, which can be many minutes), quoting the task:

```
bird arch --engine claude --linger 90 --close-when-empty 120 "<task>"
```

The flags matter. `--linger 90` keeps the finished board readable for a
minute and a half after the handoff and then exits, which is what wakes you.
`--close-when-empty 120` ends the run if the user closes the tab without
handing off, so you are never left waiting on a page nobody will reopen.

It prints `page: http://127.0.0.1:<port>/` and opens the browser. Tell the
user the board is open, give them the URL in case no tab appeared (a run
whose page is never opened waits indefinitely), that they should hand off
from the conversation there when they are done (the architect records it
with its `handoff` tool), and that you will pick up from the bundle. Then
wait for the process to exit. Do not poll it, and do not start work that
assumes a design.

If `bird` is not found, it is installed from https://github.com/srujan375/bird.
If it says `claude` is not logged in, the user runs `claude auth login`.

## 3. Read the result

The command's last lines are the contract:

- `handoff: <path>/architecture.md` means the user handed off. Read that
  file. It carries the brief, the decisions with their rationale, the
  approaches not taken and why, one sheet per component, and whatever is
  still open. Summarize the settled decisions in a few lines and ask
  whether to build.
- `no handoff: ...` means the page closed before the design was finished.
  Say so, and offer the `--resume` command it printed.

The same design is also at `.bird/sessions/<run_id>/bundle/architecture.json`
if you need it as structure rather than prose.

## 4. Build from it

The bundle is the brief. Decisions in it are settled: do not reopen one
without a reason worth saying out loud. Open questions in it are yours to
ask before touching the parts they gate. Approaches marked as not taken stay
not taken; the reason recorded is the answer to "why not X" when it comes up.
