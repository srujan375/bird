name: skill-creator
description: Use when creating a new skill, updating an existing skill, or deciding whether a workflow should become a reusable skill.

# Skill Creator

Create and improve bird skills with test-driven process design. A skill is
process documentation that changes agent behavior, so treat it like code:
establish a failing or weak baseline, write the smallest useful skill
change, then verify behavior improved.

Announce: "I'm using the skill-creator skill to design and test this skill."

## What a skill is

A skill is a markdown file with front-matter and a body:

```
name: my-skill
description: Use when <triggering conditions only>

# My Skill

<instructions the agent follows when this skill is loaded>
```

- The `name` is lowercase letters, numbers, and hyphens (e.g. `commit-style`).
  It becomes the slash command: `/my-skill`.
- The `description` starts with "Use when" and contains trigger conditions
  only — not workflow steps. It lives in the system-prompt index, so keep it
  concise; a description that summarizes the workflow can cause the agent to
  skip reading the full skill body.
- The body is the full procedure the agent follows once the skill is loaded.

## Where skills live

- Project skills: `.bird/skills/<name>.md` (version-controlled, shared with the team)
- User skills: `~/.bird/skills/<name>.md` (personal, cross-project)
- Built-in skills: ship with bird (like this one)

Project skills override user skills override built-in skills by name.

## When to create a skill

Create a skill when:
- the technique is reusable across projects
- agents will not reliably infer the process from normal instructions
- the workflow benefits from progressive disclosure (load on demand)
- future sessions need a named trigger and durable guidance

Do NOT create a skill for:
- one-off project facts — put those in a README or docs
- mechanical rules better enforced by scripts, tests, or linters
- behavior that has not been observed or pressure-tested

## Workflow

### Step 1: Capture intent

Clarify in normal chat:
- What should this skill help agents do?
- When should it trigger?
- What should NOT trigger it?
- What failure have we seen, or expect, without it?

### Step 2: Establish baseline

Before writing the skill, describe what goes wrong without it. For a new
skill, note the expected weak behavior. For an edit to an existing skill,
note the current behavior you want to improve.

### Step 3: Write the minimal skill

Create the skill file using the `write` tool:

```
write {"path": ".bird/skills/<name>.md", "content": "<front-matter + body>"}
```

Front-matter rules:
- `name`: lowercase a-z, 0-9, hyphens. No leading/trailing or consecutive hyphens.
- `description`: start with "Use when", trigger conditions only, keep concise.

Body rules:
- Put the most important gate or principle near the top.
- Prefer exact commands and paths.
- Keep the skill under ~500 lines; put bulky references in sibling files.

### Step 4: Verify

After writing the skill, verify it:
- The file parses correctly (front-matter + body).
- The skill appears in the system prompt's `[skills]` index on the next run.
- The description is specific enough to trigger on the right tasks and not
  trigger on the wrong ones.

### Step 5: Refine

If verification fails:
- If the agent acts before it should: add an explicit gate.
- If the agent claims verification without evidence: require command output.
- If the agent follows the description only: shorten the description to
  triggers only.
- If the agent over-applies the skill: add non-trigger cases.

## Report format

When done, report:

```
## Skill Created/Updated

**Skill:** `<name>`
**Path:** `.bird/skills/<name>.md`

### Baseline
- <what goes wrong without the skill>

### Verification
- <how you verified it works>

### Trigger notes
- Should trigger: <examples>
- Should not trigger: <examples>
```