"""The picker: the rules the Open Design handover says to enforce before a
question is ever drawn, and the queue that makes questions arrive one at a time.

Source of truth for the spec: design-workbench/pickers/handover-01-option-cards.html.
"""

import pytest

from bird.harnesses.picker import (
    VARIANT_CARDS,
    VARIANT_LIST,
    Ask,
    AskQueue,
    Option,
)


def _ask(**kw) -> Ask:
    base = dict(
        id="deploy-target",
        prompt="Where should I deploy this branch?",
        summary_label="Deploy target",
        confirm_template="Deploy to {v}",
        confirm_empty="Deploy",
        options=[
            Option(value="preview", label="Preview", description="Throwaway URL, deleted after 7 days."),
            Option(value="staging", label="Staging", description="Shared with the team, resets nightly."),
            Option(value="production", label="Production", description="Live traffic. Needs a second approval."),
        ],
    )
    base.update(kw)
    return Ask(**base)


# ---- the shape of the payload ----


def test_payload_is_the_handover_contract():
    ask = _ask(default="staging")
    p = ask.payload()
    assert p["type"] == "picker" and p["variant"] == VARIANT_CARDS
    assert p["id"] == "deploy-target"
    assert p["prompt"] == "Where should I deploy this branch?"
    assert p["hint"] == "Pick one", "the hint sets the expectation before the eye reaches the rows"
    assert p["summaryLabel"] == "Deploy target"
    assert p["confirm"] == {"template": "Deploy to {v}", "empty": "Deploy"}
    assert p["default"] == "staging"
    assert p["options"][1] == {
        "value": "staging", "label": "Staging",
        "description": "Shared with the team, resets nightly.",
    }
    assert "answered" not in p


def test_a_disabled_row_carries_its_reason():
    ask = _ask(options=[
        Option(value="staging", label="Staging", description="Resets nightly."),
        Option(value="production", label="Production", description="Live traffic.",
               disabled=True, disabled_reason="needs an approval you don't have"),
    ])
    rows = ask.payload()["options"]
    assert rows[1]["disabled"] is True
    assert rows[1]["disabledReason"] == "needs an approval you don't have"


def test_the_confirm_button_names_the_act():
    """"Continue" is the one label the handover forbids: the button says what
    is about to happen, built from the row that is selected."""
    assert _ask(default="production").confirm_label() == "Deploy to Production"
    assert _ask().confirm_label() == "Deploy", "no selection, no promise"


# ---- which component this is ----


def test_more_than_four_rows_falls_back_to_the_compact_list():
    """Five cards in a chat column is the wrong component — the handover says
    to fall back rather than render it."""
    rows = [Option(value=f"o{i}", label=f"Option {i}", description="a line") for i in range(5)]
    assert _ask(options=rows).variant == VARIANT_LIST
    assert _ask(options=rows[:4]).variant == VARIANT_CARDS


def test_rows_with_nothing_to_explain_are_a_list_not_cards():
    """Cards exist for the description. Without one there is nothing for the
    second line to hold, and a card is a list row with padding."""
    ask = _ask(options=[Option(value="7d", label="7 days"), Option(value="30d", label="30 days")])
    assert ask.variant == VARIANT_LIST


# ---- what it refuses to render ----


@pytest.mark.parametrize("rows, why", [
    ([Option(value="only", label="Only")], "one row is a statement"),
    ([Option(value=f"o{i}") for i in range(9)], "nine rows is a menu"),
    ([Option(value="same"), Option(value="same")], "two rows, one answer"),
    ([Option(value="a", disabled=True), Option(value="b", disabled=True)], "nothing pickable"),
])
def test_a_picker_that_cannot_be_answered_is_refused(rows, why):
    with pytest.raises(ValueError):
        _ask(options=rows).validate()


def test_a_default_has_to_be_one_of_the_options():
    with pytest.raises(ValueError, match="not one of the options"):
        _ask(default="canary").validate()


def test_an_unoffered_value_cannot_be_taken():
    with pytest.raises(ValueError, match="no option 'canary'"):
        _ask().take("canary")


def test_a_disabled_row_cannot_be_taken_and_says_why():
    ask = _ask(options=[
        Option(value="staging", label="Staging"),
        Option(value="production", label="Production", disabled=True,
               disabled_reason="it needs a second approval"),
    ])
    with pytest.raises(ValueError, match="second approval"):
        ask.take("production")


# ---- answering, and changing the answer ----


def test_taking_a_row_settles_the_ask():
    ask = _ask()
    ask.take("staging")
    assert ask.answered and not ask.open
    assert ask.answer == "staging" and ask.answer_label() == "Staging"
    p = ask.payload()
    assert p["answered"] is True and p["value"] == "staging" and p["label"] == "Staging"
    assert "revised" not in p


def test_changing_the_answer_is_flagged_as_a_revision():
    """`revised` is how the host learns it may have work to undo — they
    reopened the picker with Change after a first submit."""
    ask = _ask()
    ask.take("staging")
    ask.take("production")
    assert ask.answer == "production" and ask.revised
    assert ask.payload()["revised"] is True


def test_reconfirming_the_same_row_is_not_a_revision():
    ask = _ask()
    ask.take("staging")
    ask.take("staging")
    assert not ask.revised


def test_an_ask_round_trips_through_its_dict():
    ask = _ask(default="staging")
    ask.take("production")
    back = Ask.from_dict(ask.to_dict())
    assert back.payload() == ask.payload()


# ---- the queue: one question at a time ----


def test_only_one_question_is_ever_pending():
    q = AskQueue()
    q.add(_ask(id="one"))
    q.add(_ask(id="two"))
    assert q.pending().id == "one", "the second waits behind the first"
    q.answer("one", "staging")
    assert q.pending().id == "two", "and appears when the first is answered"
    q.answer("two", "preview")
    assert q.pending() is None and q.settled


def test_a_queue_refuses_a_second_question_under_one_id():
    q = AskQueue()
    q.add(_ask(id="one"))
    with pytest.raises(ValueError, match="already a question"):
        q.add(_ask(id="one"))


def test_a_queue_refuses_a_question_it_could_not_render():
    q = AskQueue()
    with pytest.raises(ValueError):
        q.add(_ask(options=[Option(value="only")]))
    assert not len(q), "and does not park the unrenderable one"


def test_answering_an_unknown_question_names_the_ones_it_has():
    q = AskQueue()
    q.add(_ask(id="one"))
    with pytest.raises(ValueError, match="known: one"):
        q.answer("nope", "staging")


def test_a_question_an_answer_retired_can_be_dropped():
    q = AskQueue()
    q.add(_ask(id="one"))
    q.add(_ask(id="two"))
    assert q.drop("two") and not q.drop("two")
    q.answer("one", "staging")
    assert q.settled


def test_the_queue_round_trips_through_its_list():
    q = AskQueue()
    q.add(_ask(id="one"))
    q.answer("one", "staging")
    q.add(_ask(id="two"))
    back = AskQueue.from_list(q.to_list())
    assert back.value("one") == "staging"
    assert back.pending().id == "two"
