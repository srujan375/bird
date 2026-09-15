"""Interactive terminal mode with pi-style slash commands.

`bird` with no arguments drops into a REPL. Plain input goes to the Code
harness as a conversational turn; `/commands` control the session. Because
messages are provider-neutral (decision #5), /model can swap providers
mid-session without losing history — pi's cross-provider handoff.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
from pathlib import Path
from types import SimpleNamespace

try:
    import readline
except ImportError:  # Windows lacks readline; completion just won't work
    readline = None

try:
    import termios
    import tty
except ImportError:  # Windows lacks termios; the catalog needs a tty anyway
    termios = None
    tty = None

from .activity import attach_printer
from .attachments import ingest_images
from .context.kg import KG, KGError
from .engine.compactor import compact, estimate_tokens
from .engine.runner import Runner, repair_interrupted
from .engine.session import (
    MESSAGES_FILE,
    SessionRecorder,
    find_most_recent_session,
    load_messages,
    read_session_meta,
    save_messages,
    suggest_name_with_llm,
)
from .llm.discovery import discover_models, ollama_context_window
from .llm.ollama import Ollama, OllamaError
from .llm.registry import DEFAULT_CONTEXT_WINDOW, Registry, RegistryError
from .llm.types import Message

HELP = """\
Type a task in plain language, or a command:
  /help                 this help
  /model                walk harness -> model -> thinking level; the pick is
                        saved as that harness's model (Ollama local +
                        OpenRouter catalog), and switches this session when
                        it is the running harness — history survives the
                        swap, even across providers
  /model <harness>      the same walk, starting at the model step
  /model [harness] <spec> [mode] [tokens]
                        set it directly: spec is an alias or provider:model
                        (or a filter for the picker), mode a thinking level,
                        tokens the context window; harness defaults to the
                        running one
  /think [mode]         pick a thinking mode (off/low/medium/high/max) for the
                        running model, or set it directly; off disables
                        thinking, the others set reasoning effort; carries
                        across a direct /model <spec> switch
  /kg status            graph location, readiness, staleness
  /kg build|update      (re)build or incrementally update the graph
  /kg query <question>  query the graph directly
  /mcp                  browse the MCP server catalog (interactive store)
  /mcp status           text status of configured MCP servers
  /mcp search <query>   search the official MCP registry (text)
  /mcp add <name>       install a server from the registry (asks first)
  /setup                the first-run walkthrough again: probe sources,
                        enter keys, pick + verify a default model
  /doctor               health check: one line per check, a fix per failure
  /keys                 which provider keys are set, and where from
  /keys set <NAME>      store OLLAMA_API_KEY / OPENROUTER_API_KEY in
                        ~/.bird/.env (prompts for the value, never echoed)
  /tools                list the harness tools
  /compact              compact the conversation now
  /clear                start a fresh conversation (same session log)
  /reload               respawn bird with the latest code/skills, resuming
                        this session (no need to open a new terminal)
  /session              show session id and paths
  /sessions [filter]    list past sessions with names; on a tty, prompt to
                        resume the picked one (filter by substring of name
                        or id to narrow the list first)
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

    def run(self, first_input: str | None = None) -> int:
        """The prompt loop. `first_input` runs one turn before handing over to
        the user — for commands that take the opening message as an argument
        (`bird arch --repl "design me a webhook relay"`)."""
        self.runner.on_delta = self._print_delta
        attach_printer(self.runner.ctx)  # `› tool …` headers while the agent works
        self._setup_completion()
        # `bird` with no args feels like a continuation of whatever the user was
        # last doing — same chat, same model. An accepted resume announces
        # itself, so only a fresh session needs the banner.
        if not self._auto_resume_prompt():
            print(self._welcome_banner())
        if self.kg is not None and not self.kg.is_ready():
            print("kg: building in background")
        if first_input:
            self._turn(first_input)
        while True:
            try:
                line = input("bird> ").strip()
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

    # Built-in slash commands that always take priority over skill names.
    # Kept in sync with HELP and _command(); a skill named "model" would be
    # unreachable via /model, so these are reserved.
    BUILTIN_COMMANDS = (
        "/help", "/model", "/think", "/kg", "/mcp", "/tools", "/skills",
        "/setup", "/doctor", "/keys",
        "/compact", "/clear", "/reload", "/session", "/sessions", "/continue",
        "/rename", "/quit", "/exit",
    )

    def _setup_completion(self) -> None:
        """Wire readline Tab completion for /commands and /<skill-name>.

        Only active when the readline module is available (not on Windows
        unless pyreadline is installed). Completion triggers on Tab after
        typing / — e.g. `/com<Tab>` expands to /continue, or
        `/skill-<Tab>` to /skill-creator."""
        if readline is None:
            return
        skills = self.runner.ctx.skills or []

        def completer(text: str, state: int) -> str | None:
            if not text.startswith("/"):
                return None
            candidates = list(self.BUILTIN_COMMANDS)
            candidates.extend(f"/{s.name}" for s in skills)
            matches = sorted(c for c in candidates if c.startswith(text))
            return matches[state] if state < len(matches) else None

        readline.set_completer(completer)
        readline.set_completer_delims(" \t\n")
        readline.parse_and_bind("tab: complete")

    def _welcome_banner(self) -> str:
        """The first thing the user sees. A session's auto-derived name (from
        its first user message) makes a bare `bird` feel like a continuation
        of something real, not a fresh anonymous instance."""
        name = read_session_meta(self.recorder.run_dir).get("name") or ""
        spec = self.runner.spec.spec
        if name:
            return f"bird interactive | {name} | model={spec} | /help for commands"
        return f"bird interactive | model={spec} | /help for commands"

    def _ingest(self, line: str) -> str:
        """Copy any image the user just named into the session dir and point
        the text at the copy — see attachments.py for why this happens here and
        not when the model calls read_image."""
        try:
            rewritten, found = ingest_images(
                line, self.recorder.run_dir, self.runner.ctx.repo_root
            )
        except Exception:  # a convenience, never a reason to lose the turn
            return line
        for a in found:
            print(f"  📎 saved {a.path} ({a.size:,} bytes)")
        return rewritten

    def _turn(self, line: str) -> None:
        self._streamed = False
        line = self._ingest(line)
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
        elif cmd == "/think":
            self._cmd_think(arg)
        elif cmd == "/kg":
            self._cmd_kg(arg)
        elif cmd == "/mcp":
            self._cmd_mcp(arg)
        elif cmd == "/setup":
            self._cmd_setup()
        elif cmd == "/doctor":
            self._cmd_doctor()
        elif cmd == "/keys":
            self._cmd_keys(arg)
        elif cmd == "/tools":
            for t in self.runner.tools.values():
                print(f"  {t.name:10s} {t.description.split('.')[0]}.")
        elif cmd == "/skills":
            self._cmd_skills()
        elif self._is_skill_command(cmd):
            self._invoke_skill(cmd[1:], arg)
        elif cmd == "/compact":
            before = estimate_tokens(self.messages)
            self.messages[:] = compact(
                self.messages, self.runner.spec.context_window,
                self.registry, self.runner.client, record=self.runner.ctx.emit,
                keep_paths=self.runner.hot_paths(),
            )
            print(f"compacted: ~{before} → ~{estimate_tokens(self.messages)} tokens")
        elif cmd == "/clear":
            self.messages.clear()
            self.recorder.event("clear", {})
            print("conversation cleared")
        elif cmd == "/reload":
            self._cmd_reload()
        elif cmd == "/session":
            print(f"session {self.run_id}")
            print(f"  events: {self.recorder.run_dir / 'events.jsonl'}")
            if self.kg is not None:
                print(f"  kg:     {self.kg.out_dir}")
            print(f"  ~{estimate_tokens(self.messages)} tokens in context")
        elif cmd == "/sessions":
            self._cmd_sessions(arg)
        elif cmd.startswith("/continue"):
            arg = line[len("/continue"):].strip()
            if not arg:
                self._session_picker()
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

    def _is_skill_command(self, cmd: str) -> bool:
        """True when `cmd` (e.g. '/commit-style') names a loaded skill."""
        if not cmd.startswith("/"):
            return False
        name = cmd[1:]
        skills = self.runner.ctx.skills or []
        return any(s.name == name for s in skills)

    def skill_prompt(self, name: str, args: str) -> str | None:
        """The user-turn text for `/<name> <args>`, or None if no such skill.

        Split out of _invoke_skill because `/<skill>` is a model turn wearing
        a slash: a server has to run it the same way it runs typed input
        (worker thread, turn_end, interruptible) rather than inline inside a
        command handler, so it needs the prompt without the turn."""
        skills = self.runner.ctx.skills or []
        sk = next((s for s in skills if s.name == name), None)
        if sk is None:
            return None
        self.recorder.event("skill_invoked", {"name": name, "source": sk.source, "args": args})
        if args:
            return f"Use the {name} skill for the following task:\n\n{sk.body}\n\nTask: {args}"
        return f"Use the {name} skill:\n\n{sk.body}"

    def _invoke_skill(self, name: str, args: str) -> None:
        """Load a skill's body into the conversation as a user turn.

        The skill instructions are sent as the user message so the agent
        follows them for the current task. Optional `args` after the command
        are appended as the actual task (pi's `/skill:name <args>` pattern)."""
        prompt = self.skill_prompt(name, args)
        if prompt is None:
            print(f"no skill named {name!r} — /skills lists available skills")
            return
        self._turn(prompt)

    def _cmd_skills(self) -> None:
        """List available skills (name + description), mirroring /tools."""
        skills = self.runner.ctx.skills or []
        if not skills:
            print("no skills available")
            return
        print(f"skills ({len(skills)}):")
        for s in skills:
            tag = f"  [{s.source}]" if s.source != "project" else ""
            print(f"  /{s.name:20s} {s.description}{tag}")

    def _cmd_reload(self) -> None:
        """Respawn bird with the latest code/skills, resuming this session.

        The plain REPL is a single process, so reload = re-exec: persist the
        transcript (in case the last action wasn't a turn, e.g. right after
        /clear or /model), then replace this process with a fresh `bird chat`
        that resumes the current run-id. The new process re-imports every
        module from disk, so code/skill/tool/instruction changes all take
        effect — no new terminal needed."""
        save_messages(
            [m.to_dict() for m in self.messages],
            self.recorder.run_dir,
        )
        self.recorder.event("reload", {"run_id": self.run_id})
        self.recorder.close()
        # re-exec: keep the same interpreter, re-run `bird chat` with --resume.
        # sys.argv[0] is the bird entrypoint under `python -m bird`; fall back to
        # the running interpreter + `-m bird` so it works either way.
        exe = sys.executable
        args = [exe, "-m", "bird", "chat", "--resume", self.run_id]
        # carry over the repo so the resumed session lands in the same place
        args += ["--repo", str(self.runner.ctx.repo_root)]
        # preserve the current model if the user switched mid-session
        if self.runner.spec.spec != self.registry.aliases.get("default"):
            args += ["--model", self.runner.spec.spec]
        print(f"↻ reloading bird — resuming session {self.run_id} …")
        os.execv(exe, args)

    # ---- onboarding: /setup, /doctor, /keys ----

    def _cmd_setup(self, io=None) -> None:
        """The first-run walkthrough, on demand. A chosen model is switched to
        in this session as well as persisted as the default."""
        from .onboard import ConsoleIO, walkthrough

        chosen = walkthrough(io or ConsoleIO(), self.registry, current_model=self.runner.spec.spec)
        if chosen and chosen != self.runner.spec.spec:
            self._switch_model(chosen)

    def _cmd_doctor(self) -> None:
        from .onboard import doctor_report

        lines, _failed = doctor_report(self.registry, self.runner.ctx.repo_root)
        print("\n".join(lines))

    def _cmd_keys(self, arg: str, io=None) -> None:
        from .onboard import ConsoleIO, keys_status, set_key

        parts = arg.split(maxsplit=2)
        if not parts:
            for name, where in keys_status():
                print(f"  {name:20s} {where}")
            print("  /keys set <NAME> to add or replace one")
            return
        if parts[0].lower() != "set" or len(parts) < 2:
            print("usage: /keys            show which keys are set\n"
                  "       /keys set <NAME> [value]   store one in ~/.bird/.env")
            return
        name = parts[1].upper()
        value = parts[2] if len(parts) > 2 else (io or ConsoleIO()).ask_secret(f"{name}")
        if not value:
            print("no value given; nothing written")
            return
        try:
            path = set_key(name, value)
        except ValueError as e:
            print(f"error: {e}")
            return
        print(f"{name} → {path} (live in this session too)")

    def _cmd_model(self, arg: str) -> None:
        """`/model` walks harness -> model -> thinking level. Direct forms:
        `/model <spec> [tokens]` sets the running harness (the old shape),
        `/model <harness>` starts the walk at the model step, and
        `/model <harness> <spec> [mode] [tokens]` sets everything at once.
        A direct switch carries the running thinking level across; the word
        `keep` instead leaves the new model on its own stored level (what the
        TUI sends when the walk's thinking step is skipped with esc).
        A trailing integer is the context window: without it a spec the
        catalog has no context_length for silently falls back to
        DEFAULT_CONTEXT_WINDOW, which compacts a large model at a fraction of
        its real window and sends it back to re-read its own transcript."""
        tokens = arg.split()
        aliases = self._harness_aliases()
        harness = tokens.pop(0) if tokens and tokens[0] in aliases else None
        if not tokens:
            if harness is None:
                self._harness_picker()
            else:
                self._model_picker(harness, filter_=None)
            return
        spec = tokens.pop(0)
        window: int | None = None
        mode: str | None = None
        carry_think = True
        for tok in tokens:
            if tok.isdigit():
                window = int(tok)
            elif tok in self.THINK_MODES:
                mode = tok
            elif tok == "keep":
                carry_think = False
            else:
                print(f"unexpected {tok!r} — usage: /model [harness] <spec> [mode|keep] [tokens]")
                return
        harness = harness or self._running_harness()
        if spec not in self.registry.aliases and ":" not in spec:
            # not an alias and not provider:model — treat as a picker filter
            self._model_picker(harness, filter_=spec)
            return
        self._apply_harness_model(harness, spec, window, mode, carry_think=carry_think)

    # --- harness -> alias plumbing -------------------------------------

    @staticmethod
    def _harness_aliases() -> dict[str, str]:
        """harness name -> models.json alias, in display order (see
        harnesses.registry.model_aliases)."""
        from .harnesses.registry import model_aliases

        return model_aliases()

    def _running_harness(self) -> str:
        """The harness this session runs — build_runner stamps it on the ctx."""
        return getattr(self.runner.ctx, "harness", None) or "code"

    def _current_model_for(self, harness: str) -> str | None:
        """The model `harness` runs on: the live spec for the running harness
        (which may differ from its alias after a --model override), else
        whatever its alias points at."""
        if harness == self._running_harness():
            return self.runner.spec.spec
        return self.registry.aliases.get(self._harness_aliases()[harness])

    def _harness_rows(self) -> list[dict]:
        """One row per harness for the picker (and serve's harness_list): the
        alias, the model it resolves to, that model's thinking level, and the
        other harnesses sharing the alias."""
        aliases = self._harness_aliases()
        rows = []
        for name, alias in aliases.items():
            model = self._current_model_for(name)
            rows.append({
                "name": name,
                "alias": alias,
                "model": model,
                "think_mode": self._think_label_for(model) if model else None,
                "shared_with": [h for h, a in aliases.items() if a == alias and h != name],
            })
        return rows

    def _harness_picker(self) -> None:
        """Step one of bare /model: list the harnesses (numbered) with their
        current model and thinking level; on a real terminal, prompt for a
        pick and continue to the model step."""
        rows = self._harness_rows()
        running = self._running_harness()
        print("harness:")
        width = max(len(r["name"]) for r in rows)
        mwidth = max(len(r["model"] or "(unset)") for r in rows)
        for i, r in enumerate(rows, 1):
            marker = "*" if r["name"] == running else " "
            think = r["think_mode"] or "auto"
            shared = f", shared with {', '.join(r['shared_with'])}" if r["shared_with"] else ""
            print(
                f" {marker}{i:3d}. {r['name']:<{width}}  {r['model'] or '(unset)':<{mwidth}}"
                f"  think: {think:<6}  [{r['alias']}{shared}]"
            )
        if not getattr(sys.stdin, "isatty", lambda: False)():
            print("pick with /model <harness> [spec] [mode] [tokens]")
            return
        try:
            choice = input("harness # (empty to cancel): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not choice:
            return
        if not choice.isdigit() or not 1 <= int(choice) <= len(rows):
            print(f"not a listed number: {choice!r}")
            return
        self._model_picker(rows[int(choice) - 1]["name"], filter_=None)

    # --- thinking modes ---------------------------------------------------
    # In display order. The friendly label is what the user types and sees;
    # the internal value is what the wire adapter receives as
    # `reasoning_effort`. `off` maps to "none" (thinking disabled); the rest
    # map to themselves. The adapter translates it per provider — ollama
    # forwards it raw, OpenRouter maps it onto its nested `reasoning` object
    # (see OPENROUTER_REASONING_EFFORT in openai_compat.py).
    THINK_MODES = ("off", "low", "medium", "high", "max")
    _THINK_INTERNAL = {"off": "none"}  # everything else maps to itself

    # Per-provider overrides: OpenRouter's reasoning.effort accepts only
    # high|medium|low (the adapter clamps max→high — see
    # OPENROUTER_REASONING_EFFORT in llm/wire/openai_compat.py), so "max"
    # must not be offered or accepted for it. Any provider not listed here
    # gets the full THINK_MODES set.
    _PROVIDER_THINK_MODES = {"openrouter": ("off", "low", "medium", "high")}

    def think_modes_for_provider(self, provider_name: str) -> tuple[str, ...]:
        """The thinking modes a provider actually supports, in display order."""
        return self._PROVIDER_THINK_MODES.get(provider_name, self.THINK_MODES)

    def think_modes(self) -> tuple[str, ...]:
        """Thinking modes for the current model's provider."""
        return self.think_modes_for_provider(self.runner.spec.provider.name)

    def _think_label_of(self, value: str | None) -> str | None:
        """reasoning_effort value -> friendly label; None when unset (Ollama's
        auto/default behavior) or unrecognised."""
        if value is None:
            return None
        if value == "none":
            return "off"
        return value if value in self.THINK_MODES else None

    def _think_label(self) -> str | None:
        """The friendly mode label for the running spec's reasoning_effort, or
        None when no mode is set."""
        return self._think_label_of(self.runner.spec.extra.get("reasoning_effort"))

    def _think_label_for(self, spec: str) -> str | None:
        """The friendly label for any model: the live value for the running
        spec (a /think with nowhere to persist is still in force), else what
        its models.json entry carries."""
        if spec == self.runner.spec.spec:
            return self._think_label()
        return self._think_label_of(self.registry.models.get(spec, {}).get("reasoning_effort"))

    def _cmd_think(self, arg: str) -> None:
        if arg:
            self._set_think_mode(arg)
            return
        # bare /think: show the picker on a tty, else just list + current mode
        self._think_picker()

    def _think_picker(self) -> None:
        """List the thinking modes with a `*` on the active one; on a tty,
        prompt for a mode NAME (not a number). Mirrors /model's tty behavior."""
        current = self._think_label()
        active = current if current is not None else "(auto)"
        print(f"thinking: {active}")
        for mode in self.think_modes():
            marker = "*" if mode == current else " "
            print(f" {marker} {mode}")
        if not getattr(sys.stdin, "isatty", lambda: False)():
            print("set with /think <mode>")
            return
        try:
            choice = input("mode (empty to cancel): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not choice:
            return
        self._set_think_mode(choice)

    _CANCEL = object()  # the /model walk was abandoned mid-way (Ctrl-C / EOF)

    def _think_prompt_for(self, spec: str) -> str | None | object:
        """The thinking step of the /model walk: list the modes `spec`'s
        provider supports with a `*` on its stored level and prompt for a
        NAME. Empty keeps the stored level (None); Ctrl-C/EOF returns _CANCEL.
        Only reached on a tty — the caller checked."""
        modes = self.think_modes_for_provider(spec.split(":", 1)[0])
        current = self._think_label_for(spec)
        print(f"thinking for {spec}: {current if current is not None else '(auto)'}")
        for mode in modes:
            marker = "*" if mode == current else " "
            print(f" {marker} {mode}")
        while True:
            try:
                choice = input("mode (empty to keep): ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return self._CANCEL
            if not choice:
                return None
            if choice in modes:
                return choice
            print(f"unknown mode {choice!r} — valid: {', '.join(modes)}")

    def _set_think_mode(self, label: str) -> None:
        """Validate and apply a thinking mode to the running model by its
        friendly label."""
        self._apply_think(self.runner.spec.spec, label, live=True)

    def _apply_think(self, spec: str, label: str, *, live: bool) -> None:
        """Make `label` the thinking level of `spec`: persisted into its
        models.json entry, and onto the running spec when `live` (the running
        model is the one changing)."""
        modes = self.think_modes() if live else self.think_modes_for_provider(spec.split(":", 1)[0])
        if label not in modes:
            print(f"unknown mode {label!r} — valid: {', '.join(modes)}")
            return
        internal = self._THINK_INTERNAL.get(label, label)
        if live:
            self.runner.spec.extra["reasoning_effort"] = internal
        saved = self.registry.set_think_mode(spec, internal)
        self.recorder.event("think_mode", {"mode": label, "spec": spec})
        note = "" if saved else " (this session only)"
        print(f"thinking: {label}{note}")

    # --- the model step, and applying a pick ------------------------------

    def _model_picker(self, harness: str, filter_: str | None) -> None:
        """List discovered models (numbered) for `harness`; on a real
        terminal, prompt for a pick, then for its thinking level. The pick
        becomes the harness's persisted alias — and switches this session
        when the harness is the running one."""
        alias = self._harness_aliases()[harness]
        current = self._current_model_for(harness)
        if harness == self._running_harness():
            print(f"model for {harness} ({alias}): {current} (context {self.runner.spec.context_window})")
        else:
            print(f"model for {harness} ({alias}): {current or '(unset)'}")
        models, notes = discover_models(self.registry)
        for note in notes:
            print(f"note: {note}")
        if filter_:
            needle = filter_.lower()
            models = [m for m in models if needle in m.spec.lower()]
            if not models:
                print(f"no available model matches {filter_!r}")
                return
        configured = self.registry.aliases.get(alias)
        width = max((len(m.spec) for m in models), default=0)
        for i, m in enumerate(models, 1):
            marker = "*" if m.spec == current else " "
            ctx = f"  {m.context_window // 1024}k ctx" if m.context_window else ""
            think = self._think_label_for(m.spec)
            think = f"  think: {think}" if think else ""
            tag = f"  ({alias})" if m.spec == configured else ""
            print(f" {marker}{i:3d}. {m.spec:<{width}}  [{m.source}]{ctx}{think}{tag}")
        if not getattr(sys.stdin, "isatty", lambda: False)():
            print(f"pick with /model {harness} <spec> [mode] [tokens]")
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
        mode = self._think_prompt_for(picked.spec)
        if mode is self._CANCEL:
            return
        # the walk showed this model's OWN stored level and the user kept or
        # changed it — so the running model's level must not carry over
        self._apply_harness_model(
            harness, picked.spec, picked.context_window, mode, carry_think=False
        )

    def _apply_harness_model(
        self,
        harness: str,
        spec: str,
        window: int | None,
        mode: str | None,
        *,
        carry_think: bool = True,
    ) -> None:
        """Point `harness`'s alias at `spec` (persisted), switch this session
        when that alias is the running harness's (history preserved), then
        apply `mode` as the model's thinking level when one was given."""
        aliases = self._harness_aliases()
        alias = aliases[harness]
        live = alias == aliases[self._running_harness()]
        if live:
            if not self._switch_model(spec, context_window=window, alias=alias, carry_think=carry_think):
                return
            spec = self.runner.spec.spec  # canonical: an alias resolved to its spec
        else:
            if not window:
                window = ollama_context_window(self.registry, spec)
            if window and not self.registry.models.get(spec, {}).get("context_window"):
                # let discovery's context length win over the conservative default
                self.registry.models.setdefault(spec, {})["context_window"] = window
            try:
                spec = self.registry.resolve(spec).spec
            except RegistryError as e:
                print(f"error: {e}")
                return
            saved = self.registry.set_alias(alias, spec, window)
            self.recorder.event("harness_model", {"harness": harness, "alias": alias, "spec": spec})
            where = f"{alias} saved to {self.registry.path}" if saved else f"{alias} updated for this session"
            print(f"{harness}: {spec} ({where}; takes effect on the next {harness} session)")
        if mode is not None:
            self._apply_think(spec, mode, live=live)

    def _switch_model(
        self,
        name: str,
        context_window: int | None = None,
        *,
        alias: str = "default",
        carry_think: bool = True,
    ) -> bool:
        """Swap the running model to `name` (an alias or provider:model),
        keeping the conversation, and persist it as `alias`. Returns False
        when the swap did not happen (bad spec, Ollama can't provide it)."""
        if not context_window:
            # a direct /model <spec> names a model the picker never described;
            # a local Ollama model can still say what window it serves
            context_window = ollama_context_window(self.registry, name)
        spec_key = self.registry.aliases.get(name, name)
        if context_window and not self.registry.models.get(spec_key, {}).get("context_window"):
            # let discovery's context length win over the conservative default
            self.registry.models.setdefault(spec_key, {})["context_window"] = context_window
        try:
            new_spec = self.registry.resolve(name)
        except RegistryError as e:
            print(f"error: {e}")
            return False
        if new_spec.provider.name == "ollama":
            try:
                Ollama(new_spec.provider.native_url or "http://localhost:11434").ensure(new_spec.model)
            except OllamaError as e:
                print(f"error: {e}")
                return False
        old = self.runner.spec.spec
        # a direct /model <spec> carries the thinking mode across the swap so
        # it doesn't silently reset; the picker walk asks instead
        prior_effort = self.runner.spec.extra.get("reasoning_effort")
        self.runner.spec = new_spec
        if carry_think and prior_effort is not None:
            new_spec.extra["reasoning_effort"] = prior_effort
        saved = self.registry.set_alias(alias, new_spec.spec, context_window)
        self.recorder.event("model_switch", {"from": old, "to": new_spec.spec, "alias": alias})
        where = f"{alias} saved to {self.registry.path}" if saved else f"{alias} updated for this session"
        print(f"model: {old} → {new_spec.spec} (history preserved; {where})")
        # resolve() warns about this on stderr, which the TUI does not show —
        # and an unnoticed wrong window is not cosmetic: it is the difference
        # between compacting at the model's real limit and compacting at 32k.
        if "context_window" not in self.registry.models.get(new_spec.spec, {}):
            print(
                f"warning: no context window known for {new_spec.spec}; assuming "
                f"{DEFAULT_CONTEXT_WINDOW // 1024}k. If the model is larger, set it "
                f"with `/model {new_spec.spec} <tokens>` — otherwise compaction "
                f"discards context the model still had room for."
            )
            self.recorder.event(
                "context_window_assumed",
                {"spec": new_spec.spec, "assumed": DEFAULT_CONTEXT_WINDOW},
            )
        return True

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

    def _cmd_mcp(self, arg: str) -> None:
        """In-session MCP surface. Bare /mcp on a tty opens the interactive
        catalog (the store view); /mcp status is the text status, and
        /mcp search and /mcp add remain as text fallbacks for non-interactive
        use — add always asks first, because a registry entry is arbitrary
        code."""
        from .mcp.config import McpError, load_mcp_servers

        parts = arg.split(maxsplit=1)
        action = parts[0].lower() if parts else "status"
        rest = parts[1].strip() if len(parts) > 1 else ""

        if not parts and getattr(sys.stdin, "isatty", lambda: False)():
            # bare /mcp on a tty: the catalog store. Everything below stays
            # available as the text fallback (and for /mcp status|search|add).
            try:
                self._mcp_catalog()
            except McpError as e:
                print(f"error: {e}")
            return

        if action == "status":
            clients = {c.spec.name: c for c in self.runner.ctx.mcp_clients}
            try:
                specs = load_mcp_servers(self.runner.ctx.repo_root)
            except McpError as e:
                print(f"error: {e}")
                return
            if not specs:
                print("no MCP servers configured (.bird/mcp.json, ~/.bird/mcp.json)")
                return
            for spec in specs:
                client = clients.get(spec.name)
                if spec.disabled:
                    print(f"  {spec.name:20s} disabled  [{spec.source}]")
                elif client is None:
                    print(f"  {spec.name:20s} not connected  [{spec.source}]")
                else:
                    alive = "connected" if client._alive() else "died"
                    print(f"  {spec.name:20s} {alive}, {len(client.tools)} tool(s)  [{spec.source}]")
        elif action == "search":
            if not rest:
                print("usage: /mcp search <query>")
                return
            from .mcp.discover import search_registry

            try:
                hits = search_registry(rest)
            except McpError as e:
                print(f"error: {e}")
                return
            if not hits:
                print(f"no registry matches for '{rest}'")
                return
            for h in hits:
                if h.installable:
                    print(f"  {h.name}  (v{h.version})")
                    if h.description:
                        print(f"      {h.description}")
                else:
                    print(f"  {h.name}  — {h.reason}")
        elif action == "add":
            if not rest:
                print("usage: /mcp add <registry-name>")
                return
            self._mcp_add_from_registry(rest)
        else:
            print("usage: /mcp [status|search <query>|add <name>]")

    def _mcp_add_from_registry(self, name: str) -> None:
        """Install a server from the registry into the project mcp.json.

        The confirmation goes through the session's broker as an 'offer'
        payload — the answer IS the choice, and offers stay manual in every
        auto-approve mode by design. No broker (library/test use) falls back
        to a stdin prompt on a tty, and refuses otherwise: installing
        arbitrary code must never default to yes."""
        from .mcp.config import McpError
        from .mcp.discover import fetch_server, package_to_entry
        from .mcp.management import cmd_add

        try:
            server = fetch_server(name)
            entry, warnings = package_to_entry(server)
        except McpError as e:
            print(f"error: {e}")
            return
        print(f"registry entry for '{name}':")
        print(json.dumps({name: entry}, indent=2))
        for w in warnings:
            print(f"warning: {w}")
        broker = self.runner.ctx.broker
        if broker is not None:
            approved, feedback = broker.request({
                "kind": "offer",
                "question": f"Install MCP server '{name}' from the registry? "
                            f"It will run as a local subprocess.",
            })
            answer = feedback.strip().lower() if approved else ""
        elif getattr(sys.stdin, "isatty", lambda: False)():
            try:
                answer = input("install this server? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                return
        else:
            print("no broker and no tty to confirm with — not installed "
                  "(use `bird mcp add --from-registry` from a terminal)")
            return
        if answer not in ("y", "yes"):
            print("not installed")
            return
        args = SimpleNamespace(
            mcp_command="add", name=name, command=None, args=None, env=None,
            scope="project", from_registry=False,
        )
        # write through the same code path as the CLI — one writer for mcp.json
        cmd_add(args, self.runner.ctx.repo_root)

    # ------------------------------------------------- interactive catalog

    def _mcp_connected_infos(self) -> list:
        """The Connected group's data: live clients (tool counts) plus
        configured-but-not-connected servers from mcp.json."""
        from .mcp.catalog import ConnectedInfo
        from .mcp.config import load_mcp_servers

        clients = {c.spec.name: c for c in self.runner.ctx.mcp_clients}
        infos = []
        try:
            specs = load_mcp_servers(self.runner.ctx.repo_root)
        except Exception:
            specs = []
        for spec in specs:
            client = clients.get(spec.name)
            if client is not None and client._alive():
                infos.append(ConnectedInfo(spec.name, True, len(client.tools)))
            else:
                infos.append(ConnectedInfo(spec.name, False, 0))
        return infos

    def _mcp_fetch_catalog(self, query: str = ""):
        """One catalog_page attempt -> (state, error). A stale cache keeps
        the list browsable; only unreachable-with-no-cache is a blank wall."""
        from .mcp import catalog as cat
        from .mcp.config import McpError
        from .mcp.discover import catalog_page

        connected = self._mcp_connected_infos()
        entries, info, error = [], {}, None
        try:
            entries, info = catalog_page(query)
        except McpError as e:
            error = str(e)
        state = cat.state_from_fetch(entries, info, connected, error)
        state.env_set = lambda v: v in os.environ
        return state, error

    def _mcp_catalog(self) -> None:
        """The interactive store: raw-key loop over the catalog state
        machine. Keys go to catalog.handle_key; the returned actions
        (install/open/retry/remove/close) are performed here — the state
        functions stay pure. Install always goes through the confirmation
        path (broker offer or tty y/N — never default yes) and the
        management.py write path."""
        from .mcp import catalog as cat
        from .mcp.config import McpError

        state, error = self._mcp_fetch_catalog()
        if state.degraded == "unreachable":
            # nothing to browse and nothing cached — print the degraded text
            # card instead of trapping the terminal in an empty loop
            print("✗ Couldn't reach the registry")
            print(f"  {error}")
            print("  Nothing cached yet, so there is nothing to browse offline.")
            print(f"  Your {len(state.connected)} configured server(s) are "
                  f"unaffected — the catalog is only for discovery.")
            print("  Retry with /mcp in a moment, or add manually: "
                  "bird mcp add <name> --command <cmd>")
            return
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            while True:
                print("\x1b[2J\x1b[H", end="")
                for line in cat.render(state):
                    print(line)
                key = self._read_raw_key()
                if key is None:
                    break
                action = cat.handle_key(state, key)
                if action == "close" or key is None:
                    break
                if action == "retry":
                    state, _ = self._mcp_fetch_catalog(state.query.strip())
                elif action == "open":
                    if state.target and state.target.repo:
                        print(f"repository: {state.target.repo}")
                elif action == "install":
                    if state.degraded == "stale":
                        # install is disabled on a stale cache — the entry
                        # shown may no longer be what the registry serves
                        state.view = "browse"
                        print("install disabled while offline (cached list) — "
                              "retry when the registry is reachable")
                        continue
                    self._mcp_catalog_install(state)
                elif action == "remove":
                    self._mcp_catalog_remove(state)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        print()

    def _read_raw_key(self):
        """One keypress from the raw terminal, translated to the catalog's
        key names. None on EOF."""
        ch = sys.stdin.read(1)
        if not ch:
            return None
        if ch == "\x1b":
            nxt = sys.stdin.read(1)
            if nxt == "[":
                c = sys.stdin.read(1)
                return {"A": "up", "B": "down", "C": "right", "D": "left"}.get(c, "")
            return "esc"
        if ch == "\r" or ch == "\n":
            return "enter"
        if ch == "\x7f" or ch == "\x08":
            return "backspace"
        if ch == "\t":
            return "tab"
        if ch == "\x04":  # ctrl-D closes like q
            return "close"
        return ch

    def _mcp_catalog_install(self, state) -> None:
        """The confirm card's 'install' action: explicit confirmation (the
        card already required a literal armed 'y'; the broker offer or tty
        y/N below is the session-level gate), then the management.py write
        path and a live connection test feeding the result card."""
        import time as _time

        from .mcp.catalog import ConnectedInfo
        from .mcp.config import McpError, parse_servers
        from .mcp.discover import fetch_server, package_to_entry
        from .mcp.management import install_from_registry, test_connection

        name = state.target.name
        # the session-level confirmation: broker offer, or tty y/N. Offers
        # stay manual in every auto-approve mode by design; never default yes.
        broker = self.runner.ctx.broker
        if broker is not None:
            approved, feedback = broker.request({
                "kind": "offer",
                "question": f"Install MCP server '{name}' from the registry? "
                            f"It will run as a local subprocess.",
            })
            answer = feedback.strip().lower() if approved else ""
        elif getattr(sys.stdin, "isatty", lambda: False)():
            try:
                answer = input("install this server? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                answer = ""
        else:
            answer = ""
        if answer not in ("y", "yes"):
            state.view = "detail"
            return
        try:
            entry, warnings = install_from_registry(
                name, self.runner.ctx.repo_root, scope="project", confirm=True)
        except McpError as e:
            state.result = {"ok": False, "why": str(e), "fix": [], "log": []}
            state.view = "result"
            return
        for w in warnings:
            print(f"warning: {w}")
        # live connection test for the result card
        try:
            spec = parse_servers({"servers": {name: entry}},
                                 self.runner.ctx.repo_root, "project")[0]
        except McpError as e:
            state.result = {"ok": False, "why": str(e), "fix": [], "log": []}
            state.view = "result"
            return
        started = _time.time()
        ok, tools, err, log = test_connection(spec)
        ms = int((_time.time() - started) * 1000)
        if ok:
            sample = [t.get("name", "?") for t in tools[:3]]
            if len(tools) > 3:
                sample.append(f"+{len(tools) - 3}")
            state.result = {"ok": True, "ms": ms, "tools": len(tools),
                            "sample": sample}
            state.connected = [ConnectedInfo(name, True, len(tools))]
        else:
            fix = []
            if any("$" in v for v in entry.get("env", {}).values()):
                fix.append("export the required environment variable, then press r to retry")
            fix.append("run the launch command in a shell to see the full output")
            state.result = {"ok": False, "why": err, "fix": fix, "log": log[-5:]}
        state.view = "result"

    def _mcp_catalog_remove(self, state) -> None:
        """The result card's 'x' action: remove the failed server from
        mcp.json through the same writer the CLI uses."""
        from .mcp.config import McpError
        from .mcp.management import remove_server

        name = state.target.name
        try:
            remove_server(name, self.runner.ctx.repo_root, scope="project")
            state.result = {"ok": True, "removed": True}
            state.connected = [c for c in state.connected if c.name != name]
        except McpError as e:
            state.result = {"ok": False, "why": str(e), "fix": [], "log": []}
        state.view = "result"

    def _sessions_dir(self) -> Path:
        """Location of all past session directories: run_dir is
        .bird/sessions/<run-id>, so its parent IS the sessions dir."""
        return self.recorder.run_dir.parent

    def _list_sessions(self) -> list[dict[str, str]]:
        """Scan .bird/sessions/ and return a list of {id, name, last_event} dicts.

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
        """List all past sessions with their auto-generated names. With a
        non-empty arg, filter the list by substring (case-insensitive). On a
        tty, after listing, offer an interactive picker so the user can
        resume a session without typing /continue."""
        sessions = self._list_sessions()
        if not sessions:
            print("no past sessions found")
            return
        if arg:
            needle = arg.lower()
            sessions = [s for s in sessions if needle in s["name"].lower() or needle in s["id"].lower()]
            if not sessions:
                print(f"no sessions match {arg!r}")
                return
        print(f"past sessions ({len(sessions)}):")
        width = max((len(s["id"]) for s in sessions), default=0)
        for i, s in enumerate(sessions, 1):
            marker = "*" if s["id"] == self.run_id else " "
            last = f"  | {s['last_event']}" if s["last_event"] else ""
            print(f" {marker}{i:3d}. [{s['id']:<{width}}] {s['name']}{last}")
        if not getattr(sys.stdin, "isatty", lambda: False)():
            print("resume with /continue <id|#>")
            return
        try:
            choice = input("resume # (empty to cancel): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not choice:
            return
        if not choice.isdigit() or not 1 <= int(choice) <= len(sessions):
            print(f"not a listed number: {choice!r}")
            return
        self._resume_session(sessions[int(choice) - 1]["id"])

    def _session_picker(self) -> None:
        """Shared /continue picker. Lists past sessions and resumes the
        user's pick; mirrors the model picker's tty-only prompt so CI never
        hangs. Returns silently when there's nothing to pick or the user
        cancels."""
        sessions = self._list_sessions()
        if not sessions:
            print("no past sessions found")
            return
        print(f"past sessions ({len(sessions)}):")
        for i, s in enumerate(sessions, 1):
            marker = "*" if s["id"] == self.run_id else " "
            print(f" {marker}{i:3d}. [{s['id']}] {s['name']}")
        if not getattr(sys.stdin, "isatty", lambda: False)():
            print("pick with /continue <id|#>")
            return
        try:
            choice = input("continue # (empty to cancel): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not choice:
            return
        if not choice.isdigit() or not 1 <= int(choice) <= len(sessions):
            print(f"not a listed number: {choice!r}")
            return
        self._resume_session(sessions[int(choice) - 1]["id"])
