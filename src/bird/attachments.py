"""Copy images the user names in a message, before anything can race them.

macOS hands a dragged screenshot to the terminal as a path under
``/var/folders/.../TemporaryItems/NSIRD_screencaptureui_*/`` and reaps the file
when the screenshot preview thumbnail dismisses. That window closes while a
permission card is still on screen waiting to be approved, so the model calls
`read_image` with a perfectly correct path and gets "file not found" — which
reads to the user as a path bug in bird rather than as macOS cleaning up.

The fix is to copy at the seam where the *user* hands bird the path, not at the
seam where the *model* gets around to using it. Every user message is scanned
for paths to image files that exist right now; those are copied under the
session's ``attachments/`` dir, and the text the model sees points at the copy.
The model then reads an ordinary in-repo file: no out-of-repo gate, and nothing
left to race.

Naming a path in your own message is the authorization here. That is a
stronger and more legible signal than approving a card the model generated —
the user picked the file, and bird is only making sure it still exists by the
time anyone looks at it.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from .tools.base import resolve_under
from .tools.files import MAX_IMAGE_BYTES, detect_image_mime

ATTACHMENTS_DIRNAME = "attachments"

_EXT = r"png|jpe?g|gif|webp|bmp|tiff?"

# Three spellings of "a path to an image", in the order a terminal produces
# them: single-quoted (what macOS drag-and-drop writes when the name has
# spaces), double-quoted, and bare — where a bare token may still carry
# backslash-escaped spaces, the other thing a shell does to a dropped file.
_CANDIDATE = re.compile(
    rf"""
      '(?P<sq>[^'\n]*\.(?:{_EXT}))'
    | "(?P<dq>[^"\n]*\.(?:{_EXT}))"
    | (?P<bare>(?:\\.|[^\s'"])+\.(?:{_EXT}))
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Filenames become slugs: "Screenshot 2026-07-28 at 10.59.33 PM.png" is a pain
# for a model to pass back through a tool call intact (it is exactly the string
# that needed quoting in the first place).
_SLUG_STRIP = re.compile(r"[^a-z0-9]+")
MAX_SLUG_CHARS = 48


@dataclass
class Attachment:
    original: str  # the path as the user wrote it, quotes and all
    path: str  # repo-relative path of the copy, what the model is given
    size: int


def _slugify(stem: str) -> str:
    slug = _SLUG_STRIP.sub("-", stem.lower()).strip("-")
    return (slug[:MAX_SLUG_CHARS].rstrip("-") or "image")


def _unique_dest(dest_dir: Path, name: str, suffix: str) -> Path:
    """A free filename in dest_dir. Two screenshots taken a second apart slug
    to the same thing, and silently overwriting the first would lose it."""
    candidate = dest_dir / f"{name}{suffix}"
    n = 2
    while candidate.exists():
        candidate = dest_dir / f"{name}-{n}{suffix}"
        n += 1
    return candidate


def ingest_images(
    text: str, run_dir: Path | None, repo_root: Path
) -> tuple[str, list[Attachment]]:
    """Copy every out-of-repo image named in `text` into the session, and
    return the rewritten text plus what was copied.

    Only paths that (a) exist right now, (b) are raster images, (c) live
    outside the repo, and (d) fit the vision sidecar's size cap are touched.
    In-repo images are already stable and stay as written; anything else in the
    message — prose, code, a path to a file that isn't there — is returned
    untouched. A message with no attachments comes back byte-identical.
    """
    if run_dir is None or not text:
        return text, []

    root = repo_root.resolve()
    dest_dir = run_dir / ATTACHMENTS_DIRNAME
    out: list[str] = []
    found: list[Attachment] = []
    seen: dict[Path, str] = {}  # resolved source -> repo-relative copy
    last = 0

    for m in _CANDIDATE.finditer(text):
        raw = m.group("sq") or m.group("dq") or m.group("bare")
        src = resolve_under(root, raw)

        if src in seen:  # same file named twice in one message: copy once
            out.append(text[last : m.start()])
            out.append(seen[src])
            last = m.end()
            continue

        try:
            if not src.is_file():
                continue
            if root == src or root in src.parents:
                continue  # in-repo: already stable, leave the path alone
            _, is_raster = detect_image_mime(src)
            if not is_raster:
                continue
            size = src.stat().st_size
            if size > MAX_IMAGE_BYTES:
                continue  # read_image will refuse it anyway, with a better error
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = _unique_dest(dest_dir, _slugify(src.stem), src.suffix.lower() or ".png")
            shutil.copyfile(src, dest)
        except OSError:
            continue  # unreadable, vanished mid-scan, disk full — leave it be

        rel = dest.relative_to(root).as_posix()
        seen[src] = rel
        found.append(Attachment(original=m.group(0), path=rel, size=size))
        out.append(text[last : m.start()])
        out.append(rel)
        last = m.end()

    if not found:
        return text, []
    out.append(text[last:])
    return "".join(out), found
