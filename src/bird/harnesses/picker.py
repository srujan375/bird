"""The picker: one question, a few rows, and a confirm button that names the act.

Ported from the Open Design handover for `01 · Option cards` (project
6c3bc949-70c9-4f65-9d3f-8756ede5e4ac, `handover-01-option-cards.html`). That
document is the spec; this module is its host half — the payload the page
renders, the answer it sends back, and the rules the handover says to enforce
before anything is drawn:

- 2 options minimum. One row is not a question.
- More than four cards in a chat column is the wrong component, so the variant
  falls back to the compact list rather than rendering five cards.
- The description is the row's *consequence*, not its feature. It is the thing
  that stops someone picking "Production" by reflex.
- Every ask carries a `summary_label` — the noun for the decision ("Deploy
  target"), which is what the answered row shows once the picker collapses.

Both harnesses ask through this. Arch parks questions the model raises; design
gates its intake behind them. One shape, so one component renders both.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

VARIANT_CARDS = "option-cards"
VARIANT_LIST = "compact-list"

# five cards in a chat column is a menu; past the cap the compact list takes over
MAX_CARDS = 4
MIN_OPTIONS = 2
# and past this it is not a picker at all — say so rather than rendering it
MAX_OPTIONS = 8

DEFAULT_HINT = "Pick one"


def _s(value: Any) -> str:
    return str(value or "").strip()


@dataclass
class Option:
    """One row. `label` is the noun; `description` is what taking it costs."""

    value: str
    label: str = ""
    description: str = ""
    disabled: bool = False
    disabled_reason: str = ""
    # the row the host would take. At most one, and it is a recommendation,
    # never a pre-commitment: `default` is what gets checked.
    rec: bool = False
    # opaque to the picker — arch uses it to name the approach a pick keeps
    # alive, and the design harness leaves it empty
    favor: str = ""

    def __post_init__(self) -> None:
        self.value = _s(self.value)
        self.label = _s(self.label) or self.value
        self.description = _s(self.description)
        self.disabled_reason = _s(self.disabled_reason)
        self.favor = _s(self.favor)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"value": self.value, "label": self.label}
        if self.description:
            out["description"] = self.description
        if self.disabled:
            out["disabled"] = True
            if self.disabled_reason:
                out["disabledReason"] = self.disabled_reason
        if self.rec:
            out["rec"] = True
        if self.favor:
            out["favor"] = self.favor
        return out

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Option":
        return cls(
            value=_s(d.get("value")) or _s(d.get("label")),
            label=_s(d.get("label")),
            description=_s(d.get("description")) or _s(d.get("cost")),
            disabled=bool(d.get("disabled")),
            disabled_reason=_s(d.get("disabledReason") or d.get("disabled_reason")),
            rec=bool(d.get("rec")),
            favor=_s(d.get("favor")),
        )


@dataclass
class Ask:
    """A question on the table, and its answer once there is one."""

    id: str
    prompt: str
    options: list[Option] = field(default_factory=list)
    hint: str = DEFAULT_HINT
    # the noun for the decision — what the answered row is labelled with
    summary_label: str = ""
    # the confirm button reads what is about to happen: "Use {v}", never "Continue"
    confirm_template: str = ""
    confirm_empty: str = ""
    # pre-checked row, if being wrong is cheap. Empty means confirm starts
    # disabled and neutral grey rather than tinted at low opacity.
    default: str = ""
    answer: str = ""
    answered: bool = False
    # they reopened it with Change after a first submit — the host may need to
    # undo something it already did
    revised: bool = False

    def __post_init__(self) -> None:
        self.id = _s(self.id)
        self.prompt = _s(self.prompt)
        self.hint = _s(self.hint) or DEFAULT_HINT
        self.summary_label = _s(self.summary_label)
        self.confirm_template = _s(self.confirm_template)
        self.confirm_empty = _s(self.confirm_empty)
        self.default = _s(self.default)
        self.answer = _s(self.answer)

    # ---- reading ----

    @property
    def variant(self) -> str:
        """Cards while the rows still carry their consequence and there are few
        enough of them; the compact list past that."""
        if len(self.options) > MAX_CARDS:
            return VARIANT_LIST
        return VARIANT_CARDS if any(o.description for o in self.options) else VARIANT_LIST

    @property
    def open(self) -> bool:
        return not self.answered

    def option(self, value: str) -> Option | None:
        want = _s(value)
        for o in self.options:
            if o.value == want:
                return o
        return None

    def answer_label(self) -> str:
        picked = self.option(self.answer)
        return picked.label if picked is not None else self.answer

    def confirm_label(self) -> str:
        """What the button says right now — the honest version of "Continue"."""
        picked = self.option(self.default if not self.answer else self.answer)
        if picked is None or not self.confirm_template:
            return self.confirm_empty or "Confirm"
        return self.confirm_template.replace("{v}", picked.label)

    # ---- writing ----

    def validate(self) -> None:
        """Refuse a payload rather than render it. A picker that cannot be
        answered is worse than a question asked in prose."""
        if not self.prompt:
            raise ValueError("a picker needs a question.")
        if len(self.options) < MIN_OPTIONS:
            raise ValueError(
                f"a picker needs at least {MIN_OPTIONS} options — "
                f"{len(self.options)} is a statement, not a question."
            )
        if len(self.options) > MAX_OPTIONS:
            raise ValueError(
                f"{len(self.options)} options is a menu, not a question — "
                f"{MAX_OPTIONS} is the ceiling."
            )
        seen: set[str] = set()
        for o in self.options:
            if not o.value:
                raise ValueError("every option needs a value.")
            if o.value in seen:
                raise ValueError(f"duplicate option value {o.value!r}.")
            seen.add(o.value)
        if all(o.disabled for o in self.options):
            raise ValueError("every option is disabled — there is nothing to pick.")
        if self.default and self.default not in seen:
            raise ValueError(f"default {self.default!r} is not one of the options.")

    def resolve(self, value: str) -> Option:
        """The option a client claims to have picked, or why it cannot be taken."""
        picked = self.option(value)
        if picked is None:
            known = ", ".join(o.value for o in self.options) or "none"
            raise ValueError(f"no option {_s(value)!r} on {self.id} (offered: {known}).")
        if picked.disabled:
            why = picked.disabled_reason or "it is disabled"
            raise ValueError(f"{picked.label} cannot be picked: {why}.")
        return picked

    def take(self, value: str) -> Option:
        """Record the answer. Answering twice is a revision, not an error —
        the user pressed Change, and the host may have work to undo."""
        picked = self.resolve(value)
        if self.answered and self.answer != picked.value:
            self.revised = True
        self.answer = picked.value
        self.answered = True
        return picked

    # ---- the wire ----

    def payload(self) -> dict[str, Any]:
        """Host → client, the shape the handover proposes."""
        out: dict[str, Any] = {
            "type": "picker",
            "variant": self.variant,
            "id": self.id,
            "prompt": self.prompt,
            "hint": self.hint,
            "summaryLabel": self.summary_label or self.prompt,
            "confirm": {
                "template": self.confirm_template or "Confirm {v}",
                "empty": self.confirm_empty or "Confirm",
            },
            "options": [o.to_dict() for o in self.options],
        }
        if self.default:
            out["default"] = self.default
        if self.answered:
            out["answered"] = True
            out["value"] = self.answer
            out["label"] = self.answer_label()
            if self.revised:
                out["revised"] = True
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "prompt": self.prompt,
            "options": [o.to_dict() for o in self.options],
            "hint": self.hint,
            "summary_label": self.summary_label,
            "confirm_template": self.confirm_template,
            "confirm_empty": self.confirm_empty,
            "default": self.default,
            "answer": self.answer,
            "answered": self.answered,
            "revised": self.revised,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Ask":
        return cls(
            id=_s(d.get("id")),
            prompt=_s(d.get("prompt") or d.get("question")),
            options=[Option.from_dict(o) for o in (d.get("options") or [])],
            hint=_s(d.get("hint")) or DEFAULT_HINT,
            summary_label=_s(d.get("summary_label") or d.get("summaryLabel")),
            confirm_template=_s(d.get("confirm_template")),
            confirm_empty=_s(d.get("confirm_empty")),
            default=_s(d.get("default")),
            answer=_s(d.get("answer")),
            answered=bool(d.get("answered")),
            revised=bool(d.get("revised")),
        )


class AskQueue:
    """Questions in the order they were raised, asked one at a time.

    The queue is the whole of "one by one": whatever is parked, exactly one ask
    is pending, and the next appears only when that one is answered. Both UIs
    render `pending()` and nothing else, so neither can get ahead of the user.
    """

    def __init__(self, asks: list[Ask] | None = None) -> None:
        self.asks: list[Ask] = list(asks or [])

    def __len__(self) -> int:
        return len(self.asks)

    def add(self, ask: Ask) -> Ask:
        ask.validate()
        if self.by_id(ask.id) is not None:
            raise ValueError(f"there is already a question {ask.id!r}.")
        self.asks.append(ask)
        return ask

    def drop(self, ask_id: str) -> bool:
        """Retire a question. Used when an earlier answer changes and the ones
        it raised no longer apply."""
        before = len(self.asks)
        self.asks = [a for a in self.asks if a.id != _s(ask_id)]
        return len(self.asks) != before

    def by_id(self, ask_id: str) -> Ask | None:
        want = _s(ask_id)
        return next((a for a in self.asks if a.id == want), None)

    def pending(self) -> Ask | None:
        """The one question on the table. Everything behind it waits."""
        return next((a for a in self.asks if a.open), None)

    def answered(self) -> list[Ask]:
        return [a for a in self.asks if a.answered]

    @property
    def settled(self) -> bool:
        return self.pending() is None

    def answer(self, ask_id: str, value: str) -> Ask:
        ask = self.by_id(ask_id)
        if ask is None:
            known = ", ".join(a.id for a in self.asks) or "none"
            raise ValueError(f"no question {_s(ask_id)!r} (known: {known}).")
        ask.take(value)
        return ask

    def value(self, ask_id: str, fallback: str = "") -> str:
        ask = self.by_id(ask_id)
        return ask.answer if ask is not None and ask.answered else fallback

    def to_list(self) -> list[dict[str, Any]]:
        return [a.to_dict() for a in self.asks]

    @classmethod
    def from_list(cls, data: Any) -> "AskQueue":
        return cls([Ask.from_dict(d) for d in (data or []) if isinstance(d, dict)])
