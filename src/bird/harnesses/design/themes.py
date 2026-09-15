"""Theme loader — where design themes live and who wins on a name clash.

A theme is either a directory holding THEME.md or DESIGN.md (THEME.md wins if
both — the design brief the model reads) plus an optional tokens.css (the CSS
custom properties injected into every artboard), or a bare ``<name>.md`` file
whose content is the whole brief and which carries no css. Discovery mirrors
skills.py exactly: project themes override user themes override the builtins
packaged with bird, first occurrence by name wins.

  1. project themes — ``<repo>/.bird/design-themes/<name>/`` or ``<name>.md``
  2. user themes    — ``~/.bird/design-themes/<name>/`` or ``<name>.md``
  3. built-in       — packaged inside bird (``src/bird/harnesses/design/themes/``)
"""

from __future__ import annotations

import re
from pathlib import Path

# built-in themes live next to this module
THEMES_DIR = Path(__file__).parent / "themes"

# the two overridable dirs, in precedence order. The project entry is
# repo-relative — joined with the session's repo root at call time, the same
# way skills.py joins `repo_root / ".bird" / "skills"`; the user entry is
# absolute. (The parameter is named USER_THEMES_DIRS after the task that
# introduced it, but it carries the project dir too: it is "everything that
# is not packaged with bird".)
USER_THEMES_DIRS = [Path(".bird") / "design-themes", Path.home() / ".bird" / "design-themes"]

# loaded themes, keyed by the resolved md path — mutate._bridge_script's
# pattern, but by path rather than name: a project override that appears
# mid-session resolves to its own entry instead of serving the builtin the
# name first hit. A file read once sticks for the process.
_CACHE: dict[str, dict[str, str]] = {}

# ATTRIBUTION.md is provenance for the vendored builtins, not a theme — a bare
# file of this name (any casing) is skipped by discovery in both directions.
_RESERVED_BARE = "attribution.md"


def _theme_dirs(repo_root: Path | None = None) -> list[Path]:
    """Theme dirs, highest precedence first."""
    project, user = USER_THEMES_DIRS
    root = repo_root if repo_root is not None else Path.cwd()
    return [root / project, user, THEMES_DIR]


def _entry(d: Path, name: str) -> tuple[Path, Path | None] | None:
    """The (md, css) files for theme `name` in theme dir `d`, or None.

    A directory holding THEME.md or DESIGN.md beats a bare <name>.md file in
    the same dir — the dir is the fuller form and the only one that can carry
    tokens.css. A bare file has no css (None), which load_theme reads as "".
    """
    sub = d / name
    if sub.is_dir():
        for md_name in ("THEME.md", "DESIGN.md"):
            md = sub / md_name
            if md.is_file():
                return md, sub / "tokens.css"
    bare = d / f"{name}.md"
    if bare.name.lower() != _RESERVED_BARE and bare.is_file():
        return bare, None
    return None


# list_themes results, keyed by the theme dirs' paths plus their mtimes: the
# state push asks on every mutation, and a directory scan per push is a scan
# too many. Adding or removing a theme touches its dir's mtime, so a new
# theme is seen on the next ask with no invalidation API to forget.
_LIST_CACHE: dict[tuple, list[str]] = {}


def _dir_stamp(d: Path) -> int:
    """A change detector for one theme dir: its mtime, or -1 when absent."""
    try:
        return d.stat().st_mtime_ns
    except OSError:
        return -1


def list_themes(repo_root: Path | None = None) -> list[str]:
    """Every theme name, merged with precedence (a name is listed once, from
    its winning dir) and sorted for display. A directory counts as a theme
    only if it holds THEME.md or DESIGN.md — a stray empty dir is not
    offerable. A bare <name>.md file counts as the theme named by its stem."""
    dirs = _theme_dirs(repo_root)
    key = (tuple(str(d) for d in dirs), tuple(_dir_stamp(d) for d in dirs))
    cached = _LIST_CACHE.get(key)
    if cached is not None:
        return list(cached)
    seen: set[str] = set()
    for d in dirs:
        if not d.is_dir():
            continue
        for p in sorted(d.iterdir()):
            if p.is_dir():
                if (p / "THEME.md").is_file() or (p / "DESIGN.md").is_file():
                    seen.add(p.name)
            elif p.suffix == ".md" and p.name.lower() != _RESERVED_BARE:
                seen.add(p.stem)
    _LIST_CACHE[key] = sorted(seen)
    return list(_LIST_CACHE[key])


def load_theme(name: str, repo_root: Path | None = None) -> dict[str, str]:
    """One theme as {"name", "md", "css", "motion", "digest"} — the brief
    (THEME.md, else DESIGN.md, else the bare file's content), tokens.css ("" when
    the winner is a bare file or ships none) with the alias block appended so
    both token vocabularies resolve, the motion recipe (motion.md, "" when the
    theme has none), and the digest the designer is handed on set_theme. Read
    from the first dir that has it. Raises FileNotFoundError naming the theme
    and listing the known ones, so a typo is answerable without a second call."""
    key = str(name or "").strip()
    for d in _theme_dirs(repo_root):
        entry = _entry(d, key)
        if entry is None:
            continue
        md_path, css_path = entry
        cache_key = str(md_path)
        cached = _CACHE.get(cache_key)
        if cached is not None:
            return cached
        try:
            md = md_path.read_text(encoding="utf-8")
            css = (css_path.read_text(encoding="utf-8")
                   if css_path is not None and css_path.is_file() else "")
        except OSError:
            continue  # unreadable entry: fall through to a lower-precedence copy
        motion = ""
        if css_path is not None:
            motion_path = css_path.parent / "motion.md"
            if motion_path.is_file():
                try:
                    motion = motion_path.read_text(encoding="utf-8")
                except OSError:
                    motion = ""
        css = with_aliases(css)
        theme = {"name": key, "md": md, "css": css, "motion": motion,
                 "digest": digest(key, md, css, motion)}
        _CACHE[cache_key] = theme
        return theme
    raise FileNotFoundError(
        f"no theme {key!r} (known: {', '.join(list_themes(repo_root)) or 'none'})"
    )

# One line per theme, for a picker row. Keyed like _CACHE, by resolved path.
_BLURB_CACHE: dict[str, str] = {}
# enough of the file to hold the front matter of either shape, and no more
_BLURB_BYTES = 1600
# the picker's description budget: one line at a 380px column
_BLURB_CHARS = 72


def _clip(text: str) -> str:
    """Trimmed to a picker row's worth, on a word boundary."""
    line = " ".join(text.split())
    if len(line) <= _BLURB_CHARS:
        return line
    cut = line[:_BLURB_CHARS].rsplit(" ", 1)[0]
    return cut.rstrip(",;:.") + "…"


def _first_sentence(text: str) -> str:
    """Up to the first full stop. Prose needs the cut; a written tagline does
    not — "Project management." alone says nothing the name did not."""
    line = " ".join(text.split())
    stop = line.find(". ")
    if stop == -1 and line.endswith("."):
        stop = len(line) - 1
    return line if stop == -1 else line[: stop + 1]


def describe(name: str, repo_root: Path | None = None) -> str:
    """A theme in one line, for the design-system picker.

    The vendored packages open with a `> Category:` block whose second line is
    the tagline ("Project management. Ultra-minimal, precise, purple accent.").
    The hand-written ones open with `## Mood / Feel` and a paragraph. Take
    whichever is there, trimmed to a row's worth — a description that wraps to
    three lines stops being the consequence and becomes prose.
    """
    key = str(name or "").strip()
    for d in _theme_dirs(repo_root):
        entry = _entry(d, key)
        if entry is None:
            continue
        md_path, _ = entry
        cached = _BLURB_CACHE.get(str(md_path))
        if cached is not None:
            return cached
        try:
            with md_path.open("r", encoding="utf-8") as fh:
                head = fh.read(_BLURB_BYTES)
        except OSError:
            continue
        quoted = [ln[1:].strip() for ln in head.splitlines() if ln.startswith(">")]
        tagline = next((q for q in quoted if not q.lower().startswith("category:")), "")
        if tagline:
            blurb = _clip(tagline)
        else:
            body: list[str] = []
            for ln in head.splitlines():
                stripped = ln.strip()
                if stripped.startswith("#") or (not stripped and not body):
                    continue
                if not stripped:
                    break
                body.append(stripped)
            blurb = _clip(_first_sentence(" ".join(body))) if body else ""
        _BLURB_CACHE[str(md_path)] = blurb
        return blurb
    return ""


# ---- the two token vocabularies ----
#
# The six hand-written themes say --text / --space-section / --radius /
# --max-width; the ten vendored ones say --fg / --section-y-desktop /
# --radius-md / --container-max. The instructions and the skill cite the
# first set, so a model styling an Apple board with var(--space-section) got
# nothing. Rather than rewrite sixteen files, every loaded css carries an
# alias block defining whichever half is missing in terms of the one present.
TOKEN_ALIASES = (
    ("text", "fg"),
    ("text-secondary", "fg-2"),
    ("space-section", "section-y-desktop"),
    ("radius", "radius-md"),
    ("max-width", "container-max"),
)
_TOKEN_DEF = re.compile(r"--([a-zA-Z0-9_-]+)\s*:")


def defined_tokens(css: str) -> set[str]:
    """Every custom property the css defines (any block, not only :root)."""
    return set(_TOKEN_DEF.findall(css or ""))


def alias_block(css: str) -> str:
    """The `:root` block that makes the missing half of each alias pair
    resolve, or "" when nothing is missing — so applying it twice adds
    nothing the second time."""
    have = defined_tokens(css)
    lines: list[str] = []
    for a, b in TOKEN_ALIASES:
        if a in have and b not in have:
            lines.append(f"  --{b}: var(--{a});")
        elif b in have and a not in have:
            lines.append(f"  --{a}: var(--{b});")
    # --muted is the vendored name for the hand-written --text-secondary
    if "muted" not in have:
        src = next((n for n in ("text-secondary", "fg-2") if n in have), None)
        if src:
            lines.append(f"  --muted: var(--{src});")
    if not lines:
        return ""
    return ("\n/* bird: token aliases — both vocabularies resolve in every theme */\n"
            ":root {\n" + "\n".join(lines) + "\n}\n")


def with_aliases(css: str) -> str:
    block = alias_block(css)
    return (css or "") + block if block else (css or "")


# ---- the digest: what the designer is told when a theme lands ----
#
# The brief is 4-11k tokens in the vendored themes and only the critic ever
# read it; the designer got "applied to N artboards". The digest is the part
# a model can hold while writing a document: the token names with values,
# the Do / Don't lists, the type hierarchy, the quick color reference, the
# motion recipe — ~700 tokens.
_DIGEST_CHARS = 3600
_MAX_TOKENS = 32
_MAX_BULLETS = 6
_MAX_ROWS = 8
_MOTION_CHARS = 800
_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_ROOT_BLOCK = re.compile(r":root\s*\{(.*?)\}", re.S)
_TOKEN_LINE = re.compile(r"--([a-zA-Z0-9_-]+)\s*:\s*([^;]+);")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
# the order tokens are listed in, most-used first; the rest follow as found
_PRIORITY = (
    "bg", "surface", "surface-warm", "fg", "text", "fg-2", "text-secondary",
    "muted", "meta", "border", "border-soft", "accent", "accent-on",
    "accent-hover", "accent-active", "font-display", "font-body", "font-mono",
    "radius", "radius-sm", "radius-md", "radius-lg", "radius-pill",
    "space-section", "section-y-desktop", "max-width", "container-max",
    "tracking-display", "text-4xl", "text-3xl", "text-2xl", "text-xl",
    "motion-base", "motion-fast", "ease-standard", "elev-raised", "focus-ring",
)


def _token_lines(css: str) -> list[str]:
    # comments first: the vendored files open with a prose block that quotes
    # a literal ":root { … }", which is what the block regex found instead of
    # the real one
    bare = _COMMENT.sub("", css or "")
    m = _ROOT_BLOCK.search(bare)
    body = m.group(1) if m else bare
    seen: dict[str, str] = {}
    for name, value in _TOKEN_LINE.findall(body):
        seen.setdefault(name, " ".join(value.split()))
    order = [n for n in _PRIORITY if n in seen] + [n for n in seen if n not in _PRIORITY]
    return [f"--{n}: {seen[n]}" for n in order[:_MAX_TOKENS]]


def _sections(md: str) -> list[tuple[str, list[str]]]:
    """(heading text, body lines) per heading, in document order. Lines
    before the first heading are dropped — a title, a quote block."""
    out: list[tuple[str, list[str]]] = []
    for line in (md or "").splitlines():
        m = _HEADING.match(line)
        if m:
            out.append((m.group(2), []))
        elif out:
            out[-1][1].append(line)
    return out


def _norm(heading: str) -> str:
    return re.sub(r"[^a-z ]", "", heading.lower().replace("\u2019", "").replace("'", ""))


def _bullets(lines: list[str], cap: int = _MAX_BULLETS) -> list[str]:
    found = [ln.strip() for ln in lines if ln.strip().startswith(("-", "*"))]
    return found[:cap]


def _table(lines: list[str], cap: int = _MAX_ROWS) -> list[str]:
    rows = [ln.strip() for ln in lines if ln.strip().startswith("|")]
    return rows[: cap + 2] if rows else []  # header + separator + cap rows


def digest(name: str, md: str, css: str, motion: str = "") -> str:
    """The theme in ~700 tokens, for the designer. Built heuristically from
    whichever of the two brief shapes this theme has (vendored DESIGN.md with
    Do/Don't and Hierarchy sections; hand-written THEME.md with Mood, Color
    Roles, Typography, Do's and Don'ts) — a section that is not there is
    simply not in the digest."""
    sections: list[list[str]] = []  # in priority order; each fits or is skipped
    tokens = _token_lines(css)
    if tokens:
        sections.append(
            ["## Tokens — style with var(--name); never invent a color outside them"] + tokens)
    do: list[str] = []
    dont: list[str] = []
    both: list[str] = []
    type_rows: list[str] = []
    type_bullets: list[str] = []
    colors: list[str] = []
    for heading, body in _sections(md):
        h = _norm(heading).strip()
        if h in ("do", "dos"):
            do = do or _bullets(body)
        elif h in ("dont", "donts", "donot", "donots", "do nots"):
            dont = dont or _bullets(body)
        elif "dos and donts" in h or h in ("dos donts", "do and dont"):
            both = both or _bullets(body, _MAX_BULLETS * 2)
        elif "hierarch" in h:
            type_rows = type_rows or _table(body)
        elif "typography" in h or h.startswith("type"):
            if not type_rows:
                type_rows = _table(body)
            type_bullets = type_bullets or _bullets(body, 6)
        elif "quick color" in h:
            colors = colors or _bullets(body)
    if dont:
        sections.append(["## Don't"] + dont)
    if do:
        sections.append(["## Do"] + do)
    if both and not (do or dont):
        sections.append(["## Do's and Don'ts"] + both)
    if type_rows:
        sections.append(["## Type scale"] + type_rows)
    elif type_bullets:
        sections.append(["## Type"] + type_bullets)
    motion = (motion or "").strip()
    if motion:
        clipped = motion if len(motion) <= _MOTION_CHARS else motion[:_MOTION_CHARS].rstrip() + "…"
        sections.append(["## Motion", clipped])
    if colors:
        sections.append(["## Quick color reference"] + colors)
    # whole sections, never a cut mid-line: a section that does not fit the
    # budget is left out and the smaller ones after it still get their turn
    text = f"# Theme: {name}"
    for section in sections:
        block = "\n\n" + "\n".join(section)
        if len(text) + len(block) <= _DIGEST_CHARS:
            text += block
    return text
