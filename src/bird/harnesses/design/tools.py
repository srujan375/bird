"""The design toolset: put an artboard up, read it back, edit it, undo, delete
an artboard, finalize.

Every document change goes through mutate.apply with actor="ai" — the same door
the page's inspector writes through.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ...tools import SkillTool, Tool, ToolContext, ToolError, ToolResult
from ...tools.base import resolve_under
from ...tools.files import detect_image_mime
from . import dom, mutate, themes
from .session import DesignSession, STATUS_SHOWCASE

OPS = ("set_text", "set_style", "insert", "delete", "duplicate", "move")
_ARTBOARD = {"artboard": {"type": "string", "description": "defaults to the selected artboard"}}

def _session(ctx: ToolContext) -> DesignSession:
    session = getattr(ctx, "design", None)
    if session is None:
        raise ToolError("not a design session — design tools are unavailable.")
    return session

def _guard_intake(session: DesignSession) -> None:
    """Nothing is drawn while a question the artboards depend on is unanswered.

    The page holds the same line — the composer is closed behind the picker —
    but a model reached by another route (a lead dispatch, a resumed run)
    would otherwise design in a fidelity nobody chose."""
    try:
        session.guard_intake()
    except ValueError as e:
        raise ToolError(str(e)) from e


def _artboard_id(session: DesignSession, args: dict[str, Any]) -> str:
    known = ", ".join(session.state.artboards) or "none"
    aid = str(args.get("artboard") or "").strip() or session.state.selected
    if not aid or aid not in session.state.artboards:
        raise ToolError(f"no artboard {aid!r} (known: {known}).")
    return aid

# How much of the edited subtree an edit result shows. Enough for a section's
# children; a bigger subtree points at design_read for the rest.
EDIT_OUTLINE_LINES = 40


def _scope_before(html: str, op: dict[str, Any]) -> str | None:
    """The selector of the subtree an op is about to change — the target's
    parent for element ops, the destination for insert and move — computed
    BEFORE the op runs, since a delete leaves nothing to resolve after. None
    when the op's own selector does not resolve; the refusal that follows
    names the reason."""
    root = dom.parse(html)
    name = op.get("op")
    try:
        if name in ("insert", "move"):
            dest = op.get("parent") if name == "insert" else op.get("new_parent")
            node = dom.resolve(root, str(dest)) if dest else dom._find_body(root)
            return dom._selector_path(node)
        node = dom.resolve(root, str(op.get("selector") or ""))
    except dom.DomError:
        return None
    parent = node.parent
    if parent is None or parent.tag == "#root":
        return dom._selector_path(node)
    return dom._selector_path(parent)


def _outline_after(session: DesignSession, aid: str, scope: str | None) -> str:
    """The outline of `scope` as the document stands now — what the model
    needs to keep editing without a design_read round trip, since the op it
    just made may have shifted every index path behind it."""
    if not scope:
        return ""
    html = dom.unwrap_editor(session.read_html(aid))
    if not html:
        return ""
    try:
        node = dom.resolve(dom.parse(html), scope)
    except dom.DomError:
        return ""
    lines: list[str] = []
    _outline(node, scope, 0, lines)
    body = lines[:EDIT_OUTLINE_LINES]
    if len(lines) > EDIT_OUTLINE_LINES:
        body.append(f"... {len(lines) - EDIT_OUTLINE_LINES} more — design_read with "
                    f"selector={scope!r} for the rest")
    return "\n".join(body)


def _apply(session: DesignSession, aid: str, op: dict[str, Any]) -> ToolResult:
    scope = _scope_before(session.read_html(aid), op)
    try:
        version = mutate.apply(session, aid, op, actor="ai")
    except mutate.MutationError as e:
        raise ToolError(str(e)) from e
    output = f"Applied {op.get('op')} to {aid} ({version.id})."
    outline = _outline_after(session, aid, scope)
    if outline:
        output += f"\n\nUnder {scope} now (selector, then element):\n{outline}"
    return ToolResult(output=output,
                      details={"ok": True, "artboard": aid, "version": version.id})


def _resolve_attachment(ctx: ToolContext, session: DesignSession, raw: str) -> Path:
    """Where an attachment reference in the user's message lands on disk.

    The reference the upload route returns is repo-relative — the same shape
    ingest_images rewrites named paths to — so it resolves under the repo
    root; a bare `attachments/...` name resolves against the run dir. One
    resolver, two bases, so the model never has to guess which spelling a
    reference uses."""
    candidates = [resolve_under(ctx.repo_root, raw)]
    if session.run_dir is not None:
        candidates.append(resolve_under(session.run_dir, raw))
    bases = [ctx.repo_root.resolve()]
    if session.run_dir is not None:
        bases.append(Path(session.run_dir).resolve())
    for cand in candidates:
        try:
            resolved = cand.resolve()
        except OSError:
            continue
        if not resolved.is_file():
            continue
        # the bytes go to the vision provider, so this is a read gate in
        # miniature: only files inside the repo or the session's own run dir
        # may be looked at
        if not any(b == resolved or b in resolved.parents for b in bases):
            continue
        _, is_raster = detect_image_mime(resolved)
        if not is_raster:
            raise ToolError(f"{raw!r} is not a raster image the vision model can read.")
        return resolved
    raise ToolError(
        f"no readable image at {raw!r} — pass the attachment path exactly as "
        "it appears in the user's message.")

MAX_OUTLINE_LINES = 400


class DesignCreateTool(Tool):
    name = "design_create"
    description = (
        "Put an artboard on the user's screen: one complete, self-contained HTML document "
        "(inline styles; no external CSS, JS or fonts; no build step). It is checkpointed as "
        "the artboard's first version and rendered immediately. Call it once per direction "
        "you want to show. Pass `artboard` with an existing id to REBUILD that one instead — "
        "a new version with fresh selectors, for when the direction itself is wrong. "
        "Rebuilding throws away the user's own edits; design_edit keeps them."
    )
    parameters = {"type": "object", "properties": {
        "html": {"type": "string",
                 "description": "the whole document, <!doctype html> through </html>"},
        "title": {"type": "string",
                  "description": "short name for the artboard card, e.g. 'Hero — dark'"},
        "artboard": {"type": "string",
                     "description": "an existing artboard id to rebuild; omit to add a new one"},
    }, "required": ["html"], "additionalProperties": False}

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        session = _session(ctx)
        if session.state.finalized:
            raise ToolError("the design was finalized — the session is read-only now.")
        if session.status == STATUS_SHOWCASE:
            # the canvas is gone mid-showcase: a new artboard has nowhere to
            # land, and a rebuild would pull the full-bleed view out from
            # under itself. Back is the door back to widening the field.
            raise ToolError(
                "the showcase phase owns the screen — design_create is refused "
                "until Back returns the board.")
        _guard_intake(session)
        html = str(args.get("html") or "").strip()
        if not html:
            raise ToolError("design_create needs the artboard's html.")
        aid = str(args.get("artboard") or "").strip()
        if aid:
            version = session.regenerate(_artboard_id(session, {"artboard": aid}), html)
            output = (f"Rebuilt {aid} as {version.id}. Selectors are fresh — "
                      "design_read before editing it.")
            details: dict[str, Any] = {"ok": True, "artboard": aid, "version": version.id}
        else:
            title = str(args.get("title") or "").strip() or "Artboard"
            board = session.add_artboard(html, title)
            output = f"Artboard {board.id} ({board.title!r}) is on the user's screen at v1."
            details = {"ok": True, "artboard": board.id, "version": board.current}
        # The auto-critique rode along with the create (the post-create hook on
        # the session): either the fix-list a second model returned, or the
        # one-line reason it could not run. Either way it reaches the designer
        # as tool output — the loop has to be structural, not volunteered.
        note = session.last_critique_note
        if note:
            details["critique"] = note
            output += "\n\n" + (
                note if note.startswith("not critiqued")
                else f"A second model critiqued the render — answer it with "
                     f"design_edit ops:\n{note}")
        # The document is checkpointed; the 16 KB of html in this call's
        # arguments would otherwise ride in every later turn's context. The
        # runner swaps the argument for this stub once the result lands.
        details["elide_arguments"] = {"html": _elided(
            "html", len(html), details["artboard"], details["version"],
            "design_read shows the document as it stands")}
        return ToolResult(output=output, details=details)


def _elided(field: str, n: int, aid: str, version: str, how: str) -> str:
    return f"[{field} elided: {n} chars, checkpointed as {aid} {version} — {how}]"


def _outline(node: dom.Node, selector: str, depth: int, out: list[str]) -> None:
    """One line per element: its selector, what it is, and — for leaves — its
    text. This is what the model derives edit selectors from."""
    label = node.tag
    if node.attrs.get("id"):
        label += "#" + node.attrs["id"]
    if node.attrs.get("class"):
        label += "." + ".".join(node.attrs["class"].split())
    kids = node.child_elements()
    if not kids:
        text = " ".join(node.text_content().split())
        if text:
            label += f'  "{text[:70]}"'
    out.append(f"{selector}{'  ' * (depth + 1)}{label}")
    for i, kid in enumerate(kids):
        # the index counts EVERY element child, skipped ones included, or the
        # paths would not match the ones dom.resolve walks
        if kid.tag in dom.UNSELECTABLE and kid.tag != "html":
            continue
        _outline(kid, f"{selector}>{i}" if selector else str(i), depth + 1, out)


class DesignReadTool(Tool):
    name = "design_read"
    description = (
        "Read an artboard as it stands now — where the selectors for design_edit come from. "
        "The default outline gives one line per element: its selector path, tag, id/class and, "
        "for leaves, its text. format='html' gives the markup instead. `selector` scopes "
        "either one to a subtree (an index path or #id). Read before editing anything you "
        "did not just write: the user edits this document too, and every insert or delete "
        "shifts the index paths after it (#id selectors survive that)."
    )
    parameters = {"type": "object", "properties": {
        "format": {"type": "string", "enum": ["outline", "html"]},
        "selector": {"type": "string", "description": "scope to one subtree (default: the whole document)"},
        **_ARTBOARD,
    }, "additionalProperties": False}

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        session = _session(ctx)
        aid = _artboard_id(session, args)
        # unwrapped: the bridge is a hundred lines of harness plumbing the
        # designer neither wrote nor may edit
        html = dom.unwrap_editor(session.read_html(aid))
        if not html:
            raise ToolError(f"artboard {aid!r} has no document yet.")
        root = dom.parse(html)
        selector = str(args.get("selector") or "").strip()
        try:
            nodes = [(dom.resolve(root, selector), selector)] if selector else [
                (n, str(i)) for i, n in enumerate(root.child_elements())
            ]
        except dom.DomError as e:
            raise ToolError(str(e)) from e
        version = session.state.artboards[aid].current
        if str(args.get("format") or "outline") == "html":
            body = "".join(n.outer_html() for n, _ in nodes)
            return ToolResult(output=body,
                              details={"ok": True, "artboard": aid, "version": version})
        lines: list[str] = []
        for node, sel in nodes:
            _outline(node, sel, 0, lines)
        clipped = len(lines) > MAX_OUTLINE_LINES
        head = [f"{aid} at {version} — selector, then element:"]
        body = lines[:MAX_OUTLINE_LINES]
        if clipped:
            body.append(f"... {len(lines) - MAX_OUTLINE_LINES} more elements — "
                        "pass `selector` to read one subtree")
        return ToolResult(output="\n".join(head + body),
                          details={"ok": True, "artboard": aid, "version": version})


# One op's arguments — the same shape as a single-op call, and each item of
# a batched `ops` array.
OP_SHAPES = ("set_text{selector,text} · set_style{selector,props} · "
             "insert{parent,position,html} · delete{selector} · duplicate{selector} · "
             "move{selector,parent,index}")
_OP_PROPERTIES = {
    "op": {"type": "string", "enum": list(OPS)},
    "selector": {"type": "string",
                 "description": "element-index path from design_read, e.g. 0>1>3, "
                                "or #id when the element has a unique id"},
    "text": {"type": "string"},
    "props": {"type": "object", "additionalProperties": {"type": "string"}},
    "html": {"type": "string"},
    "parent": {"type": "string", "description": "insert/move destination (default <body>)"},
    "position": {"type": "integer"},
    "index": {"type": "integer"},
}


def _build_op(item: dict[str, Any]) -> dict[str, Any]:
    """One op dict for dom.py from one op's arguments. Raises ToolError on a
    shape it cannot use, naming the op→argument mapping."""
    name = str(item.get("op") or "")
    if name not in OPS:
        raise ToolError(f"unknown op {name!r} — the ops are {OP_SHAPES}.")
    op: dict[str, Any] = {"op": name, "selector": str(item.get("selector") or "").strip()}
    if name != "insert" and not op["selector"]:
        raise ToolError(f"{name} needs a selector — {OP_SHAPES}.")
    op.update({k: item[k] for k in ("text", "html", "props", "position", "index")
               if item.get(k) is not None})
    if item.get("parent") is not None:  # dom.py's move says `new_parent`; insert `parent`
        op["new_parent" if name == "move" else "parent"] = item["parent"]
    return op


class DesignEditTool(Tool):
    name = "design_edit"
    description = (
        "Edit an artboard's document. Ops: " + OP_SHAPES + ". set_text sets a leaf "
        "element's text; set_style merges css props (\"\" clears one); insert puts html "
        "into parent at position; move puts selector into parent at index (both default "
        "to the end). One op per call with the fields at the top level, or several in "
        "`ops` — applied in order, one checkpointed version each, stopping at the first "
        "refusal and reporting which landed. Selectors are element-index paths for the "
        "CURRENT version ('0>1>2': each segment the index among that parent's element "
        "children from the document root, so <html> is '0' and <body> is usually '0>1') "
        "or '#id' when the element carries a unique id — ids survive the inserts and "
        "deletes that shift index paths. Read them with design_read; every result also "
        "shows the edited subtree's fresh outline. Undo steps back one op."
    )
    parameters = {"type": "object", "properties": {
        **_OP_PROPERTIES,
        "ops": {"type": "array",
                "description": "a batch: several ops in one call, applied in order; "
                               "one version each",
                "items": {"type": "object", "properties": dict(_OP_PROPERTIES),
                          "required": ["op"], "additionalProperties": False}},
        **_ARTBOARD,
    }, "additionalProperties": False}

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        session = _session(ctx)
        batch = args.get("ops")
        if isinstance(batch, list) and batch:
            items = batch
        elif args.get("op"):
            items = [args]
        else:
            raise ToolError(
                f"design_edit needs `op` (one edit) or `ops` (a batch) — {OP_SHAPES}.")
        aid = _artboard_id(session, args)
        if len(items) == 1 and items[0] is args:
            return _apply(session, aid, _build_op(args))
        applied: list[tuple[dict[str, Any], str]] = []
        scope: str | None = None
        failed: dict[str, Any] | None = None
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                failed = {"index": i, "op": item, "error": "each op is an object"}
                break
            try:
                op = _build_op(item)
                scope = _scope_before(session.read_html(aid), op) or scope
                version = mutate.apply(session, aid, op, actor="ai")
            except (ToolError, mutate.MutationError) as e:
                failed = {"index": i, "op": item, "error": str(e)}
                break
            applied.append((op, version.id))
        if not applied:
            assert failed is not None
            raise ToolError(f"op 1 ({_op_label(failed['op'])}) was refused: {failed['error']} "
                            "— nothing was applied.")
        first, last = applied[0][1], applied[-1][1]
        span = last if first == last else f"{first} → {last}"
        done = ", ".join(_op_label(op) for op, _ in applied)
        output = f"Applied {len(applied)} of {len(items)} ops to {aid} ({span}): {done}."
        if failed is not None:
            left = len(items) - failed["index"] - 1
            output += (f"\nStopped at op {failed['index'] + 1} "
                       f"({_op_label(failed['op'])}): {failed['error']}")
            if left:
                output += (f" The {left} op{'s' if left != 1 else ''} after it "
                           f"{'were' if left != 1 else 'was'} not applied — fix that "
                           "one and resend from it.")
        outline = _outline_after(session, aid, scope)
        if outline:
            output += f"\n\nUnder {scope} now (selector, then element):\n{outline}"
        return ToolResult(output=output, details={
            "ok": True, "artboard": aid, "version": last,
            "applied": [{"op": op.get("op"), "selector": op.get("selector", ""), "version": v}
                        for op, v in applied],
            "failed": failed, "remaining": (len(items) - failed["index"] - 1) if failed else 0,
        })


def _op_label(op: Any) -> str:
    if not isinstance(op, dict):
        return "?"
    sel = op.get("selector") or op.get("parent") or ""
    return f"{op.get('op') or '?'} [{sel}]" if sel else str(op.get("op") or "?")

class DesignUndoTool(Tool):
    name = "design_undo"
    description = ("Step the artboard back one checkpoint — yours or the user's, one shared "
                   "stack. The only way to take back an edit; there is no redo.")
    parameters = {"type": "object", "properties": dict(_ARTBOARD), "additionalProperties": False}

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        session = _session(ctx)
        aid = _artboard_id(session, args)
        try:
            version = mutate.undo(session, aid)
        except mutate.MutationError as e:
            raise ToolError(str(e)) from e
        return ToolResult(output=f"Undid {aid} — now at {version.id}.",
                          details={"ok": True, "artboard": aid, "version": version.id})

class DesignDeleteArtboardTool(Tool):
    name = "design_delete_artboard"
    description = (
        "Delete an artboard from the board OUTRIGHT — the card, its document "
        "and its whole version history are gone, and this is NOT undoable "
        "(design_undo steps document ops; it cannot resurrect a deleted "
        "artboard). This is the board-level delete: the `delete` op inside "
        "design_edit removes one element within a document, this removes the "
        "whole artboard. Use it when a direction is dead — never gut an "
        "artboard to an empty document instead; a gutted artboard leaves a "
        "blank card that still clutters the board. The page asks the user to "
        "confirm before its own delete control fires."
    )
    parameters = {"type": "object", "properties": dict(_ARTBOARD),
                  "additionalProperties": False}

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        session = _session(ctx)
        if session.state.finalized:
            raise ToolError("the design was finalized — the session is read-only now.")
        if session.status == STATUS_SHOWCASE:
            # the same line the session raises: the phase belongs to the
            # elevated artboard, and Back is the clean path out of it
            raise ToolError(
                "the showcase phase owns the screen — Back ends it before an "
                "artboard can be deleted.")
        aid = _artboard_id(session, args)
        session.delete_artboard(aid)
        remaining = len(session.state.artboards)
        output = f"Deleted {aid}."
        if remaining:
            output += (f" The board holds {remaining} "
                       f"artboard{'s' if remaining != 1 else ''}.")
        else:
            output += " The board is empty — design_create can start again."
        return ToolResult(output=output,
                          details={"ok": True, "artboard": aid, "deleted": True})

class DesignFinalizeTool(Tool):
    name = "design_finalize"
    description = ("Record the chosen artboard as the design and close the session — read-only "
                   "after. Only when the user has said they are done; finalizing is theirs to ask for.")
    parameters = {"type": "object", "properties": dict(_ARTBOARD), "additionalProperties": False}

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        session = _session(ctx)
        aid = _artboard_id(session, args)  # validates; finalize cannot miss after
        session.finalize(aid)
        return ToolResult(output=f"Finalized {aid}. The session is read-only now.",
                          details={"ok": True, "finalized": aid})

class DesignStatusTool(Tool):
    name = "design_status"
    description = ("Where the session stands: the artboards, their current versions, which is "
                   "selected, and whether the design is finalized. Read-only.")
    parameters = {"type": "object", "properties": {}, "additionalProperties": False}

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        state = _session(ctx).state
        lines = [f"prompt: {state.prompt or '(none)'}"]
        for aid, ab in state.artboards.items():
            sel = " (selected)" if aid == state.selected else ""
            vs = ", ".join(v.id for v in ab.versions) or "no versions"
            lines.append(f"{aid}: {ab.title}{sel} — at {ab.current or '?'} [{vs}]")
        if state.finalized:
            lines.append(f"finalized: {state.finalized_artboard}")
        n, ai = len(state.op_log), sum(1 for e in state.op_log if e.get("by") == "ai")
        lines.append(f"op log: {n} entr{'y' if n == 1 else 'ies'} ({ai} by ai)")
        return ToolResult(output="\n".join(lines), details={"ok": True, "artboards": list(state.artboards)})

class DesignThemesTool(Tool):
    name = "design_themes"
    description = ("List the installed design themes, one line each, and which is "
                   "active. A theme is a design brief plus a tokens.css the harness "
                   "injects into every artboard as CSS variables. The workbench already "
                   "asked the user which to use; call this only when the opening message "
                   "says 'unspecified', then pick one and design_set_theme it.")
    parameters = {"type": "object", "properties": {}, "additionalProperties": False}

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        session = _session(ctx)
        names = themes.list_themes(ctx.repo_root)
        lines = [f"active theme: {session.theme or '(none)'}"]
        for name in names:
            blurb = themes.describe(name, ctx.repo_root)
            mark = " (active)" if name == session.theme else ""
            lines.append(f"- {name}{mark}" + (f" — {blurb}" if blurb else ""))
        return ToolResult(output="\n".join(lines),
                          details={"ok": True, "themes": names, "active": session.theme})

class DesignSetThemeTool(Tool):
    name = "design_set_theme"
    description = ("Adopt a theme by name (from design_themes) and restyle every existing "
                   "artboard with its tokens — each gets a new 'themed' version. Artboards "
                   "created afterwards pick the theme up automatically. Style with the "
                   "injected variables (var(--accent), var(--font-display), ...) rather "
                   "than inventing colors, and never violate the theme's Do-nots.")
    parameters = {"type": "object", "properties": {
        "theme": {"type": "string", "description": "a theme name from design_themes"},
    }, "required": ["theme"], "additionalProperties": False}

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        session = _session(ctx)
        if session.state.finalized:
            raise ToolError("the design was finalized — the session is read-only now.")
        if session.status == STATUS_SHOWCASE:
            # a theme pass restyles every artboard; mid-showcase the user is
            # looking at one of them full-bleed and the rest are off-screen
            raise ToolError(
                "the showcase phase owns the screen — design_set_theme is "
                "refused until Back returns the board.")
        _guard_intake(session)
        name = str(args.get("theme") or "").strip()
        if not name:
            raise ToolError("design_set_theme needs a theme name — design_themes lists them.")
        try:
            theme = session.set_theme(name)
        except FileNotFoundError as e:
            raise ToolError(str(e)) from e
        themed: list[str] = []
        for aid in session.state.artboards:
            html = session.read_html(aid)
            if not html:
                continue
            session.write_version(aid, dom.inject_theme(html, theme["css"]),
                                  "themed", note=f"theme: {theme['name']}")
            themed.append(aid)
        if themed:  # one push for the batch; write_version does not push itself
            session.touched("artboard", themed[0])
        else:
            session.touched()
        # the digest, not a receipt: the designer used to hear "applied to N
        # artboards" and nothing about the tokens it was now expected to use,
        # while the critic read the whole brief and flagged what the designer
        # was never told
        head = (f"Theme {theme['name']!r} applied to {len(themed)} "
                f"artboard{'s' if len(themed) != 1 else ''}. Its tokens are injected "
                "into every artboard; style with them.")
        digest = theme.get("digest") or ""
        return ToolResult(
            output=head + (f"\n\n{digest}" if digest else ""),
            details={"ok": True, "theme": theme["name"], "artboards": themed})

class DesignPlanTool(Tool):
    name = "design_plan"
    description = (
        "Submit the plan BEFORE the first design_create: the direction, the "
        "palette use, the type scale, the composition per section, and one "
        "named Signature element. A second model critiques it against the "
        "house never-list and that critique comes back as this tool's output; "
        "the plan is stored either way and joins the rubric every later render "
        "critique judges the build against. Call it once, before designing."
    )
    parameters = {"type": "object", "properties": {
        "plan": {"type": "string",
                 "description": "direction, palette use, type scale, composition "
                                "per section, one named Signature element"},
    }, "required": ["plan"], "additionalProperties": False}

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        session = _session(ctx)
        if session.state.finalized:
            raise ToolError("the design was finalized — the session is read-only now.")
        _guard_intake(session)
        plan = str(args.get("plan") or "").strip()
        if not plan:
            raise ToolError("design_plan needs the plan text.")
        session.state.plan = plan
        session.touched()
        # The plan's value does not depend on the critic existing: it is stored
        # either way, and design-craft's self-review checklist is the fallback
        # judge. A critic that is configured but fails degrades the same way —
        # the plan is the deliverable here, the critique the bonus.
        if session.critic is None:
            return ToolResult(
                output="Plan stored. No vision model is configured, so it was "
                       "not critiqued — run the design-craft skill's checklist "
                       "against it yourself before design_create.",
                details={"ok": True, "plan": "stored", "critique": None})
        try:
            note = session.critic.critique_plan(session, plan)
        except Exception as e:
            return ToolResult(
                output=f"Plan stored. The critic call failed ({e}) — run the "
                       "design-craft skill's checklist against it yourself.",
                details={"ok": True, "plan": "stored", "critique": None})
        if not note:
            return ToolResult(
                output="Plan stored. The critic returned nothing — run the "
                       "design-craft skill's checklist against it yourself.",
                details={"ok": True, "plan": "stored", "critique": None})
        session.state.plan_critique = note
        session.touched()  # the page shows the plan's critique beside the board
        return ToolResult(
            output=f"Plan stored. A second model's critique of it — fold this "
                   f"into the plan before design_create:\n\n{note}",
            details={"ok": True, "plan": "stored", "critique": note})


class DesignLookTool(Tool):
    name = "design_look"
    description = (
        "Look at an image the user attached to a message — a pasted or dropped "
        "screenshot arrives in their text as a file path. Runs the vision model "
        "on it and returns what it sees, so 'here's a screenshot of what's "
        "wrong' becomes something you can act on. Pass the path exactly as it "
        "appears in the message; `question` focuses the look on one aspect."
    )
    parameters = {"type": "object", "properties": {
        "image": {"type": "string",
                  "description": "path to the attached image, as it appears in the user's message"},
        "question": {"type": "string",
                     "description": "what to look for (default: describe everything)"},
    }, "required": ["image"], "additionalProperties": False}

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        session = _session(ctx)
        # degrade like design_critique: an explicit ask for eyes that cannot
        # see is a named error, not a silent nothing
        critic = session.critic
        if critic is None:
            raise ToolError(
                "no vision model configured — point the 'vision' alias in "
                "models.json at a multimodal model")
        raw = str(args.get("image") or "").strip()
        if not raw:
            raise ToolError("design_look needs the image path from the user's message.")
        path = _resolve_attachment(ctx, session, raw)
        try:
            note = critic.describe_image(path, str(args.get("question") or ""))
        except Exception as e:  # a dead critic is an error here: the model asked
            raise ToolError(f"the vision call failed ({e})") from e
        if not note:
            raise ToolError("the vision model returned nothing")
        return ToolResult(
            output=f"What the image shows ({path.name}):\n\n{note}",
            details={"ok": True, "image": str(path)})


class DesignCritiqueTool(Tool):
    name = "design_critique"
    description = (
        "Get a second model's eyes on an artboard as it renders right now: a "
        "screenshot is captured from the user's page and judged against the "
        "house never-list, the active theme and your stored plan. Returns a "
        "fix-list citing selectors — answer it with design_edit ops. Call it "
        "after a large edit batch or a rebuild; every design_create is "
        "already critiqued automatically. When the user asks you to look at "
        "an artboard, this is how you look — pass their question and the "
        "answer comes first, before the fix-list."
    )
    parameters = {"type": "object", "properties": {
        "question": {"type": "string",
                     "description": "what the user wants to know about the render, "
                                    "in their words; answered before the fix-list"},
        **_ARTBOARD,
    }, "additionalProperties": False}

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        session = _session(ctx)
        if session.state.finalized:
            raise ToolError("the design was finalized — the session is read-only now.")
        _guard_intake(session)
        aid = _artboard_id(session, args)
        question = str(args.get("question") or "").strip() or None
        # Unlike the automatic hook, an explicit call that cannot run is a
        # ToolError naming what is missing: the model asked for eyes and
        # should know there are none, not silently receive nothing.
        try:
            note = session.critique_now(aid, question=question)
        except ValueError as e:
            raise ToolError(str(e)) from e
        current = session.state.artboards[aid].current
        return ToolResult(
            output=f"Vision critique of {aid} at {current} — answer it with "
                   f"design_edit ops:\n\n{note}",
            details={"ok": True, "artboard": aid, "version": current,
                     "critique": note})


class DesignShowcaseTool(Tool):
    name = "design_showcase"
    description = (
        "Enter the showcase phase with the artboard the user picked: the "
        "board is gone and that one artboard fills the screen as the finished "
        "result, ready to be animated with design_polish and handed off. The "
        "page's own pick button enters the same phase — call this when the "
        "pick arrives in chat. Edits lock to the elevated artboard and "
        "design_create is refused until the user goes Back."
    )
    parameters = {"type": "object", "properties": dict(_ARTBOARD),
                  "additionalProperties": False}

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        session = _session(ctx)
        if session.state.finalized:
            raise ToolError("the design was finalized — the session is read-only now.")
        aid = _artboard_id(session, args)
        try:
            turn = session.showcase(aid)
        except ValueError as e:
            raise ToolError(str(e)) from e
        # the returned turn is the polish brief: in this tool path the model
        # is already mid-turn and reads it as instructions, so no dispatch
        return ToolResult(output=turn,
                          details={"ok": True, "artboard": aid, "showcase": True})


class DesignPolishTool(Tool):
    name = "design_polish"
    description = (
        "The showcase phase's motion pass: css (required) plus an optional "
        "inline script, injected into the elevated artboard as marked, "
        "idempotent nodes — a re-polish replaces the previous layer instead "
        "of stacking. The ONLY place a <script> is sanctioned, and only "
        "mid-showcase: keep it to interactions CSS cannot express. One "
        "orchestrated page-load animation max, no scroll-reveal spam — the "
        "design-craft motion rules govern. Each call checkpoints a 'polished' "
        "version (undoable) and runs the vision critique on the result."
    )
    parameters = {"type": "object", "properties": {
        "css": {"type": "string",
                "description": "the motion layer's css — keyframes, transitions, "
                               "animation-delay staggers"},
        "script": {"type": "string",
                   "description": "optional inline js for interactions css cannot "
                                  "express; runs beside the editor bridge"},
        "note": {"type": "string", "description": "what this pass adds, for the version history"},
        **_ARTBOARD,
    }, "required": ["css"], "additionalProperties": False}

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        session = _session(ctx)
        if session.state.finalized:
            raise ToolError("the design was finalized — the session is read-only now.")
        if session.status != STATUS_SHOWCASE:
            raise ToolError(
                "design_polish works only in the showcase phase — "
                "design_showcase enters it once the user has picked an artboard.")
        elevated = session.state.showcase_artboard
        aid = _artboard_id(session, args)
        if aid != elevated:
            raise ToolError(
                f"the showcase phase owns the screen — polish applies to "
                f"{elevated!r} only.")
        css = str(args.get("css") or "").strip()
        script = str(args.get("script") or "").strip()
        if not css:
            raise ToolError("design_polish needs css — the motion layer's styles.")
        if css.count("{") != css.count("}"):
            # no css parser here, and none needed: the failure that matters is
            # the truncated paste, and balanced braces catch it cheaply
            raise ToolError("the css does not parse — unbalanced braces; fix it and retry.")
        html = session.read_html(aid)
        if not html:
            raise ToolError(f"artboard {aid!r} has no document yet.")
        note = str(args.get("note") or "").strip() or "polish: motion pass"
        try:
            polished = dom.inject_motion(html, css, script)
        except dom.DomError as e:
            raise ToolError(str(e)) from e
        version = session.write_version(aid, polished, "polished", note=note)
        session.touched("artboard", aid)
        # the same post-change hook the create path has. The critic judges the
        # END STATE — the capture finishes animations before rastering — and
        # reads the choreography as text; the person in the room judges the
        # live motion. Every failure folds into a skip note: the polish lands
        # exactly as it would with no critic at all.
        critique = session.auto_critique(aid)
        details: dict[str, Any] = {"ok": True, "artboard": aid, "version": version.id}
        # same deal as design_create: the layer is checkpointed, so the call's
        # css/script need not ride in every later turn
        elide = {"css": _elided("css", len(css), aid, version.id,
                                "design_read format='html' shows it")}
        if script:
            elide["script"] = _elided("script", len(script), aid, version.id,
                                      "design_read format='html' shows it")
        details["elide_arguments"] = elide
        output = f"Polished {aid} as {version.id}."
        if critique:
            details["critique"] = critique
            output += "\n\n" + (
                critique if critique.startswith("not critiqued")
                else f"A second model critiqued the polished render — answer it "
                     f"with design_edit ops or another design_polish:\n{critique}")
        return ToolResult(output=output, details=details)


def design_edit_tools(with_kg: bool = False, with_web: bool = False) -> list[Tool]:
    """`with_kg`/`with_web` are accepted and ignored on purpose: a design
    session is a conversation about one document the user is watching, not a
    research task. Looking things up in the repo or on the web is the lead's
    job, before it dispatches here."""
    return [DesignCreateTool(), DesignReadTool(), DesignEditTool(),
            DesignUndoTool(), DesignDeleteArtboardTool(), DesignFinalizeTool(),
            DesignStatusTool(), DesignThemesTool(), DesignSetThemeTool(),
            DesignPlanTool(), DesignCritiqueTool(), DesignLookTool(),
            DesignShowcaseTool(), DesignPolishTool(), SkillTool()]