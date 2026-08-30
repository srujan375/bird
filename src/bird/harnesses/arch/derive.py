"""What the harness works out for itself, from the graph alone.

Three pure functions over ArchState. No model, no I/O, nothing that can be
slow, fail silently, or become a third voice in a two-person conversation.

`frontier` is the design-tree walk: the branches whose prerequisites are
settled, so they can be asked about *now* without guessing at an answer nobody
has given. The rule that makes it a tree rather than a checklist is one line —
while more than one approach is live, only the shared nodes are askable,
because the schema of a database that exists in approach B is not a question
until B has won.

`coverage` is the deterministic thinness pass that used to be a background
critic filing "concerns". It is the same set of checks; what changed is where
they land. They are ingredients for the architect's next recommendation ("one
thing I'm noticing — this store has no retention policy, that'll bite you at
scale; set one now or defer it?"), not a list some other model maintains.

`note` is the internal state summary the engine pins each turn. It is short on
purpose and the user never sees it: the detail belongs on the canvas, and a
wall of re-rendered text in the conversation is the thing this replaced.
"""

from __future__ import annotations

from .state import COST_ORDER, ArchState, Node

FRONTIER_SHOWN = 4
COVERAGE_SHOWN = 4
DECISIONS_SHOWN = 5
QUESTIONS_SHOWN = 3
NOTES_SHOWN = 4
EDITS_SHOWN = 5

# Words that count as having said how long data lives. Deliberately generous:
# the check exists to catch a store nobody thought about, not to police wording.
_RETENTION_WORDS = (
    "retention", "retain", "ttl", "expire", "expiry", "evict", "purge",
    "archive", "forever", "indefinit", "prune", "vacuum", "rotate", "delete after",
)
# Kinds whose failure is worth a sentence somewhere: the things other nodes
# depend on and cannot simply retry away.
_DEPENDED_ON = ("store", "queue", "external", "api", "llm")
_DURABLE = ("store", "queue")


def _text(node: Node) -> str:
    facts = " ".join(f"{k} {v}" for k, v in node.facts.items())
    items = " ".join(f"{i.k} {i.v} {i.d}" for i in node.items)
    return " ".join((node.detail, node.notes, node.responsibility, facts, items)).lower()


# ------------------------------------------------------------- the frontier


def askable(state: ArchState) -> list[Node]:
    """Nodes whose branch can be walked right now.

    While the board carries more than one live approach, the fork itself is the
    open question and only the shared nodes are settled enough to deepen. Once
    one approach is left standing (or there were never any), everything active
    is fair game.
    """
    live = state.live_approaches()
    nodes = [n for n in state.active_nodes() if not n.existing]
    if len(live) > 1:
        return [n for n in nodes if n.shared()]
    return nodes


def _load_bearing(state: ArchState, node: Node) -> int:
    """How much of the design hangs off this node. Ties in cost order break
    toward the box more things touch."""
    return len(state.edges_touching(node.id))


def frontier(state: ArchState) -> list[str]:
    """The branches worth walking next, most expensive-to-get-wrong first."""
    live = state.live_approaches()
    out: list[str] = []
    if len(live) > 1:
        names = " vs ".join(a.name for a in live)
        out.append(
            f"the fork is still open: {names}. Everything downstream of it waits on "
            "which one wins — settle it before deepening anything that isn't shared."
        )
    candidates = [n for n in askable(state) if n.depth != "detailed"]
    candidates.sort(key=lambda n: (COST_ORDER.get(n.kind, 9), -_load_bearing(state, n), n.id))
    for node in candidates[:FRONTIER_SHOWN]:
        why = "costliest to change later" if COST_ORDER.get(node.kind, 9) <= 2 else "unelaborated"
        out.append(f"{node.id} ({node.kind}, {node.depth}) — {why}")
    return out


# -------------------------------------------------------------- coverage


def coverage(state: ArchState) -> list[str]:
    """Deterministic thinness. Advice for the architect's next turn; never a
    gate, never a separate voice."""
    out: list[str] = []
    ours = [n for n in state.active_nodes() if not n.existing]

    # a box nothing in the design uses
    if len(state.nodes) > 1:
        for node in ours:
            # a container is used through its members; it is not itself wired
            if node.kind == "group" or state.children_of(node.id):
                continue
            if not state.edges_touching(node.id):
                out.append(
                    f"{node.id} is on no edge — nothing in the design uses it. Merge it "
                    "into a neighbour or drop it."
                )

    # data that grows without bound
    for node in ours:
        if node.kind in _DURABLE and node.depth != "stub":
            if not any(w in _text(node) for w in _RETENTION_WORDS):
                out.append(
                    f"{node.id} ({node.kind}) never says how long its data lives — that is "
                    "the bill nobody budgets for."
                )

    # the graph-native replacement for "a happy flow with no failure twin":
    # something is depended on and nothing says what the caller does when it is down
    for node in ours:
        if node.kind not in _DEPENDED_ON:
            continue
        incoming = [e for e in state.edges if e.dst == node.id]
        if incoming and not any(e.notes.strip() for e in incoming):
            callers = ", ".join(sorted({e.src for e in incoming}))
            out.append(
                f"nothing says what {callers} does when {node.id} is down — the failure "
                "path is undesigned."
            )

    # a choice the user handed over that nothing was weighed against
    for dec in state.decisions:
        if dec.source == "user" and len(dec.options) < 2:
            out.append(
                f"{dec.id} ({dec.topic} -> {dec.choice}) came from the user with no "
                "alternative beside it — say what you'd have picked instead and why, "
                "then endorse it or push back."
            )
    return out


# ------------------------------------------------------ the internal note


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def note(state: ArchState, user_edits: list[str] | None = None) -> str:
    """The architect's own working memory, pinned each turn. Never rendered to
    the user — the canvas carries the detail and the reply carries the point.

    `user_edits` is what the person changed on the board since the architect
    last looked. The design already reflects it, but a snapshot cannot say that
    a label is new or that somebody pinned a note to a box, and drawing on the
    board is the user talking in the other medium. It arrives here so a turn
    can answer it.
    """
    if state.handed_off:
        return "[arch] handed off — the session is closed."

    live = state.live_approaches()
    counts = [_plural(len(state.nodes), "node"), _plural(len(state.edges), "edge")]
    if state.approaches:
        counts.append(f"{len(live)}/{len(state.approaches)} approaches live")
    if state.decisions:
        counts.append(f"{len(state.decisions)} decided")
    lines = ["[arch] " + " · ".join(counts)]

    stated = state.brief.stated()
    lines.append("brief: " + (" · ".join(stated) if stated else "nothing stated yet"))

    if state.decisions:
        recent = state.decisions[-DECISIONS_SHOWN:]
        lines.append(
            "settled: " + " · ".join(f"{d.topic}->{d.choice}" for d in recent)
            + " (don't re-open these without a reason)"
        )

    # what the person put on the board, in their words
    if state.annotations:
        shown = state.annotations[-NOTES_SHOWN:]
        lines.append("notes on the board: " + " · ".join(
            (f'on {a.anchor}: "{a.text}"' if a.anchor else f'"{a.text}"') for a in shown
        ))

    if user_edits:
        lines.append("the user just: " + " · ".join(user_edits[:EDITS_SHOWN]))
        lines.append("  (they are drawing, not just watching — answer it)")

    waiting = state.open_questions()
    if waiting:
        lines.append(
            "waiting on the user: "
            + " · ".join(f"{q.id} {q.question[:60]}" for q in waiting[:QUESTIONS_SHOWN])
        )

    front = frontier(state)
    if front:
        lines.append("frontier: " + " · ".join(front[:FRONTIER_SHOWN]))
    elif state.nodes:
        lines.append(
            "frontier: nothing left unelaborated — ask the user whether they're done, "
            "or find the thing you both skipped."
        )
    else:
        lines.append(
            "frontier: empty board. Put a shape up from what they asked for and open "
            "with your first real question."
        )

    thin = coverage(state)
    if thin:
        shown = thin[:COVERAGE_SHOWN]
        lines.append("noticing: " + " · ".join(shown))
        if len(thin) > len(shown):
            lines.append(f"  (+{len(thin) - len(shown)} more)")
    return "\n".join(lines)
