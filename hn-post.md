# Show HN launch

## Title (78 chars)

```
Show HN: an architecture harness where one model designs and another critiques
```

HN's title cap is 80 characters. This fits with two to spare.

"Harness" is the right noun because the thing is more than a drawing surface:
the tools, the two human gates, the critic on its own thread, the bundle and the
handoff into `code`. "Canvas" would name only the part you look at. The `where`
clause is what stops "harness" being misread as *test harness* — it defines the
word inside the same sentence, before anyone can misfile it.

"I built" was cut on purpose. `Show HN:` is defined in the guidelines as
"something you've made", so the opener spends eight characters saying it twice —
and those characters buy back `model`, without which "one designs" hangs on a
vaguer subject.

Naming the mechanism — *one designs, another critiques* — beats counting heads
("two models argue") because a reader instantly pictures a review relationship
rather than two models chatting. It also reads as an answer to the verification
bottleneck HN keeps naming as the real constraint, which keeps it out of the
saturated "another coding agent" bucket.

---

## First comment

Post this immediately after submitting.

> I'm the author. bird is a coding agent built for small open models. It runs
> against local Ollama, Ollama Cloud, or OpenRouter, and most of my hours on it
> have been 9B to 27B.
>
> The architecture harness is the design phase. It's a separate mode with its own
> tools, its own browser page and its own critic, rather than a paragraph pasted
> on top of a coding prompt. You ask for a feature or a service, and instead of a
> plan buried somewhere in chat scrollback you get a live canvas: components, the
> edges between them, the flows, the decisions and whatever alternatives lost.
> The model doesn't draw any of it. It calls tools that mutate typed state and
> the page renders whatever the tools recorded, so the canvas is the design
> itself, not a picture of one. Every box has a validated, logged tool call
> behind it.
>
> The part I'd most like feedback on is the critic. A second model reads the
> design on its own thread every turn and files objections, each with a severity.
> The architect can answer one, act on it, or overrule it with a reason, but it
> can't quietly drop it, and open blockers show up at the finalize gate. There's
> a screenshot in the README where the critic (deepseek-v4-flash) catches that
> emitted[0] gets set on the first reasoning chunk even when nothing was ever
> delivered, and the architect backs down. That's the bet, basically. One model
> asked to both design and review its own work will approve it.
>
> Why bother designing before coding: generation got cheap, decisions didn't. A
> wrong boundary now gets built at full speed across a dozen files before anyone
> reads a diff.
>
> The honest part. bird also builds a knowledge graph of your repo, and I
> benchmarked it against its own control arm (--no-kg, same run, graph tool
> removed, nothing else changed). It lost. The no-KG arm got to the fix carrying
> less context and solved the task just as often. I think I know why, kg_query
> returns about 2k tokens whether the answer needs sixty nodes or three, but
> until a control arm says otherwise I'm not claiming the graph helps. That
> column is in the README table.
>
> What did hold up: bird got to the fix carrying 7,847 tokens of context where
> Claude Code on Sonnet carried 57,577, at roughly $0 against $0.36 a run. That's
> one task, n=3, on a repo I can't publish, so treat it as a signal and not a
> benchmark. A public task pinned to a SHA is the next thing I owe this.
>
> Also worth saying, since it's the first thing I'd ask: nothing edits your repo
> without you. The conversational harness has no edit, write or bash tools at
> all, and every mutation is gated with a real diff.
>
> Happy to answer anything. I'd especially like to hear from people running local
> models about where the architect starts falling over below 20B.

### Why it's ordered this way

Paragraphs 1–2 recover what the title gave up: local models, the pre-code
sequencing, and what "canvas" means mechanically. Paragraph 3 carries the
critic, since the title no longer does, and points at the screenshot so a
curious reader scrolls to the image instead of bouncing.

The negative benchmark sits at position five on purpose — early enough that
nobody can say it was buried, late enough to read as confidence rather than
apology. Volunteering the mechanism you suspect turns a hole into visible
engineering.

The closing ask is deliberately specific. "Where does the architect fall over
below 20B" invites people to run it and report back, which is what keeps a
thread alive past the first hour.

---

## Alternate titles considered

| Title | Chars | Trade |
|---|---:|---|
| Show HN: I built an architecture harness — one model designs it, another argues | 79 | Same title with an em-dash instead of `where`; "argues" is more arresting than "critiques", less precise. |
| Show HN: I built an architecture harness where one designs and another critiques | 80 | Keeps the first-person opener, loses `model`. At the cap. |
| Show HN: I built a canvas where one model designs a system and another argues | 77 | Same mechanism, but "canvas" names only the surface, not the apparatus. |
| Show HN: I built a canvas where one model diagrams a system and another argues | 78 | Most visual reading; "diagrams" slightly demotes the output to a picture. |
| Show HN: I built a canvas where two local models design a system before you code | 80 | Clarity over curiosity; spends the title on pre-code positioning. At the cap. |
| Show HN: a canvas where two local models design a system before you build it | 76 | Same, without the first-person opener. |
| Show HN: bird – an architecture workbench with a critic that argues back | 72 | Names the product; "workbench" doesn't read as diagrammatic. |
| Show HN: I gave my coding agent a knowledge graph, benchmarked it, and grep won | 79 | Highest raw click-through. **Only use if the README leads with the numbers section** — do not pair it with the Workbench-hero README. |

## Before posting

- [ ] `src/bird/serve.py` — `_busy()` / `_enqueue()` are called but never defined.
      The TUI spawns `bird serve`, so it's dead in the working tree. Finish the
      follow-up queue or revert the file. The comment invites people to run
      `--no-kg`, so they will run it.
- [ ] Four tests fail at HEAD, independent of the WIP. The schema-budget one
      (2,167 tokens against the project's own 1,650) sits awkwardly beside a
      comment citing 7,847 tokens of context.
- [ ] `src/bird/models.json` — every alias points at a local MLX model. New users
      can't run it, and it contradicts both the Install table and the "critic is
      never the model under test" claim.
- [ ] Confirm the GitHub repo is public before the clone URL goes out.
- [ ] Optional: retake the hero canvas shot on qwen3.8:27b. It currently shows
      `openrouter:qwen/qwen3.8-2.4t-a95b`, a 2.4T model, on a page headlined
      "small open models." The critic crop is already clean.
