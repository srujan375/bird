"""The loose sketch layer — the brainstorming substrate that precedes ArchState.

Where ArchState is strict (validated, singular, handoff-ready), the sketchbook
is loose: freeform nodes and links, no required fields, several parallel
variants of the same feature, and a per-node depth slider you can move up OR
down. Nothing here is validated — it is a napkin. Strictness happens exactly
once, at promotion, when the chosen variant is replayed into ArchState through
the strict tools.

Design conversation that produced this: the opening move of an arch session
should be brainstorming, not form-filling — the agent sketches a rough shape (or
a couple of rival shapes) from the requirements, then the user and agent go
to-and-fro: add ideas, splice a node between two others, deepen or *collapse* a
node, spin off a rival variant. The brief accretes in the background; it only
has to be complete at promotion time.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# per-node fidelity — a two-way slider, not a one-way expand. "reduce the depth"
# is a first-class move: collapse a detailed node back to a stub.
DEPTHS: tuple[str, ...] = ("stub", "sketch", "detailed")
VARIANT_STATUSES: tuple[str, ...] = ("draft", "chosen", "archived")


@dataclass
class SketchNode:
    """A napkin box. `kind` is a free hint (service/store/queue/idea/...), NOT
    the strict KINDS enum — validation is deferred to promotion."""
    id: str
    label: str
    kind: str = "component"
    note: str = ""           # freeform: what it is / why it's here
    depth: str = "stub"      # stub | sketch | detailed
    detail: str = ""         # freeform internal sketch, filled as depth deepens


@dataclass
class SketchLink:
    src: str
    dst: str
    label: str = ""
    kind: str = "sync"       # loose hint: sync | async | batch | anything
    note: str = ""


@dataclass
class Variant:
    """One candidate architecture for the same feature. The rejected variants —
    with `rejected_reason` — are the ADR gold the strict layer otherwise loses."""
    id: str
    name: str
    summary: str = ""        # the idea/tradeoff this take explores, one line
    nodes: dict[str, SketchNode] = field(default_factory=dict)
    links: list[SketchLink] = field(default_factory=list)
    status: str = "draft"    # draft | chosen | archived
    rejected_reason: str = ""

    def link_index(self, src: str, dst: str, label: str | None = None) -> int:
        for i, ln in enumerate(self.links):
            if ln.src == src and ln.dst == dst and (label is None or ln.label == label):
                return i
        return -1

    def references_to(self, node_id: str) -> list[str]:
        return [f"{ln.src} -> {ln.dst}" for ln in self.links
                if node_id in (ln.src, ln.dst)]


@dataclass
class Sketchbook:
    """The loose layer as a whole: several variants, one in focus, plus notes
    the brief accretes from."""
    variants: dict[str, Variant] = field(default_factory=dict)
    active: str | None = None   # the variant currently in focus
    notes: list[str] = field(default_factory=list)

    # ---- convenience ----

    def active_variant(self) -> Variant | None:
        if self.active is None:
            return None
        return self.variants.get(self.active)

    def chosen_variant(self) -> Variant | None:
        return next((v for v in self.variants.values() if v.status == "chosen"), None)

    def is_empty(self) -> bool:
        return not self.variants

    # ---- serialization (ArchState.to_dict uses asdict, so we only rebuild) ----

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "Sketchbook":
        d = d or {}
        book = cls(active=d.get("active"), notes=list(d.get("notes", [])))
        for vid, v in (d.get("variants") or {}).items():
            book.variants[vid] = Variant(
                id=v.get("id", vid),
                name=v.get("name", vid),
                summary=v.get("summary", ""),
                nodes={
                    nid: SketchNode(
                        id=n.get("id", nid),
                        label=n.get("label", nid),
                        kind=n.get("kind", "component"),
                        note=n.get("note", ""),
                        depth=n.get("depth", "stub"),
                        detail=n.get("detail", ""),
                    )
                    for nid, n in (v.get("nodes") or {}).items()
                },
                links=[
                    SketchLink(
                        src=ln["src"], dst=ln["dst"],
                        label=ln.get("label", ""),
                        kind=ln.get("kind", "sync"),
                        note=ln.get("note", ""),
                    )
                    for ln in v.get("links", [])
                ],
                status=v.get("status", "draft"),
                rejected_reason=v.get("rejected_reason", ""),
            )
        return book

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
