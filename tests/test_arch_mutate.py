"""POST /mutate — the page editing the architecture.

The whole point of the endpoint is that a user edit and a model edit are the
same edit: same validation, same amendment trail, same state push. These pin
that, plus the refusals — because an optimistic client is only safe to write if
the server actually says no.
"""

import json
import threading

import pytest

from mha.harnesses.arch.mutate import MutationError, apply_mutation
from mha.harnesses.arch.session import ArchSession
from mha.harnesses.arch.state import ArchState, Component, Concern, Connection, Flow, FlowStep
from mha.harnesses.arch.tools import ComponentTool
from mha.tools import ToolContext

from .test_arch_e2e import Page, build_stack


def make_session(**kw):
    events = []
    session = ArchSession(state=ArchState(), on_state=events.append, **kw)
    state = session.state
    state.phase = "propose"
    state.components["gw"] = Component(id="gw", name="gateway", kind="gateway",
                                       responsibility="http entry", trace=["shorten urls"])
    state.components["db"] = Component(id="db", name="url-db", kind="store",
                                       responsibility="mappings", trace=["shorten urls"],
                                       data_owned="short->long")
    state.connections.append(Connection(src="gw", dst="db", label="lookup", kind="sync"))
    state.flows.append(Flow(id="shorten", name="shorten", kind="happy",
                            steps=[FlowStep(src="gw", dst="db", action="INSERT")]))
    return session, events


# ---------------------------------------------------------------- components


def test_rename_changes_the_name_and_nothing_else():
    """The acceptance check from the handover: a rename must leave the id in
    every connection, flow and future bundle exactly where it was."""
    session, events = make_session()
    result = session.apply_mutation({"op": "component", "id": "db", "name": "postgres-urls"})

    assert "db" in result["applied"]
    assert session.state.components["db"].name == "postgres-urls"
    assert session.state.components["db"].id == "db"
    assert session.state.connections[0].dst == "db"
    assert session.state.flows[0].steps[0].dst == "db"
    # the edit reaches the page the same way a tool call does
    assert events[-1]["type"] == "arch_state"
    assert events[-1]["changed"] == {"kind": "component", "id": "db"}


def test_editing_responsibility_clears_the_gap_it_was_causing():
    session, _ = make_session()
    session.state.components["gw"].responsibility = ""
    assert "gw" in session.state.gaps_by_subject()

    session.apply_mutation({"op": "component", "id": "gw", "responsibility": "terminates TLS"})
    assert "gw" not in session.state.gaps_by_subject()


def test_a_user_edit_goes_through_the_same_validation_as_the_tool(tmp_path):
    """Same code path, so a payload the tool would refuse is refused here too."""
    session, _ = make_session()
    ctx = ToolContext(repo_root=tmp_path, arch=session)
    tool_said = ComponentTool().execute({"id": "db", "kind": "nonsense"}, ctx)
    assert tool_said.is_error

    with pytest.raises(MutationError) as e:
        apply_mutation(session, {"op": "component", "id": "db", "kind": "nonsense"})
    # kind isn't user-editable at all, so it never even reaches validation
    assert "nothing to change" in str(e.value)


@pytest.mark.parametrize("payload, expected", [
    ({"op": "component", "id": "nope", "name": "x"}, "no component 'nope'"),
    ({"op": "component", "name": "x"}, "which component"),
    ({"op": "component", "id": "db", "name": "  "}, "needs a name"),
    ({"op": "component", "id": "db", "trace": "one goal"}, "trace must be a list"),
    ({"op": "component", "id": "db"}, "nothing to change"),
    ({"op": "wat", "id": "db"}, "unknown mutation"),
])
def test_refusals(payload, expected):
    session, events = make_session()
    before = json.dumps(session.state.to_dict(), default=str)
    with pytest.raises(MutationError) as e:
        session.apply_mutation(payload)
    assert expected in str(e.value)
    # a refused mutation changes nothing and pushes nothing
    assert json.dumps(session.state.to_dict(), default=str) == before
    assert events == []


def test_structural_fields_are_not_user_editable():
    """Ids are immutable and `kind` is the architect's call; asking for either
    is a no-op refusal rather than a silent partial edit."""
    session, _ = make_session()
    with pytest.raises(MutationError):
        session.apply_mutation({"op": "component", "id": "db", "kind": "service", "remove": True})
    assert session.state.components["db"].kind == "store"


def test_post_approval_edit_records_who_made_it():
    session, _ = make_session()
    session.state.phase = "expand"  # the user has approved the top level
    session.apply_mutation({"op": "component", "id": "gw", "responsibility": "terminates TLS"})

    assert len(session.state.amendments) == 1
    amendment = session.state.amendments[0]
    assert amendment.description.startswith("user edit")
    assert amendment.structural is False  # prose doesn't re-open the approval


def test_edits_before_approval_leave_no_amendment():
    session, _ = make_session()
    session.apply_mutation({"op": "component", "id": "gw", "responsibility": "terminates TLS"})
    assert session.state.amendments == []


# ------------------------------------------------------------------ concerns


def test_overruling_a_concern_from_the_rail_keeps_the_reason():
    session, _ = make_session()
    session.state.concerns.append(
        Concern(id="c1", severity="blocker", target="db", claim="unbounded growth")
    )
    session.apply_mutation({
        "op": "concern", "id": "c1", "status": "overruled",
        "resolution": "single-tenant, we prune by hand for now",
    })
    c = session.state.concerns[0]
    assert c.status == "overruled"
    assert c.resolution == "single-tenant, we prune by hand for now"
    assert session.state.open_blockers() == []


def test_overruling_without_a_reason_is_refused():
    """The reason IS the record — the thing the code harness inherits. A
    placeholder in its place is worse than a refusal."""
    session, _ = make_session()
    session.state.concerns.append(Concern(id="c1", severity="blocker", target="db", claim="x"))
    with pytest.raises(MutationError) as e:
        session.apply_mutation({"op": "concern", "id": "c1", "status": "overruled"})
    assert "needs a reason" in str(e.value)
    assert session.state.concerns[0].status == "open"


def test_accepting_a_concern_needs_no_reason():
    session, _ = make_session()
    session.state.concerns.append(Concern(id="c1", severity="risk", target="db", claim="x"))
    session.apply_mutation({"op": "concern", "id": "c1", "status": "accepted"})
    assert session.state.concerns[0].status == "accepted"


@pytest.mark.parametrize("payload, expected", [
    ({"op": "concern", "id": "c9", "status": "accepted"}, "no concern 'c9'"),
    ({"op": "concern", "id": "c1", "status": "open"}, "status must be one of"),
])
def test_concern_refusals(payload, expected):
    session, _ = make_session()
    session.state.concerns.append(Concern(id="c1", severity="risk", target="db", claim="x"))
    with pytest.raises(MutationError) as e:
        session.apply_mutation(payload)
    assert expected in str(e.value)


# ------------------------------------------------------------------ promote


def test_promoting_a_variant_from_the_canvas_seeds_the_design():
    from mha.harnesses.arch.sketch import SketchLink, SketchNode, Variant

    session, _ = make_session()
    session.state.components.clear()
    session.state.connections.clear()
    session.state.flows.clear()
    session.state.phase = "brainstorm"
    v = Variant(id="v2", name="evented", summary="bus in the middle")
    v.nodes["api"] = SketchNode(id="api", label="api", kind="service", note="entry")
    v.nodes["bus"] = SketchNode(id="bus", label="events", kind="queue", note="")
    v.links.append(SketchLink(src="api", dst="bus", label="publish", kind="async"))
    session.state.sketchbook.variants["v2"] = v

    result = session.apply_mutation({"op": "promote", "variant_id": "v2"})

    assert "evented" in result["applied"]
    assert set(session.state.components) == {"api", "bus"}
    assert session.state.components["bus"].kind == "queue"
    assert session.state.sketchbook.variants["v2"].status == "chosen"
    assert session.state.phase == "propose"


def test_promoting_an_empty_variant_is_refused():
    from mha.harnesses.arch.sketch import Variant

    session, _ = make_session()
    session.state.sketchbook.variants["v3"] = Variant(id="v3", name="empty", summary="")
    with pytest.raises(MutationError) as e:
        session.apply_mutation({"op": "promote", "variant_id": "v3"})
    assert "nothing sketched" in str(e.value)


# ------------------------------------------------------------------- lifecycle


def test_a_finalized_session_refuses_every_mutation():
    session, _ = make_session()
    session.state.phase = "finalized"
    with pytest.raises(MutationError) as e:
        session.apply_mutation({"op": "component", "id": "db", "name": "anything"})
    assert "read-only" in str(e.value)


def test_mutation_persists_to_the_state_file(tmp_path):
    session, _ = make_session(run_dir=tmp_path / "run")
    session.apply_mutation({"op": "component", "id": "db", "name": "postgres-urls"})
    saved = json.loads((tmp_path / "run" / "arch_state.json").read_text())
    assert saved["components"]["db"]["name"] == "postgres-urls"


# ----------------------------------------------------------------- over HTTP


def test_mutate_over_http(tmp_path):
    """The route itself: 200 + a state push for an accepted edit, 400 with a
    readable message for a refused one, and no session left behind either way."""
    server, transport, arch, _ = build_stack(tmp_path, [])
    arch.state.phase = "propose"
    arch.state.components["db"] = Component(id="db", name="url-db", kind="store",
                                            responsibility="mappings", trace=["urls"])
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    host, port = transport._server.server_address[:2]
    page = Page(host, port)
    try:
        body = page.post("/mutate", {"op": "component", "id": "db", "name": "postgres-urls"})
        assert body["ok"] is True
        assert arch.state.components["db"].name == "postgres-urls"
        pushed = page.wait_for(
            lambda e: e["type"] == "arch_state"
            and e["state"]["components"]["db"]["name"] == "postgres-urls",
            "renamed state push",
        )
        assert pushed["changed"] == {"kind": "component", "id": "db"}

        body = page.post("/mutate", {"op": "component", "id": "ghost", "name": "x"}, status=400)
        assert body["ok"] is False
        assert "ghost" in body["error"]
    finally:
        transport.shutdown()
        thread.join(timeout=5)


def test_mutate_is_refused_when_the_session_has_no_arch_state(tmp_path):
    """`mha code` and the plain REPL share this pump; the route must decline
    politely rather than explode."""
    server, transport, arch, _ = build_stack(tmp_path, [])
    server.repl.runner.ctx.arch = None
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    host, port = transport._server.server_address[:2]
    page = Page(host, port)
    try:
        body = page.post("/mutate", {"op": "component", "id": "db", "name": "x"}, status=400)
        assert body["ok"] is False
        assert "no state a UI can edit" in body["error"]
    finally:
        transport.shutdown()
        thread.join(timeout=5)
