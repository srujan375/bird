"""The arch toolset: six ways to jot something down, plus the fact-finding.

The conversation is the work. These tools are the architect's notebook — they
exist so a decision made out loud survives the session and reaches whoever
builds it. None of them is a form, none of them gates anything, and none of
them is how a turn ends: a plain reply does that.

Three rules follow:

1. A tool refuses only what is *broken* — an edge to a node that cannot exist,
   an approach greyed without saying why, a session already handed off.
   Thinness is never a refusal; it comes back as an observation the architect
   can raise in its next turn, or ignore because it does not matter here.
2. Everything batches. Putting a shape on the board is one `canvas` call with
   six nodes and five edges, not eleven calls. The user is waiting.
3. Missing edge endpoints auto-create as stubs. You are at a whiteboard.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from ...tools import Tool, ToolContext, ToolError, ToolResult
from ...tools.files import LsTool, ReadImageTool, ReadTool
from ...tools.kg_query import KgQueryTool
from ...tools.skill import SkillTool
from ...tools.web import WebFetchTool, WebSearchTool
from . import derive
from .session import ArchSession
from .state import (
    DEPTHS,
    EDGE_KINDS,
    KINDS,
    STATUSES,
    Approach,
    Decision,
    Edge,
    Node,
    Option,
    Question,
    slug,
)

NOTICES_SHOWN = 2

# fields a `canvas` node spec may set directly, all optional, all prose
_NODE_FIELDS = ("kind", "responsibility", "tech", "depth", "detail", "notes", "status")


# ------------------------------------------------------------- plumbing


def _session(ctx: ToolContext) -> ArchSession:
    if ctx.arch is None:
        raise ToolError("not an architecture session — arch tools are unavailable.")
    return ctx.arch


def _guard_open(session: ArchSession) -> None:
    """The only hard lock in the harness. Everything the old phase gates
    refused — deepening before approval, drawing before the brief was
    complete — is now simply allowed."""
    if session.state.handed_off:
        raise ToolError("the design was handed off — the session is closed.")


def _check(validate, *args) -> None:
    """State-layer ValueErrors become model-visible ToolErrors verbatim."""
    try:
        validate(*args)
    except ValueError as e:
        raise ToolError(str(e)) from e


def _str(val: Any) -> str:
    return "" if val is None else str(val).strip()


def _strlist(val: Any) -> list[str]:
    if val is None:
        return []
    if isinstance(val, str):
        return [val.strip()] if val.strip() else []
    return [str(v).strip() for v in val if str(v).strip()]


def _confirm(message: str, session: ArchSession, subjects: tuple[str, ...] = ()) -> ToolResult:
    """The confirmation, plus anything the harness noticed *about what was just
    touched*.

    `subjects` does two jobs. It filters the observations — the pinned note
    already carries the full picture once a turn, and repeating the same two
    lines on every unrelated call is how a tracker becomes wallpaper — and it
    rides along in `details` so the page can offer to show you what just moved.
    """
    details: dict[str, Any] = {"ok": True, "summary": message.split("\n")[0]}
    if subjects:
        details["subjects"] = list(dict.fromkeys(subjects))
    if not subjects:
        return ToolResult(output=message, details=details)
    notices = [
        line for line in derive.coverage(session.state)
        if any(s in line for s in subjects)
    ]
    if not notices:
        return ToolResult(output=message, details=details)
    shown = notices[:NOTICES_SHOWN]
    details["noticing"] = notices
    return ToolResult(
        output=message + "\n\nnoticing: " + " · ".join(shown),
        details=details,
    )


# ------------------------------------------------------------- the canvas


def _upsert_node(session: ArchSession, spec: dict[str, Any]) -> tuple[str, bool]:
    """Add or update one node. Returns (id, was_new). A partial spec updates
    only the fields it names — the canvas is not re-posted whole every time."""
    state = session.state
    if not isinstance(spec, dict):
        raise ToolError(f"each node must be an object, got {type(spec).__name__}.")
    label = _str(spec.get("label"))
    raw_id = _str(spec.get("id"))
    if not raw_id and not label:
        raise ToolError("a node needs a label (an id alone is not a name anyone reads).")
    nid = slug(raw_id) if raw_id else state.next_node_id(label)

    current = state.nodes.get(nid)
    candidate = replace(current) if current is not None else Node(id=nid, label=label or nid)
    if label:
        candidate.label = label
    for name in _NODE_FIELDS:
        if spec.get(name) is not None:
            setattr(candidate, name, _str(spec[name]))
    if spec.get("approaches") is not None:
        candidate.approaches = [slug(a) for a in _strlist(spec["approaches"])]
    _check(state.validate_node, candidate)
    state.nodes[nid] = candidate
    return nid, current is None


def _upsert_edge(
    session: ArchSession, spec: dict[str, Any], auto: list[str]
) -> tuple[str, bool]:
    """Add or update one edge, auto-creating either endpoint if it is missing.
    An edge is identified by (src, dst): re-sending one relabels it rather than
    stacking a second arrow between the same two boxes."""
    state = session.state
    if not isinstance(spec, dict):
        raise ToolError(f"each edge must be an object, got {type(spec).__name__}.")
    src, dst = slug(_str(spec.get("src"))), slug(_str(spec.get("dst")))
    if not _str(spec.get("src")) or not _str(spec.get("dst")):
        raise ToolError("an edge needs src and dst.")
    for ref in (src, dst):
        if ref not in state.nodes:
            stub = Node(id=ref, label=ref.replace("-", " "))
            _check(state.validate_node, stub)
            state.nodes[ref] = stub
            auto.append(ref)

    idx = state.edge_index(src, dst)
    current = state.edges[idx] if idx >= 0 else None
    candidate = replace(current) if current is not None else Edge(src=src, dst=dst)
    for name in ("label", "kind", "notes"):
        if spec.get(name) is not None:
            setattr(candidate, name, _str(spec[name]))
    _check(state.validate_edge, candidate)
    if idx >= 0:
        state.edges[idx] = candidate
    else:
        state.edges.append(candidate)
    return f"{src}->{dst}", idx < 0


def _remove(session: ArchSession, ref: str) -> str:
    """Remove a node (and everything hanging off it) or a single edge.

    Deleting is a real design move — reach for it before adding — so it is
    plain rather than ceremonial, and it says what went with it.
    """
    state = session.state
    normalized = ref.replace("->", ">")
    if ">" in normalized:
        src, _, dst = normalized.partition(">")
        idx = state.edge_index(slug(src.strip()), slug(dst.strip()))
        if idx < 0:
            raise ToolError(f"no edge {ref!r} to remove.")
        edge = state.edges.pop(idx)
        return f"edge {edge.src}->{edge.dst}"
    nid = slug(ref)
    if nid not in state.nodes:
        raise ToolError(f"no node {nid!r} to remove.")
    dropped = state.references_to(nid)
    state.edges = [e for e in state.edges if nid not in (e.src, e.dst)]
    del state.nodes[nid]
    tail = f" (and {len(dropped)} edge(s))" if dropped else ""
    return f"node {nid}{tail}"


_NODE_ITEM = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "description": "kebab-case; omit to derive one from the label"},
        "label": {"type": "string", "description": "what the box says"},
        "kind": {"type": "string", "enum": list(KINDS)},
        "responsibility": {"type": "string", "description": "one sentence on what it does"},
        "tech": {"type": "string", "description": "the concrete choice, once there is one"},
        "depth": {
            "type": "string", "enum": list(DEPTHS),
            "description": "stub = a name to react to · sketch = it has a job · "
                           "detailed = you have said what is inside. Lowering it is allowed.",
        },
        "detail": {"type": "string", "description": "prose: what's inside, in whatever "
                                                    "shape fits — schema sketch, endpoints, "
                                                    "retention, failure behaviour"},
        "approaches": {
            "type": "array", "items": {"type": "string"},
            "description": "approach ids this box belongs to. OMIT for a box every "
                           "approach shares — that is what gets drawn once.",
        },
        "notes": {"type": "string"},
        "status": {"type": "string", "enum": list(STATUSES)},
    },
    "additionalProperties": False,
}

_EDGE_ITEM = {
    "type": "object",
    "properties": {
        "src": {"type": "string"},
        "dst": {"type": "string"},
        "label": {"type": "string", "description": "what crosses it"},
        "kind": {"type": "string", "enum": list(EDGE_KINDS)},
        "notes": {"type": "string", "description": "what the caller does when the far "
                                                   "end is down, or what carries it"},
    },
    "required": ["src", "dst"],
    "additionalProperties": False,
}


class CanvasTool(Tool):
    name = "canvas"
    description = (
        "Put boxes and arrows on the board, or change ones that are there. Batch it: "
        "one call with the whole shape beats six calls building it up.\n"
        "A node's `depth` is a slider you move both ways — deepen a box when the "
        "conversation reaches its branch, collapse it back when the detail stopped "
        "earning its place. A partial spec updates only the fields it names.\n"
        "Missing edge endpoints are created as stubs, so you can draw the flow first "
        "and name what is in the boxes after.\n"
        "`approaches` is what makes rival takes coexist: label the boxes that differ, "
        "and OMIT the label on the ones both takes share so they are drawn once."
    )
    parameters = {
        "type": "object",
        "properties": {
            "nodes": {"type": "array", "items": _NODE_ITEM},
            "edges": {"type": "array", "items": _EDGE_ITEM},
            "remove": {
                "type": "array", "items": {"type": "string"},
                "description": "node ids, or 'src>dst' for a single edge",
            },
        },
        "additionalProperties": False,
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        session = _session(ctx)
        _guard_open(session)
        nodes = args.get("nodes") or []
        edges = args.get("edges") or []
        removals = _strlist(args.get("remove"))
        if not nodes and not edges and not removals:
            raise ToolError("nothing to do — pass nodes, edges, or remove.")

        auto: list[str] = []
        added = updated = 0
        subjects: list[str] = []
        for spec in nodes:
            nid, was_new = _upsert_node(session, spec)
            subjects.append(nid)
            added, updated = (added + 1, updated) if was_new else (added, updated + 1)
        new_edges = same_edges = 0
        for spec in edges:
            ref, was_new = _upsert_edge(session, spec, auto)
            new_edges, same_edges = (
                (new_edges + 1, same_edges) if was_new else (new_edges, same_edges + 1)
            )
        gone = [_remove(session, ref) for ref in removals]

        parts = []
        if added:
            parts.append(f"{added} node(s) added")
        if updated:
            parts.append(f"{updated} updated")
        if new_edges:
            parts.append(f"{new_edges} edge(s) drawn")
        if same_edges:
            parts.append(f"{same_edges} edge(s) changed")
        if auto:
            parts.append(f"auto-created stubs: {', '.join(auto)}")
        if gone:
            parts.append("removed " + "; ".join(gone))
        session.touched("canvas", subjects[0] if subjects else "board")
        return _confirm("Board: " + ", ".join(parts) + ".", session, tuple(subjects + auto))


# ------------------------------------------------------------ approaches


class ApproachTool(Tool):
    name = "approach"
    description = (
        "Name a take on the board, or grey one out when it loses.\n"
        "An approach is a label, not a separate design: boxes carry it via "
        "`canvas`, and boxes with no label are shared by every take. Two takes "
        "side by side with a real tradeoff between them is worth more than one "
        "the user will rubber-stamp.\n"
        "Greying keeps it visible in grey with the reason it lost — that reason is "
        "the single most useful thing this session leaves behind, and it is required. "
        "Nothing is ever deleted for losing."
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "e.g. 'queue-first'"},
            "id": {"type": "string", "description": "kebab-case; derived from the name if omitted"},
            "summary": {"type": "string", "description": "the tradeoff this take explores, one line"},
            "status": {"type": "string", "enum": list(STATUSES)},
            "rejected_reason": {
                "type": "string",
                "description": "why it lost. Required to grey one out.",
            },
        },
        "additionalProperties": False,
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        session = _session(ctx)
        _guard_open(session)
        state = session.state
        name = _str(args.get("name"))
        raw_id = _str(args.get("id"))
        if not raw_id and not name:
            raise ToolError("an approach needs a name (or an id).")
        aid = slug(raw_id or name)

        current = state.approaches.get(aid)
        candidate = replace(current) if current is not None else Approach(id=aid, name=name or aid)
        if name:
            candidate.name = name
        for field_name in ("summary", "status", "rejected_reason"):
            if args.get(field_name) is not None:
                setattr(candidate, field_name, _str(args[field_name]))
        _check(state.validate_approach, candidate)
        state.approaches[aid] = candidate
        session.touched("approach", aid)

        if candidate.status == "greyed":
            live = [a.name for a in state.live_approaches()]
            tail = f" Still live: {', '.join(live)}." if live else ""
            return _confirm(
                f"'{candidate.name}' greyed out, and the board keeps why: "
                f"{candidate.rejected_reason}.{tail}",
                session,
                tuple(n.id for n in state.nodes_in(aid)) or (aid,),
            )
        verb = "Named" if current is None else "Updated"
        return _confirm(
            f"{verb} approach '{candidate.name}' ({aid}). Label its boxes with "
            f"approaches=['{aid}'] and leave the shared ones unlabelled.",
            session, (aid,),
        )


# ------------------------------------------------------------- decisions


def _options(choice: str, against: Any) -> list[Option]:
    """The chosen option first, then what it beat. `against` takes bare strings
    for a quick note, or objects when the pros and cons are worth keeping."""
    opts = [Option(name=choice)] if choice else []
    for item in against or []:
        if isinstance(item, str):
            if item.strip():
                opts.append(Option(name=item.strip()))
            continue
        if not isinstance(item, dict):
            raise ToolError("each `against` entry must be a string or an object with a name.")
        name = _str(item.get("name"))
        if not name:
            raise ToolError("an `against` object needs a name.")
        opts.append(Option(name=name, pros=_strlist(item.get("pros")), cons=_strlist(item.get("cons"))))
    return opts


class DecideTool(Tool):
    name = "decide"
    description = (
        "Write down a call that was made, and what it was made against. Two lines "
        "of note, not a form.\n"
        "`why` is the part that survives: name the tradeoff in the user's terms, not "
        "the textbook's.\n"
        "`pragmatic` is first-class and not a confession. 'Less robust, and right, "
        "because it ships in a week and the rewrite is cheap' is a complete verdict — "
        "put that there and it is recorded as the reason, not as a compromise.\n"
        "When the USER named the technology, set source='user' and still put a real "
        "alternative in `against`. Absorbing their choice without weighing anything is "
        "the one move to avoid — not because they are wrong, but because they asked "
        "you to think."
    )
    parameters = {
        "type": "object",
        "properties": {
            "topic": {"type": "string", "description": "what was being decided, e.g. 'compute'"},
            "choice": {"type": "string", "description": "what won"},
            "against": {
                "type": "array",
                "items": {
                    "anyOf": [
                        {"type": "string"},
                        {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "pros": {"type": "array", "items": {"type": "string"}},
                                "cons": {"type": "array", "items": {"type": "string"}},
                            },
                            "required": ["name"],
                            "additionalProperties": False,
                        },
                    ]
                },
                "description": "what it beat — bare names, or objects when the "
                               "pros/cons are worth keeping",
            },
            "why": {"type": "string", "description": "the reason, in their terms"},
            "source": {"type": "string", "enum": ["model", "user"]},
            "pragmatic": {
                "type": "string",
                "description": "set when the choice is convenient rather than optimal, "
                               "and that is the right call — say why it is right",
            },
            "id": {"type": "string", "description": "to amend an existing decision"},
        },
        "required": ["topic", "choice"],
        "additionalProperties": False,
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        session = _session(ctx)
        _guard_open(session)
        state = session.state
        did = _str(args.get("id"))
        choice = _str(args["choice"])
        current = state.decision_by_id(did) if did else None
        if did and current is None:
            known = ", ".join(d.id for d in state.decisions) or "none"
            raise ToolError(f"no decision {did!r} to amend (known: {known}).")

        dec = Decision(
            id=current.id if current else session.next_decision_id(),
            topic=_str(args["topic"]),
            options=_options(choice, args.get("against")),
            choice=choice,
            rationale=_str(args.get("why")),
            source=_str(args.get("source")) or "model",
            pragmatism_note=_str(args.get("pragmatic")),
        )
        _check(state.validate_decision, dec)
        if current is not None:
            state.decisions[state.decisions.index(current)] = dec
        else:
            state.decisions.append(dec)
        session.touched("decision", dec.id)

        msg = f"{dec.id}: {dec.topic} -> {dec.choice}."
        if len(dec.options) < 2:
            msg += (
                " Nothing is recorded beside it, so nothing was weighed — add what you"
                " would have picked instead, even if only to say why it loses."
                if dec.source == "user"
                else " No alternative recorded — a choice with no rival is a preference."
            )
        return _confirm(msg, session)


# ------------------------------------------------------------- questions


class QuestionTool(Tool):
    name = "question"
    description = (
        "Park something only the user can settle — a cost tradeoff, a scale "
        "expectation, a business constraint — so it is not lost while you carry on "
        "elsewhere. Then actually ask it in your reply; this only records it.\n"
        "`recommendation` is required in spirit: every question goes to the user with "
        "the answer you would give, so they are reacting to a proposal instead of "
        "filling in a blank.\n"
        "NEVER park anything you could find out yourself. If the repo, the knowledge "
        "graph or a search would answer it, that is your job, not theirs.\n"
        "Pass `id` with an `answer` once they say."
    )
    parameters = {
        "type": "object",
        "properties": {
            "question": {"type": "string"},
            "recommendation": {"type": "string", "description": "what you'd do, and why"},
            "id": {"type": "string", "description": "to answer or defer one already parked"},
            "answer": {"type": "string"},
            "status": {"type": "string", "enum": ["open", "answered", "deferred"]},
        },
        "additionalProperties": False,
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        session = _session(ctx)
        _guard_open(session)
        state = session.state
        qid = _str(args.get("id"))
        if qid:
            q = state.question_by_id(qid)
            if q is None:
                known = ", ".join(x.id for x in state.questions) or "none"
                raise ToolError(f"no question {qid!r} (known: {known}).")
            if args.get("question") is not None:
                q.question = _str(args["question"])
            if args.get("recommendation") is not None:
                q.recommendation = _str(args["recommendation"])
            if args.get("answer") is not None:
                q.answer = _str(args["answer"])
                q.status = "answered"
            if args.get("status") is not None:
                q.status = _str(args["status"])
            session.touched("question", q.id)
            return _confirm(f"{q.id} {q.status}.", session)

        text = _str(args.get("question"))
        if not text:
            raise ToolError("a question needs text (or an id, to answer one already parked).")
        q = Question(
            id=session.next_question_id(),
            question=text,
            recommendation=_str(args.get("recommendation")),
        )
        state.questions.append(q)
        session.touched("question", q.id)
        if not q.recommendation:
            return _confirm(
                f"Parked {q.id}. It has no recommendation — ask it in your reply WITH the "
                "answer you'd give, or they are starting from a blank page.",
                session,
            )
        return _confirm(f"Parked {q.id}. Ask it in your reply, with your recommendation.", session)


# ----------------------------------------------------------------- brief


class BriefTool(Tool):
    name = "brief"
    description = (
        "Record a load-bearing fact about what is being built, as it surfaces. Not a "
        "form and not a prerequisite — call it when you learn something, with only the "
        "field you learned.\n"
        "`scale` is prose: 'a few hundred users, spiky at month end' is worth more than "
        "five empty numbers. Absent a stated scale, design for the smallest thing that "
        "could work and say out loud that that is what you are doing."
    )
    parameters = {
        "type": "object",
        "properties": {
            "goal": {
                "type": "string",
                "description": (
                    "what is being built, as a title — one short line, not a summary. "
                    "It is the page heading the user reads above the board, so the "
                    "comparisons, the mechanism and the later phases belong on the "
                    "board or in the chat, not in here."
                ),
            },
            "actors": {"type": "array", "items": {"type": "string"}},
            "scale": {"type": "string"},
            "constraints": {"type": "array", "items": {"type": "string"}},
            "non_goals": {"type": "array", "items": {"type": "string"}},
        },
        "additionalProperties": False,
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        session = _session(ctx)
        _guard_open(session)
        brief = session.state.brief
        written: list[str] = []
        for name in ("goal", "scale"):
            if args.get(name) is not None:
                setattr(brief, name, _str(args[name]))
                written.append(name)
        for name in ("actors", "constraints", "non_goals"):
            if args.get(name) is not None:
                setattr(brief, name, _strlist(args[name]))
                written.append(name)
        if not written:
            raise ToolError("nothing to record — pass at least one field.")
        session.touched("brief", written[0])
        return _confirm("Brief: " + ", ".join(written) + " recorded.", session)


# --------------------------------------------------------------- handoff


class HandoffTool(Tool):
    name = "handoff"
    description = (
        "Close the session and write the handoff — the board, the decisions and why "
        "they went that way, the approaches that lost and why, and everything still "
        "open — for whoever builds it.\n"
        "Call this when the USER says they are done. It is not how you end a turn (a "
        "plain reply does that) and it is not a gate you ask them to pass: nothing here "
        "checks whether the design is finished, because that judgement is theirs."
    )
    parameters = {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "where the design landed, in a line or two"},
        },
        "required": ["summary"],
        "additionalProperties": False,
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        from .bundle import write_bundle
        from .kg_seed import seed_kg

        session = _session(ctx)
        _guard_open(session)
        state = session.state
        if not state.nodes:
            raise ToolError(
                "there is nothing on the board to hand off. Draw the shape you have "
                "been talking about first."
            )
        state.handed_off = True
        written = write_bundle(state, session.run_dir) if session.run_dir else []
        seeded = seed_kg(ctx.kg, state, str(written[1]) if len(written) > 1 else "architecture.md")
        session.touched("handoff", "session")

        still_open = state.open_questions()
        paths = ", ".join(str(p) for p in written) or "(no run dir — nothing written)"
        tail = f" {len(still_open)} question(s) travel with it, unanswered." if still_open else ""
        if seeded:
            tail += f" {seeded}."
        return ToolResult(
            output=f"Handed off: {paths}.{tail} Next step: bird code.",
            details={"done": True, "artifacts": [str(p) for p in written],
                     "summary": _str(args["summary"])},
        )


# --------------------------------------------------------- reading the repo


class ImportRepoTool(Tool):
    name = "import_repo"
    description = (
        "Load the as-is architecture of an existing feature or subsystem onto the "
        "board from the code knowledge graph, so you are designing against what is "
        "actually there instead of against a guess.\n"
        "Imported boxes are marked as background: they are what exists, not what you "
        "are proposing. One-shot at the start of a session — it refuses once the board "
        "has something on it. Review what it inferred and correct it with `canvas`."
    )
    parameters = {
        "type": "object",
        "properties": {
            "scope": {
                "type": "string",
                "description": "a feature name, file path, or symbol to scope the import to",
            },
            "max_nodes": {"type": "integer"},
            "max_depth": {"type": "integer"},
        },
        "required": ["scope"],
        "additionalProperties": False,
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        from .reverse_seed import (
            DEFAULT_MAX_DEPTH,
            DEFAULT_MAX_NODES,
            reverse_seed,
            scope_subgraph,
        )

        session = _session(ctx)
        _guard_open(session)
        state = session.state
        scope = _str(args.get("scope"))
        if not scope:
            raise ToolError("scope is required — a feature name, file path, or symbol.")
        if state.nodes:
            raise ToolError(
                f"the board already has {len(state.nodes)} box(es); import_repo is a "
                "one-shot at session start. Start a new session to re-scope."
            )
        kg = ctx.kg
        if kg is None or not kg.is_ready():
            raise ToolError(
                "the knowledge graph is not ready; run `bird kg build` first, or "
                "design from what the user tells you."
            )
        subgraph = scope_subgraph(
            kg, scope,
            max_nodes=int(args.get("max_nodes", DEFAULT_MAX_NODES)),
            max_depth=int(args.get("max_depth", DEFAULT_MAX_DEPTH)),
        )
        if not subgraph.nodes:
            raise ToolError(
                "that scope matched no nodes in the graph; try a file path, a symbol "
                "name, or a broader term."
            )
        result = reverse_seed(subgraph, scope)
        if not result.nodes:
            raise ToolError(
                "that scope matched no nodes in the graph; try a file path, a symbol "
                "name, or a broader term."
            )
        for node in result.nodes:
            _check(state.validate_node, node)
            state.nodes[node.id] = node
        for edge in result.edges:
            _check(state.validate_edge, edge)
            state.edges.append(edge)
        session.touched("import_repo", scope)

        low = [i for i in result.inference_log if i.confidence == "low"]
        parts = [
            f"Loaded {len(result.nodes)} box(es) and {len(result.edges)} edge(s) from the "
            f"graph (scope: {scope!r}), marked as existing background.",
            "boxes: " + ", ".join(n.id for n in result.nodes),
        ]
        if low:
            parts.append(f"{len(low)} low-confidence guess(es) — correct them with `canvas`:")
            parts += [f"  - {i.node_id}.{i.field} = {i.value} ({i.evidence})" for i in low[:12]]
            if len(low) > 12:
                parts.append(f"  ... and {len(low) - 12} more (see details).")
        return ToolResult(
            output="\n".join(parts),
            details={
                "loaded": len(result.nodes),
                "nodes": [n.id for n in result.nodes],
                "edges": len(result.edges),
                "inferences": [
                    {"node_id": i.node_id, "field": i.field, "value": i.value,
                     "confidence": i.confidence, "evidence": i.evidence}
                    for i in result.inference_log
                ],
            },
        )


# ------------------------------------------------------------- the toolset

# what counts as progress for the engine's explore nudge
MUTATING_TOOLS = frozenset({"canvas", "approach", "decide", "question", "brief"})


def arch_harness_tools(with_kg: bool = True, with_web: bool = True) -> list[Tool]:
    """The arch toolset: six ways to record, plus the fact-finding. No
    edit/write/bash — the architect designs and argues; it does not touch the
    repo."""
    tools: list[Tool] = [ReadTool(), ReadImageTool(), LsTool()]
    if with_kg:
        tools.append(KgQueryTool())
    if with_web:
        tools.extend([WebSearchTool(), WebFetchTool()])
    tools.extend([
        ImportRepoTool(),
        CanvasTool(), ApproachTool(), DecideTool(), QuestionTool(), BriefTool(),
        SkillTool(),
        HandoffTool(),
    ])
    return tools
