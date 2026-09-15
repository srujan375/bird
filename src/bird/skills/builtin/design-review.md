name: design-review
description: Use when the user picks a design direction and before offering design_finalize, or on explicit request (/design-review) — the critique gate that scores the artboard; do not offer finalize while a dimension sits under 7.

# Design Review

The pre-finalize critique gate. Offer finalize only after this has run and
cleared — a design the designer never scored is a design shipped on hope.
Nothing in the harness enforces this; you do.

Announce: "I'm using the design-review skill."

## When to run

- After the user picks a direction and before you offer `design_finalize`.
- On explicit request: `/design-review`.

## The five dimensions

Score the current artboard 0–10 on each:

| Dimension | The question it answers |
|---|---|
| Visual hierarchy | Does the eye land where the page wants it to, in the right order? |
| Detail execution | Are the details finished — spacing, alignment, states, real copy? |
| Functionality | Does it answer the brief's jobs? Could a user actually do them here? |
| Brief consistency | Does it honor the locked brief and the active theme's tokens and Do-nots? |
| Innovation | Is there a take here, or only a template? |

Bands: **0–4** broken, **5–6** functional, **7–8** strong, **9–10**
exceptional.

## Scoring discipline

- The score is the **worst sustained band**, not an average. One dimension
  sitting at 4 means the work is a 4, however good the rest is.
- Numbers without evidence get rejected. Every score cites the specific
  element or section it is judging — "hero CTA is 44px and reads 'Start the
  14-day trial'" is evidence; "looks good" is not.
- Innovation is allowed to be low. 5/10 is fine for a production
  deliverable; do not punish appropriate conservatism.

## The subtraction pass

Before scoring, name the one decoration you would remove — a shadow, a
gradient, an accent, a rule, an animation. Remove it with a `design_edit`
only if the page reads the same without it; the user edits this document
too, and a decoration they added on purpose is theirs to keep. If nothing
names itself as removable, look again: decoration that survives the question
earned its place, decoration that was never questioned did not.

## The gate

If any dimension is below 7 — innovation exempt — fix it with concrete
`design_edit` ops (each fix is a version), then rescore. Do not offer
finalize while a dimension sits under 7.

Report the score table to the user in chat, one evidence line per dimension:

```
| Dimension | Score | Evidence |
|---|---|---|
| Visual hierarchy | 8 | hero headline dominates; nav and footer recede correctly |
| Detail execution | 6 | pricing card has no hover state; footer links are lorem |
| ... | | |
```

Offer `design_finalize` only when every dimension is 7 or above (innovation
exempt). The user may still finalize from the page at any time; the table is
what lets them do it informed.

## Keep it honest

A 10 is rare. If everything scores 9–10, re-check hierarchy and detail
execution before believing it — you are probably grading gently.
