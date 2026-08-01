"""Tool-call validation → helpful-error turns.

A small model that fumbles a tool call gets a structured explanation of what
it sent vs. what the schema wants (required/provided/missing), not a bare
traceback. The runner feeds this back as the tool result and retries
(2 retries per call, then structured abort — decision #7).
"""

from __future__ import annotations

import jsonschema

from .types import ToolCall, ToolSpec


def validate_tool_call(call: ToolCall, specs: dict[str, ToolSpec]) -> str | None:
    """Return None if the call is valid, else a helpful error string for the model."""
    spec = specs.get(call.name)
    if spec is None:
        return (
            f"Unknown tool '{call.name}'. Available tools: {', '.join(sorted(specs))}. "
            f"Call one of those instead."
        )

    if call.arguments is None:
        return (
            f"Tool '{call.name}' was called with invalid JSON arguments: "
            f"{call.arguments_json[:200]!r}. Arguments must be a single JSON object. "
            + _expected_summary(spec)
        )

    validator = jsonschema.Draft202012Validator(spec.parameters)
    errors = sorted(validator.iter_errors(call.arguments), key=lambda e: list(e.path))
    if not errors:
        return None

    required = spec.parameters.get("required", [])
    provided = sorted(call.arguments.keys())
    missing = [k for k in required if k not in call.arguments]
    problems = "; ".join(
        f"{'/'.join(str(p) for p in e.path) or '(top level)'}: {e.message}" for e in errors[:3]
    )
    return (
        f"Invalid arguments for tool '{call.name}'. "
        f"Required: {required}. Provided: {provided}. Missing: {missing}. "
        f"Problems: {problems}. " + _expected_summary(spec)
    )


def _expected_summary(spec: ToolSpec) -> str:
    props = spec.parameters.get("properties", {})
    required = set(spec.parameters.get("required", []))
    parts = []
    for name, schema in props.items():
        typ = schema.get("type", "any")
        parts.append(f"{name} ({typ}{', required' if name in required else ''})")
    return f"Expected parameters: {', '.join(parts)}."
