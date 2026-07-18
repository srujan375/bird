"""read / edit / write — the file tools."""

from __future__ import annotations

from typing import Any

from .base import Tool, ToolContext, ToolError, ToolResult

MAX_READ_CHARS = 24_000


class ReadTool(Tool):
    name = "read"
    description = "Read a file. Returns the exact file content."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Repo-relative file path"},
            "offset": {"type": "integer", "description": "1-based line to start from"},
            "limit": {"type": "integer", "description": "Max lines to return"},
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        p = ctx.resolve_path(args["path"])
        if not p.is_file():
            raise ToolError(f"file not found: {args['path']}")
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            raise ToolError(f"cannot read {args['path']}: {e}") from e

        lines = text.splitlines(keepends=True)
        total = len(lines)
        offset = max(args.get("offset", 1), 1)
        limit = args.get("limit")
        window = lines[offset - 1 : offset - 1 + limit if limit else None]
        out = "".join(window)
        note = ""
        if len(out) > MAX_READ_CHARS:
            out = out[:MAX_READ_CHARS]
            note = f"\n... [truncated; file has {total} lines — use offset/limit]"
        elif offset > 1 or (limit and offset - 1 + limit < total):
            note = f"\n[showing lines {offset}-{offset - 1 + len(window)} of {total}]"
        return ToolResult(output=out + note, details={"path": args["path"], "lines": total})


class EditTool(Tool):
    name = "edit"
    description = (
        "Replace text in a file. old_text must appear EXACTLY ONCE in the file; "
        "copy it verbatim from read output, including whitespace."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Repo-relative file path"},
            "old_text": {"type": "string", "description": "Exact text to replace"},
            "new_text": {"type": "string", "description": "Replacement text"},
        },
        "required": ["path", "old_text", "new_text"],
        "additionalProperties": False,
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        p = ctx.resolve_path(args["path"])
        if not p.is_file():
            raise ToolError(f"file not found: {args['path']}")
        text = p.read_text(encoding="utf-8")
        old, new = args["old_text"], args["new_text"]
        if old == new:
            raise ToolError("old_text and new_text are identical")
        count = text.count(old)
        if count == 0:
            raise ToolError(
                f"old_text not found in {args['path']}. Read the file and copy the "
                f"text exactly, including indentation."
            )
        if count > 1:
            raise ToolError(
                f"old_text appears {count} times in {args['path']}; include more "
                f"surrounding lines to make it unique."
            )
        p.write_text(text.replace(old, new, 1), encoding="utf-8")
        return ToolResult(
            output=f"Edited {args['path']}.",
            details={"path": args["path"], "old_text": old, "new_text": new},
        )


class WriteTool(Tool):
    name = "write"
    description = "Create or overwrite a file with the given content."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Repo-relative file path"},
            "content": {"type": "string", "description": "Full file content"},
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        p = ctx.resolve_path(args["path"])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(args["content"], encoding="utf-8")
        return ToolResult(
            output=f"Wrote {args['path']} ({len(args['content'])} chars).",
            details={"path": args["path"], "bytes": len(args["content"].encode())},
        )
