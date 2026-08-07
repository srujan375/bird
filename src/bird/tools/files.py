"""read / edit / write — the file tools."""

from __future__ import annotations

import base64
import fnmatch
import mimetypes
import os
import re
from pathlib import Path
from typing import Any

from ..llm.registry import RegistryError
from ..llm.types import ContentPart, Message
from .base import (
    Tool,
    ToolContext,
    ToolError,
    ToolResult,
    gate_outside_repo_read,
    normalize_path_arg,
)

MAX_READ_CHARS = 24_000

# Output cap for `ls`: a huge directory must not blow up the transcript. Flat
# listing of one directory only — the agent pages with `offset` or narrows
# with `pattern` when a directory exceeds this. Analogous to MAX_READ_CHARS.
MAX_LS_ENTRIES = 500

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


def detect_image_mime(path: Path) -> tuple[str | None, bool]:
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


def _not_found(path_str: str, resolved: Path) -> ToolError:
    """A 'file not found' that says where the tool actually looked.

    `path_str` is what the model passed; `resolved` is where that landed after
    normalization. When the two disagree the difference is the whole diagnosis,
    so show it. macOS screenshot temp dirs get a note of their own: those files
    are reaped when the screenshot preview dismisses, so the path can be
    perfectly correct and the file still gone by the time approval comes back.
    """
    detail = f"file not found: {path_str}"
    if str(resolved) != path_str:
        detail += f" (looked in {resolved})"
    parent = resolved.parent
    if parent.is_dir():
        if "screencaptureui" in str(resolved) or "/TemporaryItems/" in str(resolved):
            detail += (
                " — the directory exists but the file is gone. macOS deletes "
                "screenshot temp files when the preview thumbnail dismisses; "
                "ask the user to save the image somewhere durable and re-send "
                "the path."
            )
    else:
        detail += f" — the directory {parent} does not exist either"
    return ToolError(detail)


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
        gate_outside_repo_read(ctx, args["path"], p, self.name)
        if p.is_dir():
            raise ToolError(
                f"{args['path']} is a directory, not a file — use the ls tool to "
                f"list its contents."
            )
        if not p.is_file():
            raise _not_found(args["path"], p)

        # Image detection: a raster image file is garbled if read as text, so
        # nudge the model to read_image instead. SVG is excluded (it is XML
        # text) and falls through to normal reading below.
        mime, is_raster = detect_image_mime(p)
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


class LsTool(Tool):
    """List the entries of a single directory.

    Lets the agent discover what files are inside a folder instead of guessing
    filenames. Flat listing of one directory only — no recursion (the agent
    calls ls on subdirectories iteratively), so a listing either shows all
    entries or truncates with a visible marker, never a partial tree that looks
    complete. Entries are repo-relative paths the agent can pass straight to
    read or ls without hand-joining parent + name.
    """

    name = "ls"
    description = (
        "List the entries of a single directory so you can discover what "
        "files exist instead of guessing filenames. Returns one line per "
        "entry as repo-relative paths with type and size. Flat listing only — "
        "call ls on a subdirectory to see inside it."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Repo-relative directory path",
            },
            "pattern": {
                "type": "string",
                "description": (
                    "Optional fnmatch glob applied to FILE names only (e.g. "
                    "'*.py', 'test_*'); directories are always shown regardless "
                    "of pattern so navigation structure is never lost"
                ),
            },
            "offset": {
                "type": "integer",
                "description": "1-based entry index to start from (for paging)",
            },
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path_str = args["path"]
        pattern = args.get("pattern")
        offset = max(args.get("offset", 1), 1)
        p = ctx.resolve_path(path_str)
        gate_outside_repo_read(ctx, path_str, p, self.name)

        if p.is_file():
            raise ToolError(
                f"{path_str} is a file, not a directory — use the read tool to "
                f"see its contents."
            )
        if not p.is_dir():
            # Reuse the _not_found-style diagnosis: when normalization moved the
            # path, the resolved location is the whole diagnosis.
            raise _not_found(path_str, p)

        try:
            entries = list(os.scandir(p))
        except PermissionError:
            raise ToolError(f"permission denied: cannot read directory {path_str}")
        except OSError as e:
            raise ToolError(f"cannot list {path_str}: {e}") from e

        rows = _format_listing(p, entries, ctx.repo_root, pattern)
        total = len(rows)
        window = rows[offset - 1 : offset - 1 + MAX_LS_ENTRIES]
        out_lines = [r for r in window]
        note = ""
        if offset - 1 + MAX_LS_ENTRIES < total:
            omitted = total - (offset - 1 + MAX_LS_ENTRIES)
            next_offset = offset + MAX_LS_ENTRIES
            note = (
                f"\n... [truncated; {omitted} more entries — use offset="
                f"{next_offset} to see more, or pattern to narrow]"
            )
        elif offset > 1:
            shown = len(window)
            note = f"\n[showing {shown} of {total} entries]"
        return ToolResult(
            output="\n".join(out_lines) + note,
            details={"path": path_str, "entries": total, "pattern": pattern},
        )


def _classify(entry: os.DirEntry) -> str:
    """Four-way type classification for a scandir entry.

    is_dir()/is_file() follow symlinks, so a symlink-to-dir shows as 'dir' and
    is navigable. A symlink that points at nothing (or a non-dir/non-file
    target) falls through to 'symlink'; fifos/sockets/devices are 'other'.
    """
    try:
        if entry.is_dir():
            return "dir"
        if entry.is_file():
            return "file"
    except OSError:
        # stat on the target failed (broken symlink, or permission) — fall
        # through to the non-following classification below.
        pass
    if entry.is_symlink():
        return "symlink"
    return "other"


def _entry_size(entry: os.DirEntry, kind: str) -> str:
    """Size in bytes, or '?' when stat fails (e.g. a broken symlink)."""
    try:
        return str(entry.stat(follow_symlinks=True).st_size)
    except OSError:
        return "?"


def _format_listing(
    dir_path: Path,
    entries: list[os.DirEntry],
    repo_root: Path,
    pattern: str | None,
) -> list[str]:
    """Build the sorted, formatted listing rows (repo-relative paths).

    Pattern (fnmatch) applies to FILE names only; directories always pass so
    the agent never loses navigation structure. Sorted dirs-first then
    alphabetical. Each row: '{rel_path}  (type)  {size}', directories suffixed
    with '/'.
    """
    rows: list[tuple[int, str, str, str, str]] = []  # (dirfirst, name_lower, rel, type, size)
    for entry in entries:
        kind = _classify(entry)
        is_dir = kind == "dir"
        # Pattern filters files only; directories always pass.
        if pattern and not is_dir and not fnmatch.fnmatch(entry.name, pattern):
            continue
        size = _entry_size(entry, kind)
        rel = _rel_path(repo_root, dir_path, entry.name, is_dir)
        # dirs-first (0 before 1), then alphabetical by name
        rows.append((0 if is_dir else 1, entry.name.lower(), rel, kind, size))

    rows.sort(key=lambda r: (r[0], r[1]))
    return [f"{r[2]}  ({r[3]})  {r[4]}" for r in rows]


def _rel_path(repo_root: Path, dir_path: Path, name: str, is_dir: bool) -> str:
    """Repo-relative path for an entry, directories suffixed with '/'."""
    try:
        rel = (dir_path / name).relative_to(repo_root)
    except ValueError:
        # Outside the repo (e.g. a symlink resolved outside): show the name as
        # given under the requested dir rather than an absolute path.
        rel = Path(name)
    s = str(rel).replace(os.sep, "/")
    if is_dir and not s.endswith("/"):
        s += "/"
    return s


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
        gate_outside_repo_read(ctx, path_str, p, self.name)
        if not p.is_file():
            raise _not_found(path_str, p)

        # SVG is a vector format — vision models can't process it and it is
        # XML text, so the model should use `read` instead.
        mime, is_raster = detect_image_mime(p)
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
                    f"the 'vision' alias points at {vision_spec.spec}, which "
                    f"rejected inline image data — it is probably not a "
                    f"vision-capable model. Tell the user to repoint the "
                    f"'vision' alias in models.json at a multimodal model."
                ) from e
            raise ToolError(
                f"vision model call failed ({vision_spec.spec}): {e}"
            ) from e

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
        p = ctx.resolve_repo_path(args["path"])
        if not p.is_file():
            raise _not_found(args["path"], p)
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
        p = ctx.resolve_repo_path(args["path"])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(args["content"], encoding="utf-8")
        return ToolResult(
            output=f"Wrote {args['path']} ({len(args['content'])} chars).",
            details={"path": args["path"], "bytes": len(args["content"].encode())},
        )


# ---------------------------------------------------------------- search

# Directories that are enormous and machine-generated. Walking into one from a
# repo-wide search buries the answer, so they are skipped by DEFAULT — but only
# by default. This list deliberately mirrors the knowledge graph's exclusions
# (context.kg.ARTIFACT_DIRS): those are the paths the KG cannot answer about,
# which makes them exactly the paths grep must be able to reach. A path that
# names one explicitly ("node_modules/@scope/pkg") is searched.
_HEAVY_DIRS = frozenset({
    "node_modules", "dist", "build", "out", "target", "coverage", "htmlcov",
    ".venv", "venv", "__pycache__", "site-packages", ".mypy_cache", ".pytest_cache",
    ".bird", "graphify-out", ".next", ".nuxt", ".svelte-kit", ".turbo", ".parcel-cache",
    ".git",
})

MAX_GREP_MATCHES = 200
MAX_GLOB_RESULTS = 300
# Per-line clip: one minified bundle line is the whole file, and printing it
# costs the transcript more than the match is worth.
MAX_MATCH_LINE_CHARS = 400
GREP_SNIFF_BYTES = 1024


def _is_probably_binary(p: Path) -> bool:
    try:
        with p.open("rb") as f:
            return b"\x00" in f.read(GREP_SNIFF_BYTES)
    except OSError:
        return True


def _walk_files(root: Path, include_heavy: bool):
    """Yield files under `root`, pruning heavy dirs unless asked otherwise.

    Pruning happens at directory level (os.walk's dirnames splice) rather than
    by filtering results, so a skipped node_modules is never descended into —
    the difference between a fast search and a 30-second one.
    """
    if root.is_file():
        yield root
        return
    for dirpath, dirnames, filenames in os.walk(root):
        if not include_heavy:
            dirnames[:] = [d for d in dirnames if d not in _HEAVY_DIRS]
        else:
            dirnames[:] = [d for d in dirnames if d != ".git"]
        dirnames.sort()
        here = Path(dirpath)
        for name in sorted(filenames):
            yield here / name


def _names_a_heavy_dir(path_str: str) -> bool:
    """True when the requested path itself points into a skipped directory —
    the signal that the caller means it and pruning should be off."""
    return any(part in _HEAVY_DIRS for part in Path(normalize_path_arg(path_str)).parts)


def _rel(p: Path, root: Path) -> str:
    try:
        return str(p.relative_to(root))
    except ValueError:
        return str(p)


class GrepTool(Tool):
    """Regex search over file contents.

    Exists because the harness had no way to answer "which files contain this
    string" except by shelling out. Every such question became a bash call,
    a permission prompt, and a turn — and for content the knowledge graph does
    not index (dependency sources, build output, literal strings that are not
    code symbols) it was the *only* route, taken one blind `grep | head` at a
    time.
    """

    name = "grep"
    description = (
        "Regex search over file contents. Use for literal text and for what kg_query "
        "does not index: node_modules, dist, build. Returns path:line: text."
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Python regex"},
            "path": {"type": "string", "description": "File or dir to search (default: repo root)"},
            "glob": {"type": "string", "description": "Only files whose name matches, e.g. '*.js'"},
            "literal": {"type": "boolean", "description": "Match pattern as plain text"},
            "ignore_case": {"type": "boolean", "description": "Case-insensitive"},
            "files_only": {"type": "boolean", "description": "Return paths, not lines"},
            "context": {"type": "integer", "description": "Context lines per match"},
        },
        "required": ["pattern"],
        "additionalProperties": False,
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        pattern = args["pattern"]
        path_str = args.get("path") or "."
        root = ctx.resolve_path(path_str)
        gate_outside_repo_read(ctx, path_str, root, self.name)
        if not root.exists():
            raise _not_found(path_str, root)

        flags = re.IGNORECASE if args.get("ignore_case") else 0
        try:
            rx = re.compile(re.escape(pattern) if args.get("literal") else pattern, flags)
        except re.error as e:
            raise ToolError(
                f"invalid regular expression {pattern!r}: {e}. Pass literal=true to "
                f"search for it as plain text."
            ) from e

        glob = args.get("glob")
        context = max(int(args.get("context") or 0), 0)
        files_only = bool(args.get("files_only"))
        include_heavy = _names_a_heavy_dir(path_str)

        lines: list[str] = []
        matched_files = 0
        total = 0
        truncated = False
        scanned = 0

        for f in _walk_files(root, include_heavy):
            if glob and not fnmatch.fnmatch(f.name, glob):
                continue
            if _is_probably_binary(f):
                continue
            scanned += 1
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            file_lines = text.splitlines()
            hits = [i for i, line in enumerate(file_lines) if rx.search(line)]
            if not hits:
                continue
            matched_files += 1
            rel = _rel(f, ctx.repo_root)
            if files_only:
                lines.append(rel)
                total += len(hits)
                if matched_files >= MAX_GLOB_RESULTS:
                    truncated = True
                    break
                continue
            for i in hits:
                total += 1
                if total > MAX_GREP_MATCHES:
                    truncated = True
                    break
                lo = max(i - context, 0)
                hi = min(i + context + 1, len(file_lines))
                for j in range(lo, hi):
                    body = file_lines[j]
                    if len(body) > MAX_MATCH_LINE_CHARS:
                        body = body[:MAX_MATCH_LINE_CHARS] + " …[line clipped]"
                    sep = ":" if j == i else "-"
                    lines.append(f"{rel}:{j + 1}{sep} {body}")
                if context:
                    lines.append("--")
            if truncated:
                break

        if not lines:
            hint = ""
            if not include_heavy:
                hint = (
                    " Generated directories (node_modules, dist, build) were skipped — "
                    "pass that path explicitly to search inside one."
                )
            return ToolResult(
                output=f"No matches for {pattern!r} in {path_str} ({scanned} files searched).{hint}",
                details={"pattern": pattern, "matches": 0, "files": 0, "scanned": scanned},
            )

        header = (
            f"[{total} match{'es' if total != 1 else ''} in {matched_files} file"
            f"{'s' if matched_files != 1 else ''}"
            + (", truncated" if truncated else "")
            + "]"
        )
        out = header + "\n" + "\n".join(lines)
        if truncated:
            out += "\n[result cap reached — narrow with path, glob, or a tighter pattern]"
        return ToolResult(
            output=out,
            details={
                "pattern": pattern,
                "matches": total,
                "files": matched_files,
                "scanned": scanned,
                "truncated": truncated,
            },
        )


class GlobTool(Tool):
    """Find files by path pattern.

    "Which files have 'mcp' in the name" is not a question about code
    structure, so the knowledge graph answers it with sixty unrelated nodes.
    It is a question about the filesystem, and this is the tool that reads
    the filesystem.
    """

    name = "glob"
    description = (
        "Find files by path pattern, e.g. '**/*mcp*'. Use for 'which files are named X' "
        "— kg_query does not index filenames."
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Glob on the relative path, e.g. '**/*mcp*'"},
            "path": {"type": "string", "description": "Directory to search under (default: repo root)"},
        },
        "required": ["pattern"],
        "additionalProperties": False,
    }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        pattern = args["pattern"]
        path_str = args.get("path") or "."
        root = ctx.resolve_path(path_str)
        gate_outside_repo_read(ctx, path_str, root, self.name)
        if not root.is_dir():
            raise ToolError(f"{path_str} is not a directory")

        include_heavy = _names_a_heavy_dir(path_str) or _names_a_heavy_dir(pattern)
        # A bare name ('*mcp*') is meant to match anywhere, not only at the
        # root — models write that far more often than the '**/' spelling.
        candidates = [pattern]
        if not pattern.startswith("**"):
            candidates.append(f"**/{pattern.lstrip('/')}")

        found: list[str] = []
        seen: set[str] = set()
        for f in _walk_files(root, include_heavy):
            rel_root = _rel(f, root)
            if not any(fnmatch.fnmatch(rel_root, c) or fnmatch.fnmatch(f.name, c)
                       for c in candidates):
                continue
            rel = _rel(f, ctx.repo_root)
            if rel in seen:
                continue
            seen.add(rel)
            found.append(rel)
            if len(found) >= MAX_GLOB_RESULTS:
                break

        if not found:
            hint = ""
            if not include_heavy:
                hint = " Generated directories were skipped — name one explicitly to search it."
            return ToolResult(
                output=f"No files match {pattern!r} under {path_str}.{hint}",
                details={"pattern": pattern, "count": 0},
            )
        capped = len(found) >= MAX_GLOB_RESULTS
        out = f"[{len(found)} file{'s' if len(found) != 1 else ''}"
        out += ", capped]" if capped else "]"
        out += "\n" + "\n".join(found)
        return ToolResult(
            output=out, details={"pattern": pattern, "count": len(found), "truncated": capped}
        )
