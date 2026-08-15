name: research
description: Use when asked to research a topic, weigh options ("what are the options for X"), compare alternatives ("compare A vs B"), investigate an error or behavior, fact-check before a design decision, or answer a multi-step repo question ("how does X work end to end?").

# Research

A disciplined research procedure: frame first, look up efficiently,
cross-check before claiming, and always end in a fixed-format report.

Announce: "I'm using the research skill."

## The gate (read before any tool call)

**Pick a mode and a report format BEFORE making any tool call.** No search,
no fetch, no repo lookup until step 1 (Frame) below is done and stated in one
short line. Research that starts with a tool call drifts; framing costs one
sentence and prevents it.

## Step 1: Frame

State, in 2–4 lines:

1. **Mode** — exactly one of:
   - `web` — the question is about the outside world (libraries, APIs,
     standards, best practices, anything not in this repo).
   - `repo` — the question is about this codebase. Tool order:
     `kg_query` → `grep`/`glob` → `read`. Every claim in the report must cite
     `file:line` evidence.
   - `hybrid` (repo → web) — the question needs both: an error to reproduce,
     a dependency to identify, a behavior to explain. Ground in the repo
     FIRST, then take the exact strings found there to the web.
2. **Report format** — exactly one of `topic`, `options`, `comparison`
   (shapes defined below; `topic` is the default).
3. **Sub-questions** — decompose into at most 3. If it doesn't decompose,
   that's fine: one question.
4. **Sufficiency criterion** — one sentence: "I can answer when I know …"
   This is the stop condition; check it after every lookup.

## Step 2: Ground (hybrid mode only)

Before any web query, find the repo facts the query should be built from:

- the exact error message or stack-trace line (`grep` for it)
- the dependency name AND version (`pyproject.toml`, `package.json`, lockfiles)
- the config value or API usage in question (`kg_query` for where it's used,
  `read` for the call site)

Then search the web using those exact strings — `"ECONNRESET" node 22 fetch`
beats `node networking problem`. A vague query wastes lookups; a grounded
one usually answers itself in the snippets.

## Step 3: Search / lookup

Guidance: **search is cheap, fetch is expensive.** A WebSearch returns
snippets — a few lines each. A WebFetch pulls a whole page into context (up
to 500KB). So: search broadly, fetch selectively.

Rules of engagement:

- **Primary sources first**: official docs, the project's GitHub repo,
  RFCs/specs, release notes and changelogs. Prefer them over blog posts and
  Q&A sites; prefer those over aggregator content farms.
- **Judge snippets before fetching.** Read the snippet; only fetch a page
  when the snippet promises the specific fact you need and the source is
  credible for it.
- **Fetch only the most promising results** — typically 1–2 pages per
  sub-question, never a whole results page.
- In `repo` mode there is no web at all: use kg_query first (structure),
  grep/glob second (literal strings, filenames), read last (confirmation
  with line numbers).

Never pad a report with uncited guesses to hide a gap — if something could
not be checked, name it in `Coverage:`.

## Step 4: Cross-check

- A claim needs **a primary source OR 2+ independent sources**. One random
  blog post is neither.
- Note **publication dates** when they matter (version-specific behavior,
  deprecations, API changes) — a 2019 answer about a 2024 API is a caveat,
  not a source.
- When sources **disagree**, say so explicitly and weigh them (primary beats
  secondary; newer beats older; the repo's own behavior beats both).
- In `repo` mode, cross-check means: the `file:line` you cite actually says
  what you claim — re-read before citing if unsure.

## Step 5: Stop gate

After each lookup, check the sufficiency criterion from step 1:

- **Converged** (criterion met, claims cross-checked) → go to Report.
- **Not converged** → continue with the next sub-question. Every lookup
  must be checked against the sufficiency criterion: if a lookup won't
  move you toward it, don't make it.

## Step 6: Report

Conclusion first, in the fixed format chosen at framing time. Do not invent
other shapes; do not bury the answer after a narrative of your searches.

### Format: `topic` (default)

```
## <short title>

**Question:** <the question as framed>

**Answer:** <2–3 sentences, conclusion first>

**Key findings:**
- <finding> [n]            ← each bullet cites a source number or file:line
- <finding> [n]

**Caveats:**
- <what's uncertain, version-dependent, or possibly stale>

**Coverage:** <only if some sub-question could not be answered or a claim
could not be cross-checked — e.g. source unreachable/paywalled, not found,
ambiguous; name exactly what could not be checked>

Sources:
1. <title> — <url>
2. ...
```

### Format: `options` (feature research)

```
## Options for <goal>

**Goal:** <what we're trying to achieve, one line>

**Option A: <name>**
<1–2 sentence description>
- Pros: ...
- Cons: ...
- Effort: <rough — S/M/L or days>

**Option B: <name>**
...

**Recommendation:** <which option and why, 2–3 sentences>

**Open questions:**
- <what would need answering before committing>

**Coverage:** <only if some sub-question could not be answered or a claim
could not be cross-checked>

Sources:
1. ...
```

### Format: `comparison` (A vs B)

```
## <A> vs <B>

**Criteria:** <the 3–6 criteria that matter for THIS decision, listed up front>

| Criterion | <A> | <B> |
|---|---|---|
| <criterion> | <verdict + one-line why> | <verdict + one-line why> |

**Recommendation:** <which wins for the stated use case, 2–3 sentences>

**Coverage:** <only if some sub-question could not be answered or a claim
could not be cross-checked>

Sources:
1. ...
```

### Repo and hybrid reports: add `Evidence:`

`repo` and `hybrid` reports include, alongside `Sources:` (web URLs), an
`Evidence:` section with the repo facts the answer rests on:

```
Evidence:
- src/bird/tools/web.py:214 — where the cache TTL is enforced
- pyproject.toml:12 — httpx pinned to 0.27
```

Cite real line numbers from files you actually read; never cite a path you
only saw in a search hit.

## Triggers and non-triggers

Triggers (load this skill):
- "research X", "look into X", "what are the options for X"
- "compare A vs B", "A or B for our use case?"
- "investigate this error/behavior" (hybrid: repo facts → web)
- fact-checking before a design decision ("is X still true in version Y?")
- multi-step repo questions ("how does X work end to end?")

Non-triggers (do NOT load this skill):
- **One-shot repo lookups** — "where is X defined?" is a single `kg_query`
  or `grep`; just do it.
- **Trivial facts you already know** — don't burn lookups confirming what
  you're confident of; if you do cite it, say it's from prior knowledge.
- **Build/fix requests** — "fix the failing test", "add a retry" are work,
  not research; dispatch to the code harness (or just do it).
