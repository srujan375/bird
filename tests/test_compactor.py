import json

from bird.engine.compactor import (
    compact,
    estimate_tokens,
    needs_compaction,
    stub_tool_results,
)
from bird.llm.registry import Registry
from bird.llm.types import Message, ToolCall


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
    from bird.llm.wire.openai_compat import WireError

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


def read_transcript(paths, size=2000):
    """One assistant read call per path, each with its bulky result."""
    msgs = [
        Message(role="system", content="sys"),
        Message(role="user", content="Task: fix it"),
    ]
    for i, path in enumerate(paths):
        msgs.append(
            Message(
                role="assistant",
                tool_calls=[ToolCall.from_raw(f"c{i}", "read", json.dumps({"path": path}))],
            )
        )
        msgs.append(Message(role="tool", content=f"{path} body " + "x" * size, tool_call_id=f"c{i}"))
    return msgs


def test_actively_worked_file_survives_stubbing():
    # 12 reads; hot.py is the oldest, so pure recency drops it first — which is
    # exactly the read the model then has to repeat mid-edit.
    paths = ["hot.py"] + [f"other{i}.py" for i in range(11)]
    msgs = read_transcript(paths)
    unpinned, _ = stub_tool_results(msgs)
    assert "elided" in unpinned[3].content

    pinned, _ = stub_tool_results(msgs, keep_paths=frozenset({"hot.py"}))
    assert "hot.py body" in pinned[3].content


def test_only_the_newest_copy_of_a_hot_file_is_pinned():
    msgs = read_transcript(["hot.py", "hot.py"] + [f"other{i}.py" for i in range(10)])
    pinned, _ = stub_tool_results(msgs, keep_paths=frozenset({"hot.py"}))
    assert "elided" in pinned[3].content  # older copy still goes
    assert "hot.py body" in pinned[5].content  # newest one stays


def test_stub_names_the_file_and_how_to_get_it_back():
    msgs = read_transcript([f"f{i}.py" for i in range(12)])
    stubbed, _ = stub_tool_results(msgs)
    assert 'read {"path": "f0.py"}' in stubbed[3].content


def test_pinning_is_capped_so_it_cannot_starve_compaction():
    paths = [f"hot{i}.py" for i in range(12)]
    msgs = read_transcript(paths)
    _, count = stub_tool_results(msgs, keep_paths=frozenset(paths))
    assert count == 2  # 12 - 5 recent - 5 pinned


def test_batched_read_is_not_pinned_by_path():
    # a batch result holds several files, so it is not any one path's content
    msgs = read_transcript([f"other{i}.py" for i in range(11)])
    msgs.insert(
        2,
        Message(
            role="assistant",
            tool_calls=[
                ToolCall.from_raw("b0", "read", json.dumps({"path": "hot.py", "paths": ["b.py"]}))
            ],
        ),
    )
    msgs.insert(3, Message(role="tool", content="batched " + "x" * 2000, tool_call_id="b0"))
    stubbed, _ = stub_tool_results(msgs, keep_paths=frozenset({"hot.py"}))
    assert "elided" in stubbed[3].content
