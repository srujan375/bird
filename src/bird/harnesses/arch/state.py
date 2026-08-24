"""ArchState — one graph, and the record of what was decided about it.

There is exactly one representation of the design. A node is loose or
committed depending on its `depth`, not on which layer it lives in; there is
no sketchbook, no promote step, no parallel world to keep in sync. "Reduce the
depth" and "flesh this out" are the same move in opposite directions on the
same object.

Approaches are *labels on nodes*, not separate designs. A node with an empty
`approaches` list is shared by all of them — the database both takes use once,
drawn once. A rejected approach is greyed, not deleted, and it keeps the reason
it lost: that reason is the most valuable thing the session produces and the
thing an archived object nobody opens reliably loses.

Two kinds of check live here, and the split is the posture of the harness:

- `validate_*` raises ValueError for what is *broken* — a malformed id, an
  unknown kind. The tool layer turns those into ToolErrors verbatim, because
  accepting them would corrupt the graph.
- `derive.py` reports what is *thin*. Nothing there ever refuses a call; it
  feeds the architect's next recommendation, so the design gets argued about
  instead of form-filled.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

KINDS: tuple[str, ...] = (
    "service", "store", "queue", "api", "ui", "llm", "external", "infra",
)
EDGE_KINDS: tuple[str, ...] = ("sync", "async", "batch")
# a two-way slider on one object, not a one-way expand
DEPTHS: tuple[str, ...] = ("stub", "sketch", "detailed")
STATUSES: tuple[str, ...] = ("active", "greyed")
DECISION_SOURCES: tuple[str, ...] = ("model", "user")
QUESTION_STATUSES: tuple[str, ...] = ("open", "answered", "deferred")

# Kinds whose contents outlive every rewrite around them, most expensive first.
# Used to order the frontier: when several branches are equally askable, the one
# that is costliest to get wrong goes first.
COST_ORDER: dict[str, int] = {
    "store": 0, "api": 1, "queue": 2, "llm": 3, "infra": 4,
    "ui": 5, "service": 6, "external": 7,
}

_ID_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")

# Keys that only ever appeared in the pre-rebuild state file. A session written
# by the old two-layer harness cannot be read into this model without inventing
# things nobody said, so resume refuses it by name instead of silently starting
# from an empty design that looks like a lost session.
LEGACY_KEYS = ("sketchbook", "components", "obligations", "concerns")


class LegacyStateError(Exception):
    """A state file from the pre-rebuild arch harness."""


# ------------------------------------------------------------------ brief


@dataclass
class Brief:
    """What the design is for. Accreted from the conversation as facts surface,
    never a form with required fields — a blank brief is a session that has not
    got there yet, not an error state."""

    goal: str = ""
    actors: list[str] = field(default_factory=list)
    scale: str = ""  # free prose: "a few hundred users, bursty" beats five empty numbers
    constraints: list[str] = field(default_factory=list)
    non_goals: list[str] = field(default_factory=list)

    def stated(self) -> list[str]:
        """The load-bearing facts, for the architect's own state note."""
        out: list[str] = []
        if self.goal.strip():
            out.append(f"goal={self.goal.strip()}")
        if self.actors:
            out.append("actors=" + ", ".join(self.actors))
        if self.scale.strip():
            out.append(f"scale={self.scale.strip()}")
        if self.constraints:
            out.append("constraints=" + "; ".join(self.constraints))
        if self.non_goals:
            out.append("non-goals=" + "; ".join(self.non_goals))
        return out


# ------------------------------------------------------------------ graph


@dataclass
class Node:
    """A box on the board.

    `depth` is the fidelity slider. A stub is a name you can react to; a sketch
    has a responsibility and maybe a tech; a detailed node has said what is
    inside it. Nodes deepen when the conversation walks to their branch, which
    is why depth is a property of the node and not a phase of the session.

    `approaches` is the set of approach ids this node belongs to. Empty means
    shared — drawn once, used by every approach on the board.
    """

    id: str
    label: str
    kind: str = "service"
    responsibility: str = ""
    tech: str = ""
    depth: str = "stub"
    detail: str = ""  # prose, not a schema — what is inside, in the words that fit
    approaches: list[str] = field(default_factory=list)
    status: str = "active"  # greyed travels with its approach
    notes: str = ""
    existing: bool = False  # imported from the repo: background, not ours to design
    # Where the box sits, once somebody has put it somewhere. None means the
    # board has not been arranged by hand and the canvas may lay it out.
    #
    # Position is design, not decoration: which column a box sits in says which
    # approach it belongs to, and what it sits next to says what it is part of.
    # A board whose arrangement dies with the tab loses that, so the coordinates
    # live here — written by the person dragging, never by the architect, which
    # is why no tool takes them.
    x: float | None = None
    y: float | None = None

    def shared(self) -> bool:
        return not self.approaches


@dataclass
class Edge:
    src: str
    dst: str
    label: str = ""
    kind: str = "sync"
    notes: str = ""  # including what happens when dst is down

    def key(self) -> tuple[str, str, str]:
        return (self.src, self.dst, self.label)


@dataclass
class Approach:
    """One take on the board. Not a separate world — a label a subset of nodes
    carries. Greyed approaches stay visible with `rejected_reason`; that line is
    the ADR the session exists to produce."""

    id: str
    name: str
    summary: str = ""
    status: str = "active"
    rejected_reason: str = ""


@dataclass
class Annotation:
    """A note left on the board.

    The reasoning that belongs *next to* something rather than inside it — why
    this column is cheaper, what killed the third option, what to revisit and
    when. It travels into the handoff, because a note nobody can read later is
    just a decoration.

    `anchor` names the box it hangs off, or is empty for a note pinned to the
    canvas itself.
    """

    id: str
    text: str
    x: float = 0
    y: float = 0
    w: float = 190
    anchor: str = ""


# ---------------------------------------------------- decisions & questions


@dataclass
class Option:
    name: str
    pros: list[str] = field(default_factory=list)
    cons: list[str] = field(default_factory=list)


@dataclass
class Decision:
    """A settled call, with what it was weighed against.

    `pragmatism_note` is first-class, not an apology. "Less robust, and right,
    because it ships in a week and the rewrite is cheap" is a complete
    architectural verdict; recording it as a compromise against some unbuilt
    ideal misrepresents what was decided and why.
    """

    id: str
    topic: str
    options: list[Option] = field(default_factory=list)
    choice: str = ""
    rationale: str = ""
    source: str = "model"  # model | user — who put the choice on the table
    pragmatism_note: str = ""


@dataclass
class Question:
    """Something only the user can settle, parked until they do.

    Every one carries a `recommendation`: the user reacts to a concrete
    proposal rather than starting from a blank page. A question with no
    recommendation is an interview question, and this harness does not conduct
    interviews.
    """

    id: str
    question: str
    recommendation: str = ""
    answer: str = ""
    status: str = "open"

    @property
    def open(self) -> bool:
        return self.status == "open"


# ------------------------------------------------------------- the state


@dataclass
class ArchState:
    brief: Brief = field(default_factory=Brief)
    nodes: dict[str, Node] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)
    approaches: dict[str, Approach] = field(default_factory=dict)
    decisions: list[Decision] = field(default_factory=list)
    questions: list[Question] = field(default_factory=list)
    annotations: list[Annotation] = field(default_factory=list)
    # the session ends when the user says it is done; this is that, and the
    # only irreversible thing in the model
    handed_off: bool = False

    # ---- reading the board ----

    def live_approaches(self) -> list[Approach]:
        return [a for a in self.approaches.values() if a.status == "active"]

    def greyed_approaches(self) -> list[Approach]:
        return [a for a in self.approaches.values() if a.status == "greyed"]

    def nodes_in(self, approach_id: str) -> list[Node]:
        """Nodes carrying this approach's label. Shared nodes are not included —
        ask for those with `shared_nodes()`; a caller that wants the whole
        drawing of one approach wants both."""
        return [n for n in self.nodes.values() if approach_id in n.approaches]

    def shared_nodes(self) -> list[Node]:
        return [n for n in self.nodes.values() if n.shared()]

    def active_nodes(self) -> list[Node]:
        """Boxes still in play. A box is out either because it was greyed on its
        own, or because every approach it belonged to lost."""
        return [n for n in self.nodes.values() if not self.is_greyed(n)]

    def is_greyed(self, node: Node) -> bool:
        """Whether a box has fallen out of the live design.

        A box with one surviving approach stays live even when its other
        approaches lost — that is what makes "take the lambda from the left and
        the queue from the right" work: the hybrid keeps its boxes while the
        approaches around them grey out. Shared boxes (no label at all) outlive
        every approach.
        """
        if node.status == "greyed":
            return True
        if not node.approaches:
            return False
        return all(
            a in self.approaches and self.approaches[a].status == "greyed"
            for a in node.approaches
        )

    def open_questions(self) -> list[Question]:
        return [q for q in self.questions if q.open]

    def edges_touching(self, node_id: str) -> list[Edge]:
        return [e for e in self.edges if node_id in (e.src, e.dst)]

    def edge_index(self, src: str, dst: str, label: str | None = None) -> int:
        for i, e in enumerate(self.edges):
            if e.src == src and e.dst == dst and (label is None or e.label == label):
                return i
        return -1

    def references_to(self, node_id: str) -> list[str]:
        """What would dangle if the id vanished."""
        return [f"{e.src} -> {e.dst}" for e in self.edges_touching(node_id)]

    def decision_by_id(self, did: str) -> Decision | None:
        return next((d for d in self.decisions if d.id == did), None)

    def question_by_id(self, qid: str) -> Question | None:
        return next((q for q in self.questions if q.id == qid), None)

    def annotation_by_id(self, aid: str) -> Annotation | None:
        return next((a for a in self.annotations if a.id == aid), None)

    def notes_on(self, node_id: str) -> list[Annotation]:
        return [a for a in self.annotations if a.anchor == node_id]

    def next_node_id(self, label: str) -> str:
        """A free kebab-case id derived from a label."""
        base = slug(label)
        if base not in self.nodes:
            return base
        n = 2
        while f"{base}-{n}" in self.nodes:
            n += 1
        return f"{base}-{n}"

    def next_id(self, prefix: str, existing: Any) -> str:
        taken = {getattr(x, "id", x) for x in existing}
        n = len(taken) + 1
        while f"{prefix}{n}" in taken:
            n += 1
        return f"{prefix}{n}"

    # ---- validation: only what is BROKEN ----

    def validate_node(self, node: Node) -> None:
        if not _ID_RE.match(node.id):
            raise ValueError(
                f"node id {node.id!r} must be kebab-case (lowercase letters, digits, "
                "hyphens; starts with a letter), e.g. 'order-store'."
            )
        if node.kind not in KINDS:
            raise ValueError(f"unknown kind {node.kind!r}; one of: {', '.join(KINDS)}.")
        if node.depth not in DEPTHS:
            raise ValueError(f"depth must be one of {', '.join(DEPTHS)}.")
        if node.status not in STATUSES:
            raise ValueError(f"status must be one of {', '.join(STATUSES)}.")
        for aid in node.approaches:
            if aid not in self.approaches:
                raise ValueError(
                    f"node {node.id!r} is labelled with unknown approach {aid!r}; "
                    "name it with `approach` first, or leave the label off to make "
                    "the node shared."
                )

    def validate_edge(self, edge: Edge) -> None:
        for ref in (edge.src, edge.dst):
            if ref not in self.nodes:
                raise ValueError(f"edge references unknown node {ref!r}.")
        if edge.kind not in EDGE_KINDS:
            raise ValueError(f"edge kind must be one of {', '.join(EDGE_KINDS)}.")
        if edge.src == edge.dst:
            raise ValueError(f"edge from {edge.src!r} to itself.")

    @staticmethod
    def validate_decision(dec: Decision) -> None:
        if not dec.topic.strip():
            raise ValueError("a decision needs a topic.")
        names = [o.name for o in dec.options]
        if names and dec.choice and dec.choice not in names:
            raise ValueError(f"choice {dec.choice!r} must match one option name: {names}.")

    @staticmethod
    def validate_approach(app: Approach) -> None:
        if not _ID_RE.match(app.id):
            raise ValueError(f"approach id {app.id!r} must be kebab-case, e.g. 'queue-first'.")
        if app.status not in STATUSES:
            raise ValueError(f"approach status must be one of {', '.join(STATUSES)}.")
        if app.status == "greyed" and not app.rejected_reason.strip():
            raise ValueError(
                "greying an approach needs the reason it lost — that reason is the "
                "whole point of leaving it on the board."
            )

    # ---- serialization ----

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ArchState":
        present = [k for k in LEGACY_KEYS if k in d]
        if present:
            raise LegacyStateError(
                "this session was recorded by the previous arch harness "
                f"(found: {', '.join(present)}). Its two-layer design cannot be "
                "read into the current one-graph model — start a new session."
            )
        state = cls(handed_off=bool(d.get("handed_off", False)))
        b = d.get("brief") or {}
        state.brief = Brief(
            goal=b.get("goal", ""),
            actors=list(b.get("actors", [])),
            scale=b.get("scale", ""),
            constraints=list(b.get("constraints", [])),
            non_goals=list(b.get("non_goals", [])),
        )
        for aid, a in (d.get("approaches") or {}).items():
            state.approaches[aid] = Approach(
                id=a.get("id", aid),
                name=a.get("name", aid),
                summary=a.get("summary", ""),
                status=a.get("status", "active"),
                rejected_reason=a.get("rejected_reason", ""),
            )
        for nid, n in (d.get("nodes") or {}).items():
            state.nodes[nid] = Node(
                id=n.get("id", nid),
                label=n.get("label", nid),
                kind=n.get("kind", "service"),
                responsibility=n.get("responsibility", ""),
                tech=n.get("tech", ""),
                depth=n.get("depth", "stub"),
                detail=n.get("detail", ""),
                approaches=list(n.get("approaches", [])),
                status=n.get("status", "active"),
                notes=n.get("notes", ""),
                existing=bool(n.get("existing", False)),
                x=n.get("x"),
                y=n.get("y"),
            )
        state.edges = [
            Edge(
                src=e["src"], dst=e["dst"], label=e.get("label", ""),
                kind=e.get("kind", "sync"), notes=e.get("notes", ""),
            )
            for e in d.get("edges", [])
        ]
        state.decisions = [
            Decision(
                id=x["id"], topic=x.get("topic", ""),
                options=[Option(**o) for o in x.get("options", [])],
                choice=x.get("choice", ""), rationale=x.get("rationale", ""),
                source=x.get("source", "model"),
                pragmatism_note=x.get("pragmatism_note", ""),
            )
            for x in d.get("decisions", [])
        ]
        state.questions = [
            Question(
                id=q["id"], question=q.get("question", ""),
                recommendation=q.get("recommendation", ""),
                answer=q.get("answer", ""), status=q.get("status", "open"),
            )
            for q in d.get("questions", [])
        ]
        state.annotations = [
            Annotation(
                id=a["id"], text=a.get("text", ""),
                x=a.get("x", 0), y=a.get("y", 0), w=a.get("w", 190),
                anchor=a.get("anchor", ""),
            )
            for a in d.get("annotations", [])
        ]
        return state


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(text).strip().lower()).strip("-")
    return s or "node"
