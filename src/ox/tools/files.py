"""read / edit / write — the file tools."""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from typing import Any

from ..llm.registry import RegistryError
from ..llm.types import ContentPart, Message
from .base import Tool, ToolContext, ToolError, ToolResult

MAX_READ_CHARS = 24_000

# Pre-read size guard for the vision sidecar: a 4MB image base64-encodes to
# ~5.3MB, within most provider request-body limits. Checked via stat() before
# any bytes are read, so a path to /dev/zero can't OOM the runner.
MAX_IMAGE_BYTES = 4 * 1024 * 1024

# Raster image MIME types the vision sidecar accepts. SVG is deliberately
# excluded — it is vector XML, not a raster image, so it stays on the text
# read path (and read_image refuses it with a clear error).
_RASTER_IMAGE_MIMES = {
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "image/bmp",
    "image/tiff",
}

# Magic-byte signatures for extensionless image files: mimetypes.guess_type is
# extension-only, so a file saved as "screenshot" with no extension gets
# (None, None). These leading bytes identify the format from content.
_IMAGE_MAGIC = [
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"RIFF", "image/webp"),  # RIFF....WEBP — confirmed by the WEBP tag below
]

# BOMs that mark a file as text regardless of any null bytes (UTF-16/32 files
# contain null bytes in their normal encoding and would otherwise be
# misclassified as binary by the null-byte sniff).
_TEXT_BOMS = [
    (b"\xef\xbb\xbf", "utf-8"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
    (b"\x00\x00\xff\xfe", "utf-32-le"),
    (b"\xff\xfe\x00\x00", "utf-32-be"),
]


def _detect_image_mime(path: Path) -> tuple[str | None, bool]:
    """Detect whether a file is a raster image.

    Returns ``(mime, is_raster_image)``. `is_raster_image` is True only for
    raster formats the vision sidecar can process — SVG (image/svg+xml) is
    excluded because it is vector XML, not a raster image.

    Shared by both `read` (to decide whether to nudge to read_image) and
    `read_image` (to decide whether to accept), so the two tools can never
    disagree: they call the same function with the same logic.

    Detection order: extension via stdlib mimetypes, then magic-byte sniff of
    the first few bytes for extensionless files. No third-party deps.
    """
    mime, _ = mimetypes.guess_type(str(path))
    if mime == "image/svg+xml":
        return mime, False  # SVG is text, not a raster image
    if mime in _RASTER_IMAGE_MIMES:
        return mime, True
    # Extension unknown or non-image: sniff the leading bytes.
    try:
        with path.open("rb") as f:
            head = f.read(16)
    except OSError:
        return mime, False
    for sig, sig_mime in _IMAGE_MAGIC:
        if head.startswith(sig):
            # WebP is RIFF....WEBP — confirm the format tag, not just RIFF.
            if sig_mime == "image/webp" and head[8:12] != b"WEBP":
                continue
            return sig_mime, True
    return mime, False


def _looks_binary(path: Path) -> bool:
    """Heuristic: is this a binary (non-text) file? Used by `read` to decide
    whether to nudge toward read_image when the extension is unknown.

    A BOM (UTF-8/16/32) means text regardless of null bytes. Otherwise a null
    byte in the first 1024 bytes marks the file as binary. mimetypes has
    already been consulted for the image case before this runs.
    """
    try:
        with path.open("rb") as f:
            head = f.read(1024)
    except OSError:
        return False
    for bom, _ in _TEXT_BOMS:
        if head.startswith(bom):
            return False
    return b"\x00" in head


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

        # Image detection: a raster image file is garbled if read as text, so
        # nudge the model to read_image instead. SVG is excluded (it is XML
        # text) and falls through to normal reading below.
        mime, is_raster = _detect_image_mime(p)
        if is_raster:
            return ToolResult(
                output=(
                    f"{args['path']} is a binary image file ({mime}). Reading it as "
                    f"text would show garbled bytes — use the read_image tool to "
                    f"have the vision model describe it instead."
                ),
                details={"path": args["path"], "mime": mime, "nudge": "read_image"},
            )

        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            raise ToolError(f"cannot read {args['path']}: {e}") from e

        # An extensionless binary file that isn't a recognized image still
        # gets the nudge — the model can try read_image and, if it isn't an
        # image, fall back to reading the raw bytes.
        if mime is None and _looks_binary(p):
            return ToolResult(
                output=(
                    f"{args['path']} appears to be a binary file. Reading it as text "
                    f"may show garbled bytes — if it is an image, use the read_image "
                    f"tool to describe it; otherwise read the raw bytes with errors "
                    f"replaced (shown below).\n\n{text}"
                ),
                details={"path": args["path"], "binary": True, "nudge": "read_image"},
            )

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


class ReadImageTool(Tool):
    """Read an image file via the vision model sidecar.

    The main conversation model never sees image bytes: this tool's parameter
    schema has no field for image data, only a path string. The tool reads
    the file from disk, base64-encodes it, builds a content-parts message, and
    sends it to the vision model (resolved via the 'vision' registry alias).
    The model's text description comes back as a normal ToolResult — no image
    bytes enter the transcript.
    """

    name = "read_image"
    description = (
        "Read an image file (.png, .jpg, .jpeg, .gif, .webp, .bmp, .tiff) by "
        "sending it to a vision model and returning its text description. Use "
        "this for any image file — the main model cannot see image bytes "
        "directly. Pass an optional question to focus the description."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Repo-relative image file path"},
            "question": {
                "type": "string",
                "description": "Optional question about the image (default: "
                "'describe this image in detail')",
            },
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path_str = args["path"]
        question = args.get("question") or "describe this image in detail"
        p = ctx.resolve_path(path_str)
        if not p.is_file():
            raise ToolError(f"file not found: {path_str}")

        # SVG is a vector format — vision models can't process it and it is
        # XML text, so the model should use `read` instead.
        mime, is_raster = _detect_image_mime(p)
        if mime == "image/svg+xml":
            raise ToolError(
                "SVG is a vector format — use read to see the XML source, or "
                "rasterize it first."
            )
        if not is_raster:
            raise ToolError(f"{path_str} is not a recognized image format")

        # Pre-read size guard: refuse before reading any bytes so a huge file
        # (or a path to /dev/zero) can't OOM the runner.
        try:
            size = p.stat().st_size
        except OSError as e:
            raise ToolError(f"cannot stat {path_str}: {e}") from e
        if size > MAX_IMAGE_BYTES:
            raise ToolError(
                f"{path_str} is {size} bytes; the vision sidecar caps images at "
                f"{MAX_IMAGE_BYTES} bytes. Use a smaller image."
            )

        if ctx.registry is None:
            raise ToolError(
                "vision model not configured — add a vision alias to models.json"
            )
        try:
            vision_spec = ctx.registry.resolve("vision")
        except RegistryError:
            raise ToolError(
                "vision model not configured — add a vision alias to models.json"
            ) from None
        if ctx.client is None:
            raise ToolError("no LLM client available to call the vision model")

        try:
            raw = p.read_bytes()
        except OSError as e:
            raise ToolError(f"cannot read {path_str}: {e}") from e

        data_uri = f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
        messages = [
            Message(
                role="user",
                content=[
                    ContentPart.text_part(question),
                    ContentPart.image(data_uri),
                ],
            )
        ]

        # No tools passed to the vision model — it is a single-turn describer,
        # not an agent. Same pattern as suggest_name_with_llm / the compactor.
        try:
            resp = ctx.client.complete(vision_spec, messages, tools=None)
        except Exception as e:  # WireError, http errors, etc.
            msg = str(e)
            if "image" in msg.lower() or "400" in msg:
                raise ToolError(
                    "the configured vision model does not support inline image "
                    "data — check that the 'vision' alias points to a "
                    "vision-capable model"
                ) from e
            raise ToolError(f"vision model call failed: {e}") from e

        description = resp.message.content or ""
        return ToolResult(
            output=description,
            details={
                "source": path_str,
                "mime": mime,
                "question": question,
                "vision_model": vision_spec.spec,
            },
        )


class EditTool(Tool):
    name = "edit"
    requires_permission = True
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
    requires_permission = True
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
