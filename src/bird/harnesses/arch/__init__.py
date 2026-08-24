"""The Architecture harness: a design conversation with a memory.

The model and the user walk the design tree together, one or two questions at a
time. Structure accretes on a single canvas as they talk — rival approaches
side by side, boxes deepening as their branch is visited — and the session ends
when the user says it does, leaving a handoff bundle behind.

`state.py` is the one graph; `derive.py` is everything the harness works out
for itself (the frontier, what's thin); `tools.py` is the architect's notebook.
"""
