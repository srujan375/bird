"""Session recording: append-only events.jsonl under .bird/sessions/<run-id>/."""

from __future__ import annotations

import json
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{secrets.token_hex(3)}"


# one message per line, provider-neutral Message.to_dict() schema; rewritten
# wholesale after each turn (compaction rewrites history, so append-only
# would drift from the live transcript)
MESSAGES_FILE = "messages.jsonl"


class SessionRecorder:
    def __init__(self, run_dir: Path, model: str | None = None, name: str | None = None):
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "artifacts").mkdir(exist_ok=True)
        self._events_path = self.run_dir / "events.jsonl"
        self._meta_path = self.run_dir / "session.json"
        # write metadata once on creation; never overwrite (preserves any
        # /rename edit a future invocation makes)
        if model is not None or name is not None:
            meta = self._read_meta()
            if model is not None and not meta.get("model"):
                meta["model"] = model
            if name is not None and not meta.get("name"):
                meta["name"] = name
            meta.setdefault("created_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
            self._write_meta(meta)
        self._fh = self._events_path.open("a", encoding="utf-8")
        self._seq = 0

    def _read_meta(self) -> dict[str, Any]:
        if not self._meta_path.is_file():
            return {}
        try:
            return json.loads(self._meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _write_meta(self, meta: dict[str, Any]) -> None:
        self._meta_path.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    def set_name(self, name: str) -> None:
        """User-renamed this session; persisted in session.json so /sessions
        shows the human label instead of the auto-derived first-message one."""
        meta = self._read_meta()
        meta["name"] = name
        meta["renamed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._write_meta(meta)

    def event(self, event_type: str, data: dict[str, Any]) -> None:
        self._seq += 1
        record = {
            "seq": self._seq,
            "ts": time.time(),
            "type": event_type,
            "data": data,
        }
        self._fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()

    def __enter__(self) -> "SessionRecorder":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def save_messages(messages: list[dict[str, Any]], run_dir: Path) -> None:
    """Persist the conversation transcript to disk so it can be resumed later."""
    path = run_dir / MESSAGES_FILE
    with open(path, "w", encoding="utf-8") as f:
        for m in messages:
            f.write(json.dumps(m, ensure_ascii=False, default=str) + "\n")


def load_messages(run_dir: Path) -> list[dict[str, Any]] | None:
    """Load a persisted conversation transcript. Returns None if not found
    or unreadable. Falls back to reconstructing from events.jsonl for
    sessions recorded by older bird versions that didn't separate the
    message log from events."""
    path = run_dir / MESSAGES_FILE
    if path.is_file():
        try:
            with open(path, encoding="utf-8") as f:
                rows = [json.loads(line) for line in f if line.strip()]
            if rows:
                return rows
        except (json.JSONDecodeError, OSError):
            pass
    return _load_messages_from_events(run_dir)


def _load_messages_from_events(run_dir: Path) -> list[dict[str, Any]] | None:
    """Reconstruct a transcript from events.jsonl for legacy sessions that
    never wrote messages.jsonl. Returns None if events.jsonl is missing,
    empty, or contains no reconstructable user/assistant turns.

    Reconstruction is best-effort: run_start.data.task becomes the first
    user message, and each assistant event becomes an assistant message.
    No tool_calls survive — older sessions didn't record them on the
    assistant message anyway, and the resume path can't replay them."""
    events_path = run_dir / "events.jsonl"
    if not events_path.is_file():
        return None
    messages: list[dict[str, Any]] = []
    saw_user = False
    try:
        with open(events_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                rtype = rec.get("type")
                data = rec.get("data") or {}
                if rtype == "run_start":
                    task = (data.get("task") or "").strip()
                    if task:
                        messages.append({"role": "user", "content": task})
                        saw_user = True
                elif rtype == "assistant":
                    content = (data.get("content") or "").strip()
                    if content:
                        messages.append({"role": "assistant", "content": content})
    except (json.JSONDecodeError, OSError):
        return None
    # require at least the user seed from run_start, otherwise the session
    # never really started and we'd be inventing a transcript
    return messages if saw_user else None


def list_sessions(sessions_dir: Path | None = None) -> list[dict[str, Any]]:
    """List all known sessions. Returns a list of dicts with id, name, date.

    A session is any directory under sessions/ that recorded at least one
    event (a non-empty events.jsonl — every launch creates the file, so an
    empty one is a session that was opened and never used). Each entry's
    name comes from session.json (set by /rename or by the auto-renamer)
    and falls back to the messages-derived label.
    """
    if not sessions_dir or not sessions_dir.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for p in sorted(sessions_dir.iterdir(), key=lambda q: q.stat().st_mtime, reverse=True):
        if not p.is_dir() or not _has_activity(p):
            continue
        out.append({
            "id": p.name,
            "name": _session_name(p) or p.name,
            "date": _session_date(p),
        })
    return out


def _has_activity(run_dir: Path) -> bool:
    """A session counts only once something happened in it: every launch
    creates events.jsonl, so existence alone means nothing — it must be
    non-empty."""
    try:
        return (run_dir / "events.jsonl").stat().st_size > 0
    except OSError:
        return False


def find_most_recent_session(sessions_dir: Path | None) -> Path | None:
    """Return the most recently modified session directory, or None.

    Used to prompt the user on startup: "continue <name>?". Sorted by mtime
    so the most recently active session wins even if its name sorts earlier
    alphabetically than older ones. Sessions with no recorded activity
    (an empty or missing events.jsonl) are skipped — they're
    freshly-created but unused.
    """
    if not sessions_dir or not sessions_dir.is_dir():
        return None
    candidates = [p for p in sessions_dir.iterdir() if p.is_dir() and _has_activity(p)]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def read_session_meta(run_dir: Path) -> dict[str, Any]:
    """Read the session.json metadata for a given session directory.

    Always returns a dict (possibly empty) — the file is optional. Keys of
    interest: 'model' (provider:model spec that served the session) and
    'name' (a human label, set by /rename or by `bird` on creation)."""
    path = run_dir / "session.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def suggest_name_with_llm(
    first_user: str,
    registry: "Registry",
    client: "OpenAICompatClient",
) -> str | None:
    """Ask the pinned `compactor` model to write a short session title.

    Uses a separate model so a live coding session isn't derailed by a
    naming request. Returns None on any failure (model unreachable, no
    'compactor' alias, refusal, empty response) — the caller falls back to
    a heuristic label derived from the first user message.
    """
    if not first_user or not first_user.strip():
        return None
    try:
        spec = registry.resolve("compactor")
    except Exception:  # RegistryError or anything else — never break /quit
        return None
    prompt = (
        "Write a short (3-7 word) title for a coding-agent session based on "
        "the user's first request. The title should be imperative, lowercase, "
        "and capture the task (e.g. 'fix login redirect bug', 'add pytest for "
        "compactor', 'refactor session list'). Reply with ONLY the title — "
        "no quotes, no punctuation at the end.\n\n"
        f"Request: {first_user[:1000]}"
    )
    try:
        resp = client.complete(
            spec,
            [
                Message(role="system", content="You name coding sessions concisely."),
                Message(role="user", content=prompt),
            ],
            temperature=0.0,
            max_tokens=40,
        )
    except Exception:  # WireError, network, etc. — name is optional
        return None
    title = (resp.message.content or "").strip().strip('"').strip("'").strip()
    # sanitise: one line, reasonable length, drop trailing punctuation
    title = title.splitlines()[0][:80].rstrip(".,;:")
    return title or None


def _session_name(run_dir: Path) -> str:
    """Derive a human-readable session name from the messages file."""
    msgs = load_messages(run_dir)
    if not msgs:
        return ""
    # Use the last assistant message's summary or first user message as name
    for m in reversed(msgs):
        if isinstance(m, dict) and m.get("role") == "assistant":
            content = str(m.get("content", "")).strip()
            if len(content) < 200:
                return content[:150] or ""
    # fallback to first user message
    for m in msgs:
        if isinstance(m, dict) and m.get("role") == "user":
            content = str(m.get("content", "")).strip()
            if len(content) < 200:
                return content[:150] or ""
    return run_dir.name


def _session_date(run_dir: Path) -> str:
    """Return the creation date of a session directory."""
    try:
        stat = run_dir.stat()
        dt = datetime.fromtimestamp(stat.st_ctime, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M")
    except OSError:
        return ""


def load_run_id_from_events(run_dir: Path) -> str | None:
    """Extract the run_id from events.jsonl (first event's data)."""
    path = run_dir / "events.jsonl"
    if not path.is_file():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                data = rec.get("data", {})
                if isinstance(data, dict):
                    return str(data.get("run_id", "")) or None
    except OSError:
        pass
    return None
