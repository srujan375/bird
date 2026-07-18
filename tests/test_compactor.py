from mha.harness.compactor import (
    compact,
    estimate_tokens,
    needs_compaction,
    stub_tool_results,
)
from mha.llm.registry import Registry
from mha.llm.types import Message


def big_tool_msg(i, size=2000):
    return Message(role="tool", content=f"result {i} " + "x" * size, tool_call_id=f"c{i}")


def transcript(n_tools=12):
    msgs = [
        Message(role="system", content="sys"),
        Message(role="user", content="Task: fix it"),
    ]
    for i in range(n_tools):
        msgs.append(Message(role="assistant", content=f"turn {i}"))
        msgs.append(big_tool_msg(i))
    return msgs


def test_needs_compaction_trigger_at_90pct():
    msgs = [Message(role="user", content="x" * 4000)]  # ~1000 tokens
    assert not needs_compaction(msgs, 10000)
    assert needs_compaction(msgs, 1100)  # 90% of 1100 = 990 < ~1000


def test_stub_keeps_recent_tool_results():
    msgs = transcript(12)
    stubbed, count = stub_tool_results(msgs)
    assert count == 7  # 12 - 5 recent
    tool_msgs = [m for m in stubbed if m.role == "tool"]
    assert all("elided" in m.content for m in tool_msgs[:7])
    assert all("elided" not in m.content for m in tool_msgs[7:])
    # tool_call_id preserved so the wire format stays valid
    assert tool_msgs[0].tool_call_id == "c0"


def test_stub_leaves_small_results_alone():
    msgs = [Message(role="system", content="s")] + [
        Message(role="tool", content="tiny", tool_call_id=f"c{i}") for i in range(10)
    ]
    _, count = stub_tool_results(msgs)
    assert count == 0


def test_compact_offline_falls_back_to_trim(monkeypatch):
    """No compactor model reachable → stub + trim, never raises."""
    from mha.llm.wire.openai_compat import WireError

    class DeadClient:
        def complete(self, *a, **k):
            raise WireError("offline")

    registry = Registry(
        providers={}, models={}, aliases={"compactor": "openrouter:x"}
    )
    # resolving 'compactor' would fail (no provider) — patch resolve to raise WireError path
    msgs = transcript(30)
    events = []
    out = compact(
        msgs,
        context_window=2000,
        registry=Registry(providers={}, models={}, aliases={}),
        client=DeadClient(),
        record=lambda t, d: events.append((t, d)),
    )
    assert estimate_tokens(out) <= 0.90 * 2000 or len(out) <= 6
    # system + task survived
    assert out[0].role == "system"
    assert out[1].content == "Task: fix it"
    assert events and events[0][0] == "compaction"


def test_compact_records_event():
    msgs = transcript(8)
    events = []
    compact(msgs, 10**6, Registry(providers={}, models={}, aliases={}),
            client=None, record=lambda t, d: events.append((t, d)))
    assert events[0][1]["stage"] == "stub"
