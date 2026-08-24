"""ArchSession — the harness-owned wrapper the arch tools mutate through.

Holds the ArchState plus the little wiring the tools need: the arch_state event
sink (straight to the transport — arch_state is a top-level protocol event, not
a harness_event) and per-mutation persistence.

There is no broker here, and no critic. There are no gates to broker: the
session ends when the user says it is done, so nothing blocks a turn waiting on
a modal. And the background critic is gone by design — the user is in the room
the whole time, which makes them the critic; the deterministic checks that used
to feed it live in `derive.py` and reach the architect as material for its next
recommendation instead of as a third voice.

The write lock stays: `apply_mutation` arrives on an HTTP thread, so a page edit
must not interleave with a tool call into a half-written state file.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from collections.abc import Sequence
from typing import Any, Callable

from . import derive, render
from .state import ArchState

STATE_FILENAME = "arch_state.json"

# How many turns a user's edit stays in front of the architect.
#
# The tracker is re-rendered every loop iteration, so draining it on the first
# render would show an edit once and then delete it from the conversation
# mid-turn — the architect would glimpse it and lose it. Holding each edit for
# a few renders means it is genuinely read, without lingering forever and
# inviting the architect to react to the same edit twice.
SHOW_EDITS_FOR = 3

# How a board edit announces itself as a turn. The page reads this to render it
# as something the user *did* rather than something they typed.
BOARD_EDIT_PREFIX = "[the user changed the board]"

# How a message says which boxes the user had selected when they sent it. The
# page reads this back so the selection shows as context on the turn rather
# than as words the user typed.
FOCUS_PREFIX = "[the user is pointing at]"

# A page can only select one box today. The cap is here because the id list
# arrives over HTTP and nothing else bounds it.
MAX_FOCUS = 8


class ArchSession:
    def __init__(
        self,
        state: ArchState | None = None,
        run_dir: Path | None = None,
        on_state: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.state = state or ArchState()
        self.run_dir = run_dir
        self.on_state = on_state
        self._write_lock = threading.RLock()  # touched() nests inside apply_mutation
        self._user_edits: list[list[Any]] = []  # [text, renders_so_far]

    def state_event(self, changed: dict[str, str] | None = None) -> dict[str, Any]:
        """The full-replacement push. `status` is what the transport's stop
        condition reads — a session is open until it is handed off."""
        return {
            "type": "arch_state",
            "status": "handed_off" if self.state.handed_off else "open",
            "state": self.state.to_dict(),
            "renders": render.render_all(self.state),
            # what the harness noticed, keyed for the page to badge a node. The
            # architect gets the same material through its own state note; the
            # page shows it so the user can point at it, never as a task list.
            "noticing": derive.coverage(self.state),
            # how much the user has drawn that the architect has not been shown
            # yet. The page badges its submit with this, and reading it off the
            # harness means a refresh does not lose the count.
            "pending_edits": self.pending_edit_count(),
            "changed": changed,
        }

    def touched(self, changed_kind: str | None = None, changed_id: str | None = None) -> None:
        """Persist + push the full state after every mutation (mid-turn)."""
        with self._write_lock:
            if self.run_dir is not None:
                self.run_dir.mkdir(parents=True, exist_ok=True)
                (self.run_dir / STATE_FILENAME).write_text(
                    json.dumps(self.state.to_dict(), ensure_ascii=False, indent=1),
                    encoding="utf-8",
                )
            if self.on_state is not None:
                changed = None
                if changed_kind is not None and changed_id is not None:
                    changed = {"kind": changed_kind, "id": changed_id}
                self.on_state(self.state_event(changed))

    def apply_mutation(self, payload: dict[str, Any]) -> dict[str, Any]:
        """A user edit made on the page, applied through the model's own code
        paths. Duck-typed on purpose: the pump finds this by name on `ctx.arch`,
        so `POST /mutate` needs no knowledge that the arch harness exists.
        Raises MutationError, which the transport turns into a 400."""
        from .mutate import apply_mutation

        with self._write_lock:  # touched() re-enters it on this thread
            return apply_mutation(self, payload)

    # ---- what the user did to the board ----

    def note_user_edit(self, text: str) -> None:
        """Record something the person changed on the page.

        The design itself already reflects it — the state is shared — but the
        state is a snapshot and cannot say *who* moved something or that a
        label is new. A rename or a pinned note is the user talking; it should
        reach the architect as plainly as a typed message would.
        """
        with self._write_lock:
            for entry in self._user_edits:
                if entry[0] == text:
                    entry[1] = 0  # said again: start its life over
                    return
            self._user_edits.append([text, 0])

    def take_user_edits(self) -> list[str]:
        """The edits to show this turn, ageing out the ones already read."""
        with self._write_lock:
            out = [text for text, _ in self._user_edits]
            for entry in self._user_edits:
                entry[1] += 1
            self._user_edits = [e for e in self._user_edits if e[1] < SHOW_EDITS_FOR]
            return out

    def pending_edit_count(self) -> int:
        """Edits the architect has not seen. Only these are worth submitting."""
        with self._write_lock:
            return sum(1 for _, renders in self._user_edits if renders == 0)

    def compose_activity_prompt(self) -> str | None:
        """A turn's worth of prompt from what the user just drew, or None.

        Only edits the architect has not already seen count. An edit made while
        a turn was running has reached it through the pinned note, and asking it
        to respond to the same gesture a second time is worse than not asking.
        """
        with self._write_lock:
            fresh = [text for text, renders in self._user_edits if renders == 0]
            if not fresh:
                return None
            # they are delivered now — by this prompt rather than by the note
            self._user_edits = [e for e in self._user_edits if e[1] > 0]
        # Just what they did. How to answer it is in the system prompt, where
        # it costs nothing to repeat; restating it every turn is tokens spent
        # on something the architect already knows.
        lines = "\n".join(f"- {text}" for text in fresh)
        return f"{BOARD_EDIT_PREFIX}\n{lines}"

    def describe_subjects(self, ids: Sequence[str]) -> str | None:
        """The boxes the user had selected, described for the architect.

        Selection lives in the page — the harness has no idea what is
        highlighted until a message says so. Sending each box's own details
        rather than just its id is the whole point: it lets "why this one?" be
        answered without a lookup and without guessing which box "this" meant.

        Unknown ids are dropped rather than reported. A selection that no
        longer resolves is the page being a moment behind the graph, which is
        not something to make the architect explain.
        """
        with self._write_lock:
            blocks = [
                self._describe_node(nid)
                for nid in list(dict.fromkeys(ids))[:MAX_FOCUS]
            ]
        kept = [b for b in blocks if b]
        if not kept:
            return None
        return "\n".join([FOCUS_PREFIX, *kept])

    def _describe_node(self, node_id: str) -> str | None:
        """One box: a `- label` bullet the page reads back for the transcript,
        plus indented lines it ignores and the architect does not."""
        node = self.state.nodes.get(node_id)
        if node is None:
            return None
        named = [
            self.state.approaches[a].name
            for a in node.approaches
            if a in self.state.approaches
        ]
        facts = [f"id: {node.id}", f"kind: {node.kind}", f"depth: {node.depth}"]
        facts.append(
            "approach: " + (", ".join(named) if named else "shared by every approach")
        )
        if self.state.is_greyed(node):
            facts.append("on an approach that was not taken")
        if node.existing:
            facts.append("already in the repo — background, not ours to design")

        lines = [f"- {node.label}", "  " + " · ".join(facts)]
        for name, value in (
            ("owns", node.responsibility),
            ("built on", node.tech),
            ("inside", node.detail),
            ("noted", node.notes),
        ):
            if value:
                lines.append(f"  {name}: {value}")

        wires = [
            f"→ {self._node_name(e.dst)}" + (f" ({e.label})" if e.label else "")
            for e in self.state.edges if e.src == node.id
        ] + [
            f"← {self._node_name(e.src)}" + (f" ({e.label})" if e.label else "")
            for e in self.state.edges if e.dst == node.id
        ]
        if wires:
            lines.append("  wires: " + " · ".join(wires))
        return "\n".join(lines)

    def _node_name(self, node_id: str) -> str:
        node = self.state.nodes.get(node_id)
        return node.label if node is not None else node_id

    def next_question_id(self) -> str:
        return self.state.next_id("q", self.state.questions)

    def next_decision_id(self) -> str:
        return self.state.next_id("d", self.state.decisions)

    @classmethod
    def load(cls, run_dir: Path, **kw: Any) -> "ArchSession":
        """Restore persisted state for --resume; fresh state if none saved.
        A state file from the pre-rebuild harness raises LegacyStateError — see
        `ArchState.from_dict`."""
        path = run_dir / STATE_FILENAME
        state = None
        if path.is_file():
            state = ArchState.from_dict(json.loads(path.read_text(encoding="utf-8")))
        return cls(state=state, run_dir=run_dir, **kw)
