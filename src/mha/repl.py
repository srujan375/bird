"""Interactive terminal mode with pi-style slash commands.

`mha` with no arguments drops into a REPL. Plain input goes to the Code
harness as a conversational turn; `/commands` control the session. Because
messages are provider-neutral (decision #5), /model can swap providers
mid-session without losing history — pi's cross-provider handoff.
"""

from __future__ import annotations

import json
import secrets
import sys
from pathlib import Path

from .activity import attach_printer
from .context.kg import KG, KGError
from .harness.compactor import compact, estimate_tokens
from .harness.runner import Runner, repair_interrupted
from .harness.session import (
    MESSAGES_FILE,
    SessionRecorder,
    find_most_recent_session,
    load_messages,
    read_session_meta,
    save_messages,
    suggest_name_with_llm,
)
from .llm.discovery import discover_models
from .llm.ollama import Ollama, OllamaError
from .llm.registry import Registry, RegistryError
from .llm.types import Message

HELP = """\
Type a task in plain language, or a command:
  /help                 this help
  /model [spec|filter]  pick from available models (Ollama local + OpenRouter
                        catalog), or switch directly by alias/provider:model;
                        the pick becomes the default; history survives the
                        swap, even across providers
  /kg status            graph location, readiness, staleness
  /kg build|update      (re)build or incrementally update the graph
  /kg query <question>  query the graph directly
  /tools                list the harness tools
  /compact              compact the conversation now
  /clear                start a fresh conversation (same session log)
  /session              show session id and paths
  /sessions             list all past sessions with auto-generated names
  /continue <id>        resume a previous session by its run-id or name
  /rename <name>        give this session a human label (visible in /sessions)
  /quit                 exit (also: /exit, Ctrl-D)"""


class Repl:
    def __init__(
        self,
        runner: Runner,
        registry: Registry,
        kg: KG | None,
        recorder: SessionRecorder,
        run_id: str,
    ):
        self.runner = runner
        self.registry = registry
        self.kg = kg
        self.recorder = recorder
        self.run_id = run_id
        self.messages: list[Message] = []
        self._streamed = False  # assistant text already printed live this turn

    def _print_delta(self, chunk: str | None) -> None:
        if chunk is None:
            print(flush=True)  # message complete → end the line
        elif chunk:  # "" is a wire-level cancel heartbeat, not display text
            self._streamed = True
            print(chunk, end="", flush=True)

    def run(self) -> int:
        self.runner.on_delta = self._print_delta
        attach_printer(self.runner.ctx)  # `› tool …` headers while the agent works
        # `mha` with no args feels like a continuation of whatever the user was
        # last doing — same chat, same model. An accepted resume announces
        # itself, so only a fresh session needs the banner.
        if not self._auto_resume_prompt():
            print(self._welcome_banner())
        if self.kg is not None and not self.kg.is_ready():
            print("kg: building in background")
        while True:
            try:
                line = input("mha> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if not line:
                continue
            if line.startswith("/"):
                if self._command(line) is False:
                    return 0
                continue
            self._turn(line)

    def _welcome_banner(self) -> str:
        """The first thing the user sees. A session's auto-derived name (from
        its first user message) makes a bare `mha` feel like a continuation
        of something real, not a fresh anonymous instance."""
        name = read_session_meta(self.recorder.run_dir).get("name") or ""
        spec = self.runner.spec.spec
        if name:
            return f"mha interactive | {name} | model={spec} | /help for commands"
        return f"mha interactive | model={spec} | /help for commands"

    def _turn(self, line: str) -> None:
        self._streamed = False
        try:
            result = self.runner.chat(self.messages, line)
        except KeyboardInterrupt:
            repair_interrupted(self.messages)
            print("\n[interrupted]")
            return
        # persist the conversation after each turn so /continue can resume it
        save_messages(
            [m.to_dict() for m in self.messages],
            self.recorder.run_dir,
        )
        if result.status == "reply" and self._streamed:
            return  # the reply already streamed to the terminal
        prefix = {"reply": "", "done": "✓ ", "max_turns": "⚠ ", }.get(result.status, "⚠ ")
        print(f"{prefix}{result.summary}")
        if result.status.startswith("aborted"):
            print("(conversation kept; rephrase or /clear to reset)")

    def _command(self, line: str) -> bool | None:
        parts = line.split(maxsplit=1)
        cmd, arg = parts[0].lower(), (parts[1].strip() if len(parts) > 1 else "")
        if cmd in ("/quit", "/exit"):
            return False
        if cmd == "/help":
            print(HELP)
        elif cmd == "/model":
            self._cmd_model(arg)
        elif cmd == "/kg":
            self._cmd_kg(arg)
        elif cmd == "/tools":
            for t in self.runner.tools.values():
                print(f"  {t.name:10s} {t.description.split('.')[0]}.")
        elif cmd == "/compact":
            before = estimate_tokens(self.messages)
            self.messages[:] = compact(
                self.messages, self.runner.spec.context_window,
                self.registry, self.runner.client, record=self.runner.ctx.emit,
            )
            print(f"compacted: ~{before} → ~{estimate_tokens(self.messages)} tokens")
        elif cmd == "/clear":
            self.messages.clear()
            self.recorder.event("clear", {})
            print("conversation cleared")
        elif cmd == "/session":
            print(f"session {self.run_id}")
            print(f"  events: {self.recorder.run_dir / 'events.jsonl'}")
            if self.kg is not None:
                print(f"  kg:     {self.kg.out_dir}")
            print(f"  ~{estimate_tokens(self.messages)} tokens in context")
        elif cmd == "/sessions":
            self._cmd_sessions("")
        elif cmd.startswith("/continue"):
            arg = line[len("/continue"):].strip()
            if not arg:
                # show the list first, then prompt for a pick
                sessions = self._list_sessions()
                if not sessions:
                    print("no past sessions found")
                    return None
                print(f"past sessions ({len(sessions)}):")
                for i, s in enumerate(sessions, 1):
                    marker = "*" if s["id"] == self.run_id else " "
                    print(f" {marker}{i:3d}. [{s['id']}] {s['name']}")
                try:
                    choice = input("continue # (empty to cancel): ").strip()
                except (EOFError, KeyboardInterrupt):
                    return None
                if not choice or not choice.isdigit() or not 1 <= int(choice) <= len(sessions):
                    print(f"not a listed number: {choice!r}")
                    return None
                self._resume_session(sessions[int(choice) - 1]["id"])
            else:
                self._resume_session(arg)
        elif cmd.startswith("/rename"):
            new_name = line[len("/rename"):].strip()
            if not new_name:
                print("usage: /rename <name>")
            else:
                self.recorder.set_name(new_name)
                print(f"renamed to: {new_name}")
        else:
            print(f"unknown command {cmd} — /help lists commands")
        return None

    def _cmd_model(self, arg: str) -> None:
        if not arg:
            self._model_picker(filter_=None)
        elif arg in self.registry.aliases or ":" in arg:
            self._switch_model(arg)
        else:
            # not an alias and not provider:model — treat as a picker filter
            self._model_picker(filter_=arg)

    def _model_picker(self, filter_: str | None) -> None:
        """List discovered models (numbered); on a real terminal, prompt for a
        pick. The pick switches the session AND becomes the persisted default."""
        spec = self.runner.spec
        print(f"model: {spec.spec} (context {spec.context_window})")
        models, notes = discover_models(self.registry)
        for note in notes:
            print(f"note: {note}")
        if filter_:
            needle = filter_.lower()
            models = [m for m in models if needle in m.spec.lower()]
            if not models:
                print(f"no available model matches {filter_!r}")
                return
        default = self.registry.aliases.get("default")
        width = max((len(m.spec) for m in models), default=0)
        for i, m in enumerate(models, 1):
            marker = "*" if m.spec == spec.spec else " "
            ctx = f"  {m.context_window // 1024}k ctx" if m.context_window else ""
            tag = "  (default)" if m.spec == default else ""
            print(f" {marker}{i:3d}. {m.spec:<{width}}  [{m.source}]{ctx}{tag}")
        if not getattr(sys.stdin, "isatty", lambda: False)():
            print("pick with /model <spec>")
            return
        try:
            choice = input("model # (empty to cancel): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not choice:
            return
        if not choice.isdigit() or not 1 <= int(choice) <= len(models):
            print(f"not a listed number: {choice!r}")
            return
        picked = models[int(choice) - 1]
        self._switch_model(picked.spec, context_window=picked.context_window)

    def _switch_model(self, name: str, context_window: int | None = None) -> None:
        if context_window and name not in self.registry.models:
            # let discovery's context length win over the conservative default
            self.registry.models[name] = {"context_window": context_window}
        try:
            new_spec = self.registry.resolve(name)
        except RegistryError as e:
            print(f"error: {e}")
            return
        if new_spec.provider.name == "ollama":
            try:
                Ollama(new_spec.provider.native_url or "http://localhost:11434").ensure(new_spec.model)
            except OllamaError as e:
                print(f"error: {e}")
                return
        old = self.runner.spec.spec
        self.runner.spec = new_spec
        saved = self.registry.set_default(new_spec.spec, context_window)
        self.recorder.event("model_switch", {"from": old, "to": new_spec.spec})
        where = f"default saved to {self.registry.path}" if saved else "default updated for this session"
        print(f"model: {old} → {new_spec.spec} (history preserved; {where})")

    def _cmd_kg(self, arg: str) -> None:
        if self.kg is None:
            print("kg is disabled for this session (--no-kg)")
            return
        parts = arg.split(maxsplit=1)
        action = parts[0].lower() if parts else "status"
        if action == "status":
            print(f"store: {self.kg.out_dir}")
            print(f"ready: {self.kg.is_ready()}")
            if self.kg.graph_path.exists():
                print(f"stale: {self.kg.is_stale()}")
        elif action in ("build", "update"):
            try:
                stats = self.kg.build() if action == "build" else self.kg.update()
                print(f"kg {stats.action}: {stats.nodes} nodes, {stats.edges} edges")
            except KGError as e:
                print(f"error: {e}")
        elif action == "query":
            if len(parts) < 2:
                print("usage: /kg query <question>")
                return
            try:
                print(self.kg.query(parts[1]).text)
            except KGError as e:
                print(f"error: {e}")
        else:
            print("usage: /kg [status|build|update|query <question>]")

    def _sessions_dir(self) -> Path:
        """Location of all past session directories: run_dir is
        .mha/sessions/<run-id>, so its parent IS the sessions dir."""
        return self.recorder.run_dir.parent

    def _list_sessions(self) -> list[dict[str, str]]:
        """Scan .mha/sessions/ and return a list of {id, name, last_event} dicts.

        The name comes from session.json when set (/rename or the auto-namer),
        else is derived from the session's first task. Sessions with an empty
        events.jsonl (opened but never used) are skipped."""
        sessions_dir = self._sessions_dir()
        if not sessions_dir.exists():
            return []
        entries = sorted(sessions_dir.iterdir(), key=lambda p: p.stat().st_mtime)
        out = []
        for entry in entries:
            events_path = entry / "events.jsonl"
            try:
                if events_path.stat().st_size == 0:
                    continue
            except OSError:
                continue
            name = read_session_meta(entry).get("name") or self._derive_session_name(entry)
            last_event = ""
            try:
                with open(events_path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        rec = json.loads(line)
                        last_event = rec.get("data", {}).get("summary", "") or \
                                    rec.get("data", {}).get("task", "") or \
                                    rec.get("type", "")
            except (OSError, json.JSONDecodeError):
                pass
            out.append({
                "id": entry.name,
                "name": name,
                "last_event": last_event[:200],
            })
        return out

    @staticmethod
    def _derive_session_name(entry: Path) -> str:
        """Build a short label from the session's first task (the run_start
        event), falling back to the first user message on disk."""
        try:
            with open(entry / "events.jsonl", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    if rec.get("type") == "run_start":
                        task = (rec.get("data", {}).get("task") or "").strip()
                        if task:
                            return task[:80]
        except (OSError, json.JSONDecodeError):
            pass
        for msg in load_messages(entry) or []:
            if msg.get("role") == "user":
                content = (msg.get("content") or "").strip()
                if content:
                    return content[:80]
        return entry.name

    def _resume_session(self, identifier: str) -> None:
        """Load a previous session's messages into the current Repl.

        The session's recorded model (from session.json) is re-applied so the
        resumed conversation keeps running on the same LLM it was started on
        — that's the whole point of `/continue`. Failures are reported but
        never raise: the user is still in a usable REPL afterwards."""
        target = self._find_session_dir(identifier)
        if target is None:
            print(f"no session matches {identifier!r}")
            return
        loaded = self._load_session_messages(target)
        if loaded is None:
            return  # _load_session_messages already printed the reason
        self.messages = loaded
        self.recorder.event("resume", {"from": target.name, "messages": len(loaded)})
        print(f"resumed session: {target.name}")
        print(f"  loaded {len(loaded)} messages")
        self._apply_session_model(target)

    def _find_session_dir(self, identifier: str) -> Path | None:
        """Resolve a user-typed id to a session directory. Tries exact match,
        then prefix (so "fix-login" matches "2024-01-01-fix-login-…"). Returns
        None when no candidate fits."""
        sessions_dir = self._sessions_dir()
        if not sessions_dir.exists():
            print(f"no past sessions found at {sessions_dir}")
            return None
        for entry in sorted(sessions_dir.iterdir(), key=lambda p: p.stat().st_mtime):
            if entry.name == identifier or entry.name.startswith(identifier + "-"):
                return entry
        return None

    def _load_session_messages(self, run_dir: Path) -> list[Message] | None:
        """Read the persisted transcript from a session directory and convert
        to Message objects. None on any failure (with a printed reason)."""
        rows = load_messages(run_dir)
        if rows is None:
            print(f"session {run_dir.name} has no readable {MESSAGES_FILE}")
            return None
        return [Message.from_dict(r) for r in rows]

    def _apply_session_model(self, run_dir: Path) -> None:
        """Switch the runner to the model the resumed session was using.

        Quiet when there's nothing to do (the session predates the metadata
        file, or its model matches the current spec). Quiet failures too:
        `/continue` is about continuing the chat, not lecturing about model
        setup — a `note:` line keeps the user informed without blocking."""
        meta = read_session_meta(run_dir)
        recorded = meta.get("model")
        if not recorded or recorded == self.runner.spec.spec:
            return
        try:
            new_spec = self.registry.resolve(recorded)
        except RegistryError:
            print(f"note: previous model {recorded!r} is not in this registry; staying on {self.runner.spec.spec}")
            return
        if new_spec.provider.name == "ollama":
            try:
                Ollama(new_spec.provider.native_url or "http://localhost:11434").ensure(new_spec.model)
            except OllamaError as e:
                print(f"note: previous model unavailable ({e}); staying on {self.runner.spec.spec}")
                return
        old = self.runner.spec.spec
        self.runner.spec = new_spec
        self.recorder.event("model_switch", {"from": old, "to": new_spec.spec, "via": "resume"})
        print(f"model: {old} → {new_spec.spec} (from resumed session)")

    def _auto_resume_prompt(self) -> bool:
        """Offer to resume the most recent session on startup.

        Returns True when the user accepted and we should drop straight back
        into the REPL (caller's `run()` should NOT print its banner — the
        resume already announced the model and a summary). False when no
        session was found, the user declined, or we're not on a tty (CI
        shouldn't get a hung prompt).

        Skips when the most recent session is younger than ~5 minutes AND
        was last modified within the same process invocation — that is, when
        it would be the *current* session. We never re-resume ourselves.
        """
        if not getattr(sys.stdin, "isatty", lambda: False)():
            return False
        recent = find_most_recent_session(self._sessions_dir())
        if recent is None or recent.resolve() == self.recorder.run_dir.resolve():
            return False
        meta = read_session_meta(recent)
        label = meta.get("name") or self._derive_session_name(recent)
        label = label or recent.name
        recorded_model = meta.get("model") or "?"
        # count messages to show "n turns" in the prompt
        msgs = self._load_session_messages(recent) or []
        n_turns = sum(1 for m in msgs if m.role == "user")
        print(f"continue previous session?  [{recent.name}]")
        print(f"  name:  {label}")
        print(f"  model: {recorded_model}")
        print(f"  turns: {n_turns}")
        try:
            ans = input("resume? [Y/n/fresh]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        if ans in ("", "y", "yes"):
            loaded = self._load_session_messages(recent)
            if loaded is None:
                return False
            self.messages = loaded
            self.recorder.event("auto_resume", {"from": recent.name, "messages": len(loaded)})
            print(f"resumed {recent.name} ({len(loaded)} messages)")
            self._apply_session_model(recent)
            return True
        if ans in ("f", "fresh"):
            print("starting a fresh session")
            return False
        print("starting a fresh session")
        return False

    def _cmd_sessions(self, arg: str) -> None:
        """List all past sessions with their auto-generated names."""
        sessions = self._list_sessions()
        if not sessions:
            print("no past sessions found")
            return
        print(f"past sessions ({len(sessions)}):")
        for i, s in enumerate(sessions, 1):
            marker = "*" if s["id"] == self.run_id else " "
            print(f" {marker}{i:3d}. [{s['id']}] {s['name']}")
