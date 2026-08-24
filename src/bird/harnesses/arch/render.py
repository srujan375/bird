"""The board, as a diagram. Pure functions ArchState -> mermaid.

One drawing, not a set of views to switch between. Shared nodes sit at the top
level and are drawn once; a node that belongs to exactly one approach is drawn
inside that approach's box, so the tradeoff is visible spatially — approach A
here, approach B there, the database they both use in the middle. A greyed
approach stays on the board in grey, carrying the reason it lost.

(A node labelled with two approaches but not all of them is drawn at the top
level: mermaid can only place a node in one subgraph, and duplicating it would
draw two boxes where the design has one.)
"""

from __future__ import annotations

import re
from typing import Any

from .state import ArchState, Node

_NODE_SHAPES = {
    "store": ('[("', '")]'),
    "queue": ('[["', '"]]'),
    "external": ('(("', '"))'),
}
_EDGE_ARROWS = {"sync": "-->", "async": "-.->", "batch": "==>"}

GREY_CLASS = "classDef greyed fill:#f5f5f5,stroke:#b0b0b0,color:#9a9a9a"
EXISTING_CLASS = "classDef existing fill:#f7f7f7,stroke:#949494,color:#949494"


def _label(text: str) -> str:
    # render must never hard-crash on a stray non-string that slipped past the
    # tool layer (which is the real gate)
    return str(text).replace('"', "'")


def _ident(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", str(text)).strip("_") or "X"


def _node_line(node: Node, indent: str = "  ") -> str:
    open_b, close_b = _NODE_SHAPES.get(node.kind, ('["', '"]'))
    caption = _label(node.label or node.id)
    tail = f"<br/><i>{_label(node.tech)}</i>" if node.tech else f"<br/><i>{node.kind}</i>"
    return f"{indent}{_ident(node.id)}{open_b}{caption}{tail}{close_b}"


def board_mermaid(state: ArchState) -> str:
    """The whole board: shared structure, every approach beside it, greyed ones
    included."""
    lines = ["flowchart LR"]
    placed: set[str] = set()

    # each approach gets a box; only its exclusive nodes go inside it
    for app in state.approaches.values():
        exclusive = [
            n for n in state.nodes.values()
            if n.approaches == [app.id]
        ]
        if not exclusive:
            continue
        caption = _label(app.name)
        if app.status == "greyed" and app.rejected_reason:
            caption += f"<br/><i>not taken: {_label(app.rejected_reason)}</i>"
        lines.append(f'  subgraph {_ident(app.id)}["{caption}"]')
        for node in exclusive:
            lines.append(_node_line(node, "    "))
            placed.add(node.id)
        lines.append("  end")

    for node in state.nodes.values():
        if node.id not in placed:
            lines.append(_node_line(node))

    for edge in state.edges:
        arrow = _EDGE_ARROWS.get(edge.kind, "-->")
        label = f'|"{_label(edge.label)}"|' if edge.label else ""
        lines.append(f"  {_ident(edge.src)} {arrow}{label} {_ident(edge.dst)}")

    greyed = [_ident(n.id) for n in state.nodes.values() if state.is_greyed(n)]
    if greyed:
        lines.append(f"  {GREY_CLASS}")
        lines.append(f"  class {','.join(sorted(set(greyed)))} greyed")
    existing = [_ident(n.id) for n in state.nodes.values() if n.existing]
    if existing:
        lines.append(f"  {EXISTING_CLASS}")
        lines.append(f"  class {','.join(sorted(set(existing)))} existing")
    return "\n".join(lines)


def render_all(state: ArchState) -> dict[str, Any]:
    """The `renders` block of the arch_state event. One board — the page draws
    its own canvas from the structured state, and this is what the bundle and
    the render tests pin."""
    return {"board": board_mermaid(state)}
