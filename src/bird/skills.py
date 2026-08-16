"""Skills — reusable, lazily-loaded procedures for the agent.

A skill is a markdown file with a small front-matter header (`name`,
`description`) and a free-form body of instructions the model loads on demand
via the `skill` tool, or the user loads via the `/<skill-name>` slash command.
This mirrors how pi / Claude Code treat skills (the Agent Skills standard):
a cheap index (name + one-line description) lives in the system prompt, and
the full body is only injected when the skill is relevant — keeping the base
prompt small (decision #6: schema/prompt budget).

Two front-matter formats are supported:
  - bird-native: a leading block of bare `key: value` lines terminated by a
    blank line (backward compatible with all existing bird skills).
  - Claude Code: YAML-style `---` delimited front-matter, as used by
    `.claude/skills/<name>/SKILL.md` files.

Discovery order (first wins on name collision):
  1. project skills  — `.bird/skills/*.md` (repo-local, version-controlled)
  2. project skills  — `.claude/skills/<name>/SKILL.md` (Claude-compatible)
  3. user skills    — `~/.bird/skills/*.md` (personal, cross-project)
  4. user skills    — `~/.claude/skills/<name>/SKILL.md` (Claude-compatible)
  5. built-in skills — packaged inside bird (`src/bird/skills/builtin/*.md`)

Built-in skills ship with bird so features like `skill-creator` are available
out of the box; project and user skills can override them by name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# front-matter: a leading block of `key: value` lines, terminated by a blank
# line or end-of-file. Only `name` and `description` are parsed; everything
# after the header is the skill body.
_FM_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$")

# valid skill names: lowercase letters, numbers, hyphens; no leading/trailing
# or consecutive hyphens (matches the Agent Skills standard)
_VALID_NAME = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

# built-in skills live next to this module
_BUILTIN_DIR = Path(__file__).parent / "skills" / "builtin"


@dataclass(frozen=True)
class Skill:
    name: str
    description: str  # one line, shown in the system-prompt index
    body: str  # full instructions, returned by the `skill` tool
    path: Path
    source: str  # "project" | "user" | "builtin"

    def index_line(self) -> str:
        """The cheap index entry that lives in the system prompt."""
        return f"- {self.name}: {self.description}"


def _parse_skill(text: str, path: Path, source: str, fallback_name: str = "") -> Skill | None:
    """Parse front-matter (`name:`/`description:`) + body. Returns None if the
    file lacks a usable body (silently skipped, not an error — a stray .md
    note in the skills dir shouldn't break the harness).

    Two front-matter formats are supported:
      - YAML-style: a leading line that is exactly ``---`` opens a block of
        ``key: value`` lines closed by another ``---``. Other fields beyond
        ``name``/``description`` (e.g. ``license``, ``allowed-tools``) are
        parsed without error but ignored.
      - bird-native: a leading block of bare ``key: value`` lines terminated
        by a blank line (the original format, backward compatible).

    `fallback_name` is used when no `name` is found in the front-matter. It
    defaults to the file's stem — but for Claude `SKILL.md` files the stem is
    always "SKILL", so callers pass the subdirectory name instead.
    """
    lines = text.splitlines()
    name = ""
    description = ""
    i = 0
    n = len(lines)

    # YAML-style front-matter: a leading line that is exactly "---".
    if n > 0 and lines[0].strip() == "---":
        i = 1
        while i < n:
            line = lines[i]
            if line.strip() == "---":
                # closing delimiter — body starts after this line
                i += 1
                break
            m = _FM_LINE.match(line)
            if m:
                key, val = m.group(1).lower(), m.group(2).strip()
                if key == "name":
                    name = val
                elif key == "description":
                    description = val
                # any other key: parsed without error, ignored
            # a non-`key: value` line inside the YAML block is ignored too
            i += 1
    else:
        # bird-native front-matter: bare `key: value` lines until a blank line.
        while i < n:
            line = lines[i]
            if not line.strip():
                # blank line ends the front-matter block
                if name:
                    break
                i += 1
                continue
            m = _FM_LINE.match(line)
            if not m:
                # a non-blank, non-`key: value` line before we have a name means
                # this isn't front-matter — treat the whole file as body and
                # derive a name from the filename
                break
            key, val = m.group(1).lower(), m.group(2).strip()
            if key == "name":
                name = val
            elif key == "description":
                description = val
            i += 1

    if not name:
        name = fallback_name or path.stem
    if not description:
        description = ""
    body = "\n".join(lines[i:]).strip()
    if not body:
        return None
    return Skill(name=name, description=description, body=body, path=path, source=source)


def _scan_dir(d: Path, source: str) -> list[Skill]:
    if not d.is_dir():
        return []
    out: list[Skill] = []
    for p in sorted(d.glob("*.md")):
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        sk = _parse_skill(text, p, source)
        if sk is not None:
            out.append(sk)
    return out


def _scan_claude_skills_dir(d: Path, source: str) -> list[Skill]:
    """Scan a Claude Code-style skills directory.

    Claude skills live one-per-subdirectory: ``<d>/<skill-name>/SKILL.md``.
    The skill name comes from the front-matter ``name`` field if present,
    otherwise from the subdirectory name (NOT the ``SKILL.md`` stem, which is
    always "SKILL"). Subdirectories without a ``SKILL.md`` are skipped
    silently, the same way ``_scan_dir`` skips unparseable files.
    """
    if not d.is_dir():
        return []
    out: list[Skill] = []
    for sub in sorted(p for p in d.iterdir() if p.is_dir()):
        skill_md = sub / "SKILL.md"
        if not skill_md.is_file():
            continue
        try:
            text = skill_md.read_text(encoding="utf-8")
        except OSError:
            continue
        sk = _parse_skill(text, skill_md, source, fallback_name=sub.name)
        if sk is not None:
            out.append(sk)
    return out


def load_skills(repo_root: Path) -> list[Skill]:
    """Load skills from project, user, and built-in directories.

    Project skills override user skills override built-in skills by name.
    Order is project-first so the index reads naturally; dedup keeps the
    first (highest-priority) occurrence.

    Discovery order (first wins on name collision):
      1. ``.bird/skills/*.md`` (project, bird-native)
      2. ``.claude/skills/*/SKILL.md`` (project, Claude-compatible)
      3. ``~/.bird/skills/*.md`` (user, bird-native)
      4. ``~/.claude/skills/*/SKILL.md`` (user, Claude-compatible)
      5. built-in skills (packaged inside bird)
    """
    project = _scan_dir(repo_root / ".bird" / "skills", "project")
    project_claude = _scan_claude_skills_dir(repo_root / ".claude" / "skills", "project")
    user = _scan_dir(Path.home() / ".bird" / "skills", "user")
    user_claude = _scan_claude_skills_dir(Path.home() / ".claude" / "skills", "user")
    builtin = _scan_dir(_BUILTIN_DIR, "builtin")
    seen: set[str] = set()
    out: list[Skill] = []
    for sk in (*project, *project_claude, *user, *user_claude, *builtin):
        if sk.name in seen:
            continue
        seen.add(sk.name)
        out.append(sk)
    return out


def render_index(skills: list[Skill]) -> str:
    """The block appended to the system prompt. Empty when there are no
    skills, so the prompt is unchanged for repos that don't use them."""
    if not skills:
        return ""
    lines = ["[skills] Call the `skill` tool with a name to load its full instructions:"]
    lines.extend(s.index_line() for s in skills)
    return "\n".join(lines)


def is_valid_skill_name(name: str) -> bool:
    """Whether `name` is a legal skill name (Agent Skills standard)."""
    return bool(_VALID_NAME.match(name))