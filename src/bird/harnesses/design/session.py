"""DesignSession — the harness-owned wrapper the design tools mutate through.

Modelled on ArchSession, minus the broker, the critic, and the edit tracker.
"""

from __future__ import annotations

import itertools
import json
import shutil
import threading
from pathlib import Path
from typing import Any, Callable

from collections.abc import Sequence

from ..picker import Ask
from . import dom, intake, themes
from .state import Artboard, DesignState, Version

STATE_FILENAME = "design_state.json"
ARTBOARDS_DIRNAME = "artboards"

# How a message says what the user had selected on the workbench when they
# sent it. The same block the arch board uses, so the page reads it back with
# the same code (FOCUS_PREFIX in arch-ui/src/wire/task.ts) and the transcript
# shows "about h1 in Hero — dark" rather than words the user never typed.
FOCUS_PREFIX = "[the user is pointing at]"

# How a message reports what the user edited on the board since the designer
# last heard — the block compose_activity_prompt puts ahead of their words.
BOARD_EDIT_PREFIX = "[the user edited on the board]"

# A page selects one element at a time. The cap is here because the id list
# arrives over HTTP and nothing else bounds it.
MAX_FOCUS = 8

# How a subject names an element: the artboard, then the element-index path
# the bridge and dom.py agree on. A bare artboard id is the frame itself.
SUBJECT_SEP = "#"

# The transport's stop condition reads `status`; the page shows the rest.
# `asking` is the one the designer is not running in: a question is on the
# table and nothing is being drawn behind it.
STATUS_ASKING = "asking"
STATUS_GENERATING = "generating"
STATUS_READY = "ready"
# The showcase phase: one artboard elevated, the board gone, edits locked to
# it. A status, not a phase object — the transport and the page both already
# switch on status, and the late-joiner replay carries it for free.
STATUS_SHOWCASE = "showcase"
STATUS_FINALIZED = "finalized"

# How long a capture request waits for the page's png before it degrades to a
# skip — the BASE, for a small document. Long enough for a foreignObject raster
# of a full artboard; short enough that a closed tab costs the create one note,
# not a hang.
CAPTURE_TIMEOUT = 10.0
# Heavy artboards raster far slower than their byte size suggests (backdrop-
# filter, radial gradients, scroll-driven animation — the ~50KB landing-page
# documents that timed out at the flat 10s), so the budget grows with the
# stored document's length and is capped: a hung page must still cost the
# create one skip note, never a hang.
CAPTURE_MAX_TIMEOUT = 30.0
# Stored-html characters per extra second of budget. ~50KB documents needed
# more than the base; ones in the hundreds of KB reach the cap.
CAPTURE_CHARS_PER_SECOND = 20_000
# The page's own raster timer must expire strictly inside the harness waiter —
# a page answering at the same instant the harness gives up would have its
# reply land on a slot that is already closed.
CAPTURE_PAGE_MARGIN = 2.0


def capture_budget(html_len: int) -> float:
    """How long to wait for a png of a document `html_len` characters long.

    The harness knows the document; the page does not — so the budget is
    computed here and the page's own deadline rides the capture_request
    payload (see request_capture)."""
    return min(CAPTURE_TIMEOUT + html_len / CAPTURE_CHARS_PER_SECOND,
               CAPTURE_MAX_TIMEOUT)


def _noop(event: dict[str, Any]) -> None:
    pass


class DesignSession:
    def __init__(
        self,
        state: DesignState | None = None,
        run_dir: Path | None = None,
        on_state: Callable[[dict[str, Any]], None] | None = None,
        intake_gate: bool = False,
        repo_root: Path | None = None,
    ) -> None:
        self.state = state or DesignState()
        self.run_dir = run_dir
        # where project themes are looked up (<repo>/.bird/design-themes).
        # None falls back to cwd, which is not the repo in a lead dispatch —
        # design_themes listed a project theme that set_theme could not find
        self.repo_root = repo_root
        self.on_state = on_state or _noop
        # Whether the session asks before it draws. Off by default: a design
        # conversation with nobody in the room cannot answer a picker, and a
        # gate nobody can open is a hang. `run_design_interactive` turns it on
        # because that path has a person looking at the page.
        self.intake_gate = bool(intake_gate)
        # A session built from a restored DesignState comes back in the phase
        # it was in; the page's late-joiner replay carries the same status, so
        # a refresh mid-showcase restores the view with no special-casing.
        if self.state.finalized:
            self.status = STATUS_FINALIZED
        elif self.state.showcase_artboard:
            self.status = STATUS_SHOWCASE
        elif self.pending_ask() is not None:
            self.status = STATUS_ASKING
        else:
            self.status = STATUS_READY
        self._write_lock = threading.RLock()  # touched() nests inside apply_edit
        # current document per artboard: disk when run_dir is set, else memory
        self._html: dict[str, str] = {}
        # the active theme's name, or None until set_theme — add_artboard
        # injects its tokens into every artboard created after that. Mirrored
        # in state.theme so a reload comes back styled the way it was left.
        self.theme: str | None = self.state.theme or None
        # The critique loop. `critic` is the vision-model judge (None when no
        # 'vision' alias resolves — every critique then degrades to a skip
        # note); `capture_hook` is what emits a capture_request to the page,
        # installed by run.py, left None in a session with no workbench
        # (headless) — the render critique skips, the plan critique still runs.
        self.critic: Any | None = None
        self.capture_hook: Callable[[dict[str, Any]], None] | None = None
        # the note the last add_artboard/regenerate's auto-critique produced —
        # the skip reason, or the fix-list the create's ToolResult carries
        self.last_critique_note = ""
        # the single-slot capture waiter: one in-flight screenshot, id-matched.
        # The tool thread parks here; the transport thread resolves through
        # resolve_capture — the permission broker's exact round trip.
        self._capture_lock = threading.Lock()
        self._capture_wait: dict[str, Any] | None = None
        self._capture_seq = itertools.count(1)

    @classmethod
    def load(cls, run_dir: Path, **kwargs: Any) -> "DesignSession":
        """A session rebuilt from a run dir: design_state.json plus the
        per-version html files write_version left under artboards/. The
        constructor kwargs are the same as __init__'s. Nothing is re-asked —
        a settled intake stays settled, an unsettled one resumes at the
        question it was on — and the theme, plan, critiques and op log come
        back exactly as persisted. Callers do not call start() after this."""
        run_dir = Path(run_dir)
        path = run_dir / STATE_FILENAME
        if not path.is_file():
            raise FileNotFoundError(f"no design session at {run_dir} ({STATE_FILENAME} is missing)")
        state = DesignState.from_dict(json.loads(path.read_text(encoding="utf-8")))
        kwargs.pop("state", None)
        kwargs.pop("run_dir", None)
        session = cls(state=state, run_dir=run_dir, **kwargs)
        if session.intake_gate:
            # the design-system question exists only in the high-fidelity
            # branch; rebuild the queue from its answers like answer_ask does
            intake.sync(session.state.intake, session.repo_root)
            if session.pending_ask() is not None and not session.state.finalized:
                session.status = STATUS_ASKING
        return session

    # ---- the push ----

    def state_event(self, changed: dict[str, str] | None = None) -> dict[str, Any]:
        """The full-replacement push; `status` is what the transport reads."""
        event: dict[str, Any] = {
            "type": "design_state",
            "status": self.status,
            "changed": changed,
            # the brief the session opened with, for the rail's card: the page
            # has no other way to show what the artboards are answering
            "prompt": self.state.prompt,
            # the active design system, or None until one is set, and its
            # tokens: the page draws an artboard while the designer is still
            # writing it, before add_artboard has injected them
            "theme": self.theme,
            "theme_css": self._theme_css(),
            # the offerable theme names, for the page's start overlay: the page
            # cannot scan the disk, so discovery rides the state push (cached
            # in themes.py — this fires on every mutation)
            "themes": themes.list_themes(self.repo_root),
            "artboards": [
                {
                    "id": a.id,
                    "title": a.title,
                    "current": a.current,
                    # objects, not ids: the page's history panel shows who made
                    # each version, and `note` is where mutate.apply records it
                    "versions": [{"id": v.id, "kind": v.kind, "note": v.note} for v in a.versions],
                    "finalized": self.state.finalized and a.id == self.state.finalized_artboard,
                }
                for a in self.state.artboards.values()
            ],
            "html": {
                aid: self.read_html(aid)
                for aid, a in self.state.artboards.items()
                if a.current_version() is not None
            },
        }
        # the one question on the table, and the whole intake behind it: the
        # page renders `ask` and nothing else, so it cannot get ahead of the
        # user, while `intake` is the record every answered row is drawn from
        pending = self.pending_ask()
        event["ask"] = pending.payload() if pending is not None else None
        event["intake"] = [a.payload() for a in self.state.intake.asks]
        # once the brief has gone out, an answer is history rather than a
        # setting: the page swaps Change for a locked note (the handover's
        # rule — editing it then rewrites the past instead of the future)
        event["intake_locked"] = bool(self.state.intake.asks) and self.state.intake.settled
        # what the critic said, for the page: the latest render/polish critique
        # per artboard and the plan's, so the person watching the round trip
        # sees the fix-list too and not only the designer's paraphrase of it
        event["critiques"] = self._latest_critiques()
        event["plan_critique"] = self.state.plan_critique or None
        # edits the user made on the board that the designer has not been told
        # about yet — drained by compose_activity_prompt when they next send
        event["pending_user_edits"] = len(self._untold_user_edits())
        if self.state.selected:
            event["selected"] = self.state.selected
        if self.state.showcase_artboard:
            # the phase rides the same push the page already reads: status
            # says the view swaps, this says which artboard fills it
            event["showcase_artboard"] = self.state.showcase_artboard
        if self.state.finalized_artboard:
            event["finalized_artboard"] = self.state.finalized_artboard
        return event

    def touched(self, changed_kind: str | None = None, changed_id: str | None = None) -> None:
        """Persist + push the full state after every mutation."""
        with self._write_lock:
            if self.run_dir is not None:
                self.run_dir.mkdir(parents=True, exist_ok=True)
                (self.run_dir / STATE_FILENAME).write_text(
                    json.dumps(self.state.to_dict(), ensure_ascii=False, indent=1),
                    encoding="utf-8",
                )
            changed = None
            if changed_kind is not None and changed_id is not None:
                changed = {"kind": changed_kind, "id": changed_id}
            self.on_state(self.state_event(changed))

    def _theme_css(self) -> str:
        if not self.theme:
            return ""
        try:
            return themes.load_theme(self.theme, self.repo_root)["css"]
        except FileNotFoundError:
            return ""  # a theme removed mid-session: the artboards keep their injected copy

    def _latest_critiques(self) -> dict[str, dict[str, str]]:
        """{artboard: {version, kind, text}} — the newest critique per artboard.
        `kind` is read off the version it judged: a polished version's
        critique judged the motion pass, everything else judged a render."""
        out: dict[str, dict[str, str]] = {}
        for aid, artboard in self.state.artboards.items():
            found = self.state.latest_critique_version(aid)
            if not found:
                continue
            vid, entry = found
            version = next((v for v in artboard.versions if v.id == vid), None)
            kind = "polish" if version is not None and version.kind == "polished" else "render"
            out[aid] = {"version": vid, "kind": kind, "text": str(entry.get("critique") or "")}
        return out

    # ---- what the user did on the board, for the designer ----

    def _untold_user_edits(self) -> list[dict[str, Any]]:
        return [e for e in self.state.op_log if e.get("by") == "user" and not e.get("told")]

    def compose_activity_prompt(self) -> str | None:
        """A turn's worth of prompt from what the user edited on the board
        since the designer last heard, or None. The arch board's contract
        (serve.py finds this by name on the harness session): the entries are
        marked told here, so the same gesture is never reported twice, and
        the pending count on the next push drops to zero."""
        with self._write_lock:
            fresh = self._untold_user_edits()
            if not fresh:
                return None
            lines = [f"- {self._describe_edit(e)}" for e in fresh]
            for e in fresh:
                e["told"] = True
        return "\n".join([BOARD_EDIT_PREFIX, *lines])

    def _describe_edit(self, entry: dict[str, Any]) -> str:
        """One op-log entry in words: the artboard, the op, and — when the
        element can still be found — what it is."""
        op = entry.get("op") or {}
        name = str(op.get("op") or "edit")
        aid = str(entry.get("artboard") or "")
        artboard = self.state.artboards.get(aid)
        where = f"in {artboard.title!r}" if artboard else f"in {aid}"
        at = f" ({entry.get('version')})" if entry.get("version") else ""
        selector = str(op.get("selector") or "")
        label = self._element_label(aid, selector) if name in ("set_style", "set_text", "duplicate") else ""
        target = f"{label} [{selector}]" if label else (f"[{selector}]" if selector else "")
        if name == "set_style":
            props = op.get("props") or {}
            decl = "; ".join(f"{k}: {v}" if v not in ("", None) else f"{k} cleared" for k, v in props.items())
            return f"set_style on {target} {where}{at}: {decl}".replace("  ", " ")
        if name == "set_text":
            text = " ".join(str(op.get("text") or "").split())
            return f'set_text on {target} {where}{at}: "{text[:120]}"'.replace("  ", " ")
        if name == "insert":
            parent = op.get("parent") or "body"
            html = " ".join(str(op.get("html") or "").split())
            return f"insert into [{parent}] {where}{at}: {html[:120]}"
        if name == "move":
            return f"move {target} into [{op.get('new_parent') or op.get('parent') or 'body'}] {where}{at}"
        return f"{name} {target} {where}{at}".replace("  ", " ")

    def _element_label(self, artboard_id: str, selector: str) -> str:
        """`h1.hero "Bird"` for a selector, or "" when it no longer resolves —
        best effort: a later insert may have shifted it, and a guess would
        name the wrong element."""
        if not selector or artboard_id not in self.state.artboards:
            return ""
        html = self.read_html(artboard_id)
        if not html:
            return ""
        try:
            node = dom.resolve(dom.parse(html), selector)
        except dom.DomError:
            return ""
        label = node.tag
        if node.attrs.get("id"):
            label += "#" + node.attrs["id"]
        if node.attrs.get("class"):
            label += "." + ".".join(node.attrs["class"].split()[:2])
        text = " ".join(node.text_content().split())
        if text:
            label += f' "{text[:40]}"'
        return label

    # ---- the document ----

    def read_html(self, artboard_id: str) -> str:
        """The current version's HTML — the harness's own copy."""
        artboard = self._artboard(artboard_id)
        if artboard.current_version() is None:
            return ""
        html = self._html.get(artboard_id)
        if html is not None:
            return html
        if self.run_dir is None:
            return ""
        path = self.run_dir / artboard.current_version().html_path
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    def write_version(self, artboard_id: str, html: str, kind: str, note: str = "") -> Version:
        """Checkpoint a document as the next Version and make it current.

        Stored WRAPPED: the editor bridge rides with EVERY version, the
        generated first one included, so the iframe view and this copy resolve
        the same selectors from the moment an artboard appears. Only boundaries
        that hand html OUT of the harness (handoff's bundle) unwrap."""
        from .mutate import _bridge_script  # lazy: mutate imports this module

        artboard = self._artboard(artboard_id)
        vid = self.state.next_version_id(artboard)
        rel = f"{ARTBOARDS_DIRNAME}/{artboard_id}/{vid}.html"
        script = _bridge_script()
        if script:  # missing bridge file: store unwrapped rather than crash
            html = dom.wrap_editor(html, script)
        self._html[artboard_id] = html
        if self.run_dir is not None:
            path = self.run_dir / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(html, encoding="utf-8")
        version = Version(id=vid, kind=kind, html_path=rel, note=note)
        artboard.versions.append(version)
        artboard.current = vid
        return version

    # ---- what the user is pointing at ----

    def describe_subjects(self, ids: Sequence[str]) -> str | None:
        """What the user had selected on the page, described for the designer.

        A subject is an artboard id (`hero-dark` — the frame itself) or an
        element in one (`hero-dark#0>1>0>0`). Selection lives in the page; the
        harness has no idea what is highlighted until a message says so, and
        "make this bigger" is unanswerable without it. Duck-typed like
        `apply_mutation`: the pump finds this by name on `ctx.design`.

        Unknown ids and selectors that no longer resolve are dropped rather
        than reported: a stale selection is the page being a moment behind the
        document, not something to make the designer explain.
        """
        with self._write_lock:
            blocks = [
                self._describe_subject(subject)
                for subject in list(dict.fromkeys(str(s) for s in ids))[:MAX_FOCUS]
            ]
        kept = [b for b in blocks if b]
        if not kept:
            return None
        return "\n".join([FOCUS_PREFIX, *kept])

    def _describe_subject(self, subject: str) -> str | None:
        """One subject: a `- label` bullet the page reads back for the
        transcript, plus indented lines it ignores and the designer does not."""
        aid, _, selector = subject.partition(SUBJECT_SEP)
        artboard = self.state.artboards.get(aid.strip())
        if artboard is None:
            return None
        at = artboard.current or "?"
        if not selector.strip():
            return f"- {artboard.title}\n  artboard {artboard.id}, at {at}"
        html = self.read_html(artboard.id)
        if not html:
            return None
        try:
            node = dom.resolve(dom.parse(html), selector.strip())
        except dom.DomError:
            return None
        label = node.tag
        if node.attrs.get("id"):
            label += "#" + node.attrs["id"]
        if node.attrs.get("class"):
            label += "." + ".".join(node.attrs["class"].split())
        lines = [
            f"- {label} in {artboard.title}",
            f"  selector {selector.strip()} (artboard {artboard.id}, at {at})",
        ]
        text = " ".join(node.text_content().split())
        if text:
            lines.append(f'  text: "{text[:160]}"')
        return "\n".join(lines)

    # ---- the capture round trip ----

    def request_capture(self, artboard_id: str, version_id: str) -> tuple[str | None, str]:
        """Ask the workbench page for a png of an artboard as it renders now,
        and wait for it. Returns (png data url, "") or (None, why-not).

        The permission broker's exact shape: the tool thread blocks, the
        transport thread resolves through resolve_capture, a timeout or an
        interrupt degrades to a skip. Failure is a skip, never an error — the
        create that asked for eyes must never hang or fail on the page's
        behalf. One slot: a second request while one is parked refuses rather
        than queueing, because the critique that matters is of the newest
        render, and a queued one would judge a stale document anyway."""
        hook = self.capture_hook
        if hook is None:
            return None, "no workbench page is connected"
        # the budget is the document's, not the request's: a heavy artboard
        # gets the time its raster needs. An unknown artboard has no document
        # to size (the capture fails anyway) — the base budget applies.
        try:
            html_len = len(self.read_html(artboard_id))
        except KeyError:
            html_len = 0
        budget = capture_budget(html_len)
        with self._capture_lock:
            if self._capture_wait is not None:
                return None, "another capture is already in flight"
            cap_id = f"cap-{next(self._capture_seq)}"
            wait: dict[str, Any] = {
                "id": cap_id, "event": threading.Event(), "png": None, "error": "",
            }
            self._capture_wait = wait
        try:
            # emitted AFTER the artboard's design_state push (touched() ran in
            # add_artboard before this), so the page has the frame mounted
            # before it is asked to raster it. `deadline` is the page's own
            # timer, derived from the same budget and strictly inside it: the
            # page answers with an error before the harness would give up, so
            # a slow raster is a named skip, never a race with the waiter.
            hook({"id": cap_id, "artboard": artboard_id, "version": version_id,
                  "deadline": round(budget - CAPTURE_PAGE_MARGIN, 2)})
            answered = wait["event"].wait(timeout=budget)
        finally:
            with self._capture_lock:
                self._capture_wait = None
        if not answered:
            return None, (
                f"the page did not raster the artboard within the "
                f"{budget:.0f}s capture budget — it is likely too heavy to "
                "capture (backdrop-filter, huge gradients); simplify the "
                "effects or check the workbench tab is still open")
        if wait["error"]:
            return None, f"the page could not capture: {wait['error']}"
        return wait["png"], ""

    def resolve_capture(self, payload: dict[str, Any]) -> dict[str, Any]:
        """The page's answer to a capture_request: {id, png|error}.

        Duck-typed like apply_mutation and answer_ask: the pump finds this by
        name on ctx.design and never learns what a screenshot is. Id-matched —
        a reply to a capture that already timed out is dropped, not applied to
        whatever is waiting now."""
        with self._capture_lock:
            wait = self._capture_wait
            if wait is None or str(payload.get("id") or "") != wait["id"]:
                return {"ok": False, "error": "no capture is waiting (it may have timed out)"}
            png = payload.get("png")
            if isinstance(png, str) and png.startswith("data:image"):
                wait["png"] = png
            else:
                wait["error"] = str(payload.get("error") or "no image came back")
            wait["event"].set()
        return {"ok": True}

    def cancel_capture(self, reason: str = "interrupted") -> None:
        """Deny a pending capture — the interrupt path. A waiter parked on a
        page that will never answer must not outlive the turn that asked."""
        with self._capture_lock:
            wait = self._capture_wait
            if wait is not None and not wait["event"].is_set():
                wait["error"] = reason
                wait["event"].set()

    def critique_now(self, artboard_id: str, question: str | None = None) -> str:
        """Capture the artboard as rendered, have the critic judge it, record
        the critique, and return the fix-list. Raises ValueError naming the
        reason on any failure — the explicit path (design_critique) surfaces
        that as a ToolError, because a model that asked for eyes should know
        there are none. `question` is the user's "look at this" ask, answered
        ahead of the fix-list — the designer's only way to see its own render."""
        critic = self.critic
        if critic is None:
            raise ValueError(
                "no vision model configured — point the 'vision' alias in "
                "models.json at a multimodal model")
        artboard = self._artboard(artboard_id)
        if not artboard.current:
            raise ValueError(f"artboard {artboard_id!r} has no document yet.")
        png, why = self.request_capture(artboard_id, artboard.current)
        if png is None:
            raise ValueError(why)
        try:
            if question:
                note = critic.critique_render(self, artboard_id, png, question=question)
            else:
                note = critic.critique_render(self, artboard_id, png)
        except Exception as e:  # a dead critic is a skip, not a failed create
            raise ValueError(f"the critic call failed ({e})") from e
        if not note:
            raise ValueError("the critic returned nothing")
        self.state.record_critique(artboard_id, artboard.current, note,
                                   model=getattr(critic, "model", ""))
        self.touched("artboard", artboard_id)
        return note

    def auto_critique(self, artboard_id: str) -> str:
        """The post-create hook: critique_now, with every failure folded into
        a one-line skip note. The create that triggered it must never hang or
        fail because the loop around it could not run — the create lands
        exactly as it did before this loop existed, plus a note."""
        try:
            return self.critique_now(artboard_id)
        except (ValueError, KeyError) as e:
            return f"not critiqued: {e}"

    # ---- the questions asked before anything is drawn ----

    def pending_ask(self) -> Ask | None:
        """The one question the session is waiting on, or None.

        Only the gate parks them, so a session running without a person in
        front of it never has one and never blocks."""
        return self.state.intake.pending() if self.intake_gate else None

    def guard_intake(self) -> None:
        """Refuse work that the pending question would invalidate."""
        pending = self.pending_ask()
        if pending is not None:
            raise ValueError(
                f"the user has not answered {pending.id!r} yet — "
                f"{pending.prompt} Ask, and wait for the answer; designing now "
                "spends the session on artboards nobody asked for."
            )

    def answer_ask(self, payload: dict[str, Any]) -> dict[str, Any]:
        """A picker answer from the page.

        Duck-typed like `apply_mutation`: the pump finds this by name on
        `ctx.design` and never learns what a design question is. Returns the
        turn the answer unblocks, if any — dispatching it is the caller's job,
        because this object does not talk to the model.
        """
        if not self.intake_gate:
            raise ValueError("this session has no questions on the table.")
        queue = self.state.intake
        ask = queue.answer(str(payload.get("id") or ""), str(payload.get("value") or ""))
        # an answer can retire a later question (a wireframe has no design
        # system) or raise one — the queue is rebuilt from the answers it has
        intake.sync(queue, self.repo_root)
        result: dict[str, Any] = {
            "answered": ask.id, "value": ask.answer, "label": ask.answer_label(),
        }
        if queue.settled:
            # everything the designer was waiting on is settled, so the brief
            # held since start() becomes the opening turn
            result["input"] = intake.opening_message(queue, self.state.prompt)
            self.status = STATUS_GENERATING
        else:
            self.status = STATUS_ASKING
        self.touched("question", ask.id)
        return result

    # ---- the showcase phase ----

    def showcase(self, artboard_id: str) -> str:
        """Enter the showcase phase: the picked artboard is elevated, the
        board is gone, and edits lock to it. Returns the polish turn's input —
        dispatching it is the caller's job (the answer_ask pattern), because
        this object does not talk to the model.

        Both doors come through here: the page's pick gesture (POST /mutate,
        op 'showcase') and the model's design_showcase tool. touched() pushes
        the state BEFORE this returns, so a page that dispatches the turn has
        already swapped to the showcase view when the designer starts writing
        — the same ordering the capture hook respects."""
        if self.state.finalized:
            raise ValueError("the design was finalized — the session is read-only now.")
        if self.status == STATUS_SHOWCASE:
            raise ValueError(
                f"the showcase phase is already on "
                f"{self.state.showcase_artboard!r} — Back first to widen the field.")
        artboard = self._artboard(artboard_id)
        if not artboard.current:
            raise ValueError(f"artboard {artboard_id!r} has no document to showcase yet.")
        self.state.showcase_artboard = artboard_id
        self.state.selected = artboard_id
        self.status = STATUS_SHOWCASE
        self.touched("artboard", artboard_id)
        return self._polish_turn(artboard)

    def showcase_exit(self) -> None:
        """Back: the phase ends and the canvas returns exactly as it was left.
        The polished versions stay on the stack — going back never throws work
        away; re-entering showcases the same document again."""
        if self.status != STATUS_SHOWCASE:
            raise ValueError("the session is not in the showcase phase.")
        self.state.showcase_artboard = ""
        self.status = STATUS_READY
        self.touched()

    def _polish_turn(self, artboard: Artboard) -> str:
        """The polish turn's input, composed like intake.opening_message: the
        designer learns the phase changed without re-reading instructions."""
        return (
            f"Showcase: the user picked {artboard.title!r} ({artboard.id}). "
            "The board is gone — that artboard now fills the screen as the "
            "finished result. Polish it for handoff with design_polish: one "
            "orchestrated page-load animation, hover micro-interactions, and "
            "section reveals only where they earn their place. CSS first; "
            "inline script only where an interaction needs it. The "
            "design-craft motion rules still govern — one page-load "
            "orchestration max, no scroll-reveal spam."
        )

    # ---- the session lifecycle ----

    def start(self, prompt: str) -> None:
        """A session opens.

        With the gate on the designer is not running yet: the first question
        goes to the page and the brief waits behind it. Without it, this is
        what it always was — a generation pass beginning.
        """
        self.state.prompt = prompt
        if self.intake_gate and not self.state.intake.asks:
            self.state.intake.add(intake.fidelity_ask())
        if self.pending_ask() is not None:
            self.status = STATUS_ASKING
        elif self.state.artboards:
            self.status = STATUS_READY  # a resumed board: nothing is being drawn yet
        else:
            self.status = STATUS_GENERATING
        self.touched()

    def set_theme(self, name: str) -> dict[str, str]:
        """Adopt a theme. Validates through the loader — an unknown name raises
        its FileNotFoundError, naming the theme and the known ones. Returns
        the loaded theme so the caller can inject its css into artboards that
        already exist; new ones get it from add_artboard."""
        theme = themes.load_theme(name, self.repo_root)
        self.theme = theme["name"]
        self.state.theme = theme["name"]
        self.touched()
        return theme

    def add_artboard(self, html: str, label: str) -> Artboard:
        """A generated artboard: Version 1, kind 'generated'.

        The auto-critique runs after the push, so the page has the frame
        mounted before it is asked to raster it; the note lands in
        last_critique_note for the create's ToolResult."""
        aid = self.state.next_artboard_id(label)
        artboard = Artboard(id=aid, title=label)
        self.state.artboards[aid] = artboard
        if not self.state.selected:
            self.state.selected = aid
        self.status = STATUS_READY
        if self.theme:  # the tokens ride with version 1, like the bridge does
            html = dom.inject_theme(html, self._theme_css())
        self.write_version(aid, html, "generated", note="generated")
        self.touched("artboard", aid)
        self.last_critique_note = self.auto_critique(aid)
        return artboard

    def regenerate(self, artboard_id: str, html: str, note: str = "rebuilt") -> Version:
        """A rebuilt document for an artboard that already exists: a new
        Version, kind 'refined'. Selectors are fresh — nothing that resolved
        against the old version resolves against this one."""
        self._artboard(artboard_id)
        self.state.selected = artboard_id
        self.status = STATUS_READY
        version = self.write_version(artboard_id, html, "refined", note=note)
        self.touched("artboard", artboard_id)
        self.last_critique_note = self.auto_critique(artboard_id)
        return version

    def apply_edit(self, artboard_id: str, op: dict[str, Any]) -> dict[str, Any]:
        """One dom.py op against the harness's own copy, checkpointed as a
        Version — through mutate.apply, the same door the page's inspector
        uses. Raises MutationError on a refusal."""
        from .mutate import apply  # lazy: mutate imports this module

        version = apply(self, artboard_id, op, actor="user")
        return {"applied": f"Applied {op.get('op')} to {artboard_id}.", "version": version.id}

    def undo(self, artboard_id: str) -> dict[str, Any]:
        """Step an artboard back one checkpoint — through mutate.undo, the same
        door design_undo uses, so the page and the model share one stack.
        Raises MutationError on a refusal."""
        from .mutate import undo  # lazy: mutate imports this module

        version = undo(self, artboard_id)
        return {"applied": f"Undid {artboard_id}.", "version": version.id}

    def delete_artboard(self, artboard_id: str) -> None:
        """Remove an artboard from the board outright — card, document and
        version history gone. The board-level act the element-level delete op
        inside design_edit cannot do. Both doors come through here: the
        model's design_delete_artboard tool and the page's card control
        (POST /mutate, op 'delete_artboard').

        Deliberately NOT in the op-undo stack: design_undo steps document ops
        within an artboard, and replaying them cannot resurrect a whole
        document's version history cheaply. Deletion is a deliberate act
        guarded by the page's confirm — the house pattern for the irreversible
        (cf. finalize). An empty board is legal: design_create starts again.
        """
        if self.state.finalized:
            raise ValueError("the design was finalized — the session is read-only now.")
        if self.status == STATUS_SHOWCASE:
            # the phase belongs to the elevated artboard; deleting under it —
            # the elevated one included — would pull the full-bleed view out
            # from under itself. Back is the clean path out of the phase.
            raise ValueError(
                "the showcase phase owns the screen — Back ends it before an "
                "artboard can be deleted.")
        self._artboard(artboard_id)  # KeyError names the known ones
        del self.state.artboards[artboard_id]
        # critiques are keyed "artboard:version" — with the artboard gone they
        # are orphans no version can point at again
        for key in [k for k in self.state.critiques if k.split(":", 1)[0] == artboard_id]:
            del self.state.critiques[key]
        # the stored versions are generated artifacts of a document that no
        # longer exists — the same files write_version created, safe to remove
        if self.run_dir is not None:
            shutil.rmtree(self.run_dir / ARTBOARDS_DIRNAME / artboard_id,
                          ignore_errors=True)
        self._html.pop(artboard_id, None)
        if self.state.selected == artboard_id:
            self.state.selected = next(iter(self.state.artboards), "")
        self.touched("artboard", artboard_id)

    def finalize(self, artboard_id: str) -> None:
        """The chosen artboard is recorded; the session is read-only after.

        Writing the handoff bundle is part of finalizing, not a step a caller
        can forget: whoever dispatched this session (the lead's `design` tool,
        `bird design`) reads DESIGN.md off disk the moment this returns, and
        both finalize paths — the model's tool and the page's button — come
        through here."""
        from .handoff import write_bundle  # lazy: handoff imports this module

        self._artboard(artboard_id)
        self.state.finalized = True
        self.state.finalized_artboard = artboard_id
        # the phase is over either way — finalized from inside it or past it
        self.state.showcase_artboard = ""
        self.status = STATUS_FINALIZED
        if self.run_dir is not None:
            write_bundle(self, self.run_dir)
        self.touched("artboard", artboard_id)

    def _artboard(self, artboard_id: str) -> Artboard:
        artboard = self.state.artboards.get(artboard_id)
        if artboard is None:
            known = ", ".join(self.state.artboards) or "none"
            raise KeyError(f"no artboard {artboard_id!r} (known: {known})")
        return artboard