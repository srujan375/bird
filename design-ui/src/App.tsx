import { useCallback, useEffect, useMemo, useState } from "react";
import { openAsk, reopenAsk, selectAsk, setChat, useChat } from "@arch/board/chat";
import { rowAt } from "@arch/board/picker";
import type { Attachment } from "@arch/board/types";
import { ChatHostProvider, type ChatHost } from "@arch/chat/host";
import { Chat } from "@arch/components/Chat";
import { Lightbox } from "@arch/components/Lightbox";
import { readChatClosed, useRail, writeChatOpen } from "@arch/hooks/useRail";
import { dispatchWindowKey, setKeyHandler, type KeyPress } from "./board/keys";
import { nudgeX, reveal } from "./board/stageApi";
import { clearSelection, flash, getUi, useUi } from "./board/ui";
import { AppBar } from "./components/AppBar";
import { Rail, railOpen } from "./components/Rail";
import { Stage } from "./components/Stage";
import { Showcase } from "./components/Showcase";
import { useDrafts } from "./wire/drafts";
import { interrupt, mutate, refusal, sendAnswer, sendBoard, sendInput, uploadAttachment, useSession } from "./wire/session";

export default function App() {
  const session = useSession();
  const ui = useUi();
  const drafts = useDrafts();
  const chat = useChat();
  const { rail, setRail } = useRail();

  const [chatOpen, setChatOpen] = useState(() => !readChatClosed());
  /* Suppress the width transition for the first frame when we open already
     closed — restoring a state is not the same as being put away. */
  const [sizing, setSizing] = useState(() => readChatClosed());
  const [shot, setShot] = useState<Attachment | null>(null);

  useEffect(() => { setChat({ open: chatOpen }); }, [chatOpen]);

  useEffect(() => {
    if (!sizing) return;
    const raf = requestAnimationFrame(() => setSizing(false));
    return () => cancelAnimationFrame(raf);
  }, [sizing]);

  const toggleChat = useCallback((next?: boolean) => {
    setChatOpen((open) => {
      const want = next === undefined ? !open : next;
      if (want === open) return open;
      writeChatOpen(want);
      if (want) setChat({ unread: false });
      /* hold your place: the board gains or loses the rail's width, so slide
         the world half of that and whatever you were reading stays put */
      nudgeX(want ? -rail / 2 : rail / 2, 430);
      return want;
    });
  }, [rail]);

  const readOnly = session.status === "finalized";
  const focus = session.artboards.find((a) => a.id === ui.focus) ?? null;
  const drafting = drafts.length > 0;

  const undo = useCallback(async () => {
    if (readOnly || !focus || focus.versions.length < 2) return;
    /* one stack per artboard, the designer's ops and yours interleaved. The
       push that follows reloads the frame: undo moves the document itself. */
    const res = await mutate({ op: "undo", artboard: focus.id });
    if (!res.ok) refusal(res.error ?? "undo failed");
  }, [focus, readOnly]);

  const finalize = useCallback(async () => {
    if ((session.status !== "ready" && session.status !== "showcase") || !focus) return;
    const res = await mutate({ op: "finalize", artboard: focus.id });
    if (!res.ok) refusal(res.error ?? "finalize failed");
  }, [focus, session.status]);

  /* Back ends the showcase phase: the canvas returns exactly as it was left,
     polished versions still on the stack. */
  const back = useCallback(async () => {
    const res = await mutate({ op: "showcase_exit" });
    if (!res.ok) refusal(res.error ?? "could not go back");
  }, []);

  /* One key handler, reached two ways: the window's keydown, and the `key`
     messages a frame's bridge forwards once a click inside an artboard has
     taken keyboard focus with it. */
  useEffect(() => {
    const onKey = (e: KeyPress) => {
      const mod = e.meta || e.ctrl;
      if (mod && e.key === "\\") { e.preventDefault(); toggleChat(); return; }
      if (mod && e.key.toLowerCase() === "z" && !e.shift && !e.typing) {
        e.preventDefault(); void undo(); return;
      }
      if (mod || e.alt || e.typing) return;
      if (e.key === "Escape") { clearSelection(); return; }
      if (e.key === "f" || e.key === "F") { reveal(null); return; }
      /* Digits choose a row of the open question from anywhere, and Enter
         confirms it. Choosing is deliberately not answering: the confirm
         button is the answer, here as on the page. */
      const open = openAsk();
      if (!open) return;
      const { host, ask } = open;
      const n = Number(e.key);
      if (Number.isInteger(n) && n >= 1 && n <= ask.picker.options.length) {
        const o = ask.picker.options[n - 1];
        if (o.disabled) return;
        e.preventDefault();
        selectAsk(host, o.value);
      } else if (e.key === "Enter" && ask.selected) {
        const o = rowAt(ask.picker, ask.picker.options.findIndex((x) => x.value === ask.selected) + 1);
        if (!o) return;
        e.preventDefault();
        sendAnswer(host, ask.picker.id, o);
      }
    };
    setKeyHandler(onKey);
    addEventListener("keydown", dispatchWindowKey);
    return () => { setKeyHandler(null); removeEventListener("keydown", dispatchWindowKey); };
  }, [toggleChat, undo]);

  /* What the shared rail needs from this board. A message is about the
     element that is selected, or failing that the artboard in focus; "show
     me" brings the artboard a line names on screen. Intake answers may be
     changed until the brief has gone out. Hand edits the designer has not
     seen ride the next message — the server composes them in — so Send is
     live on them alone, as it is on the arch board. */
  const { intake, intakeLocked, pendingEdits } = session;
  const host = useMemo<ChatHost>(() => ({
    who: "Message the designer",
    placeholder: "Describe a change…",
    pendingEdits,
    subjects: () => {
      const { selection, focus: f } = getUi();
      if (selection) return [`${selection.artboard}#${selection.selector}`];
      return f ? [f] : [];
    },
    sendInput,
    sendBoard,
    sendAnswer,
    reveal: (ids) => { reveal(ids); flash(ids); },
    askChange: {
      allowed: (id) => !intakeLocked && intake.includes(id),
      reopen: (turn) => reopenAsk(turn),
    },
    /* while a turn runs the composer offers Stop — a wedged designer turn
       used to have no way to be stopped from the page */
    running: session.running,
    stop: interrupt,
    /* pasted/dropped screenshots are delivered, not just described: the
       upload lands in the session's attachments and the message carries the
       reference, which design_look can then be pointed at */
    upload: uploadAttachment,
  }), [intake, intakeLocked, pendingEdits, session.running]);

  /* The one line in the bar about now. In order: the wire, the phase, what
     the harness says is happening (planning, capturing, the critic), what
     is being drawn, and otherwise where the work is. */
  const writing = drafts.find((d) => !d.closed) ?? drafts[0];
  const sub = (() => {
    if (session.conn === "reconnecting") return "reconnecting to the harness…";
    if (session.conn === "disconnected") return "the harness disconnected";
    if (readOnly) {
      const chosen = session.artboards.find((a) => a.id === session.finalizedArtboard);
      return chosen ? `${chosen.title} · ${chosen.current} · read-only` : "read-only";
    }
    if (session.progress) return session.progress;
    if (writing) {
      const name = writing.artboard
        ? session.artboards.find((a) => a.id === writing.artboard)?.title ?? writing.artboard
        : writing.title ?? "a new artboard";
      return `drawing ${name}`;
    }
    if (session.working) {
      const name = session.artboards.find((a) => a.id === session.working!.artboard)?.title ?? session.working.artboard;
      return `${session.working.verb} ${name}`;
    }
    if (session.status === "showcase") {
      const shown = session.artboards.find((a) => a.id === session.showcaseArtboard);
      return shown ? `showcase · ${shown.title} · ${shown.current}` : "showcase";
    }
    if (session.running) return "the designer is thinking";
    if (focus) return `${focus.title} · ${focus.current}`;
    if (session.ready) return session.ready.model;
    return "";
  })();

  /* the composer's line is about the message, not the session: what it
     will be taken to be about */
  const tip = ui.selection ? `${ui.selection.tag} selected — your message is about it`
    : focus ? `${focus.title} in focus — your message is about it`
    : "Nothing selected — click an element to point at it";

  const canFinalize = (session.status === "ready" || session.status === "showcase") && Boolean(focus) && !drafting;

  return (
    <div className="app" data-chat={chatOpen ? "open" : "closed"}>
      <AppBar
        status={session.status}
        brief={session.prompt}
        sub={sub}
        chatOpen={chatOpen}
        unread={chat.unread}
        canUndo={!readOnly && Boolean(focus) && (focus?.versions.length ?? 0) >= 2}
        canFinalize={canFinalize}
        finalizeTitle={focus?.title ?? ""}
        onUndo={() => void undo()}
        onFinalize={() => void finalize()}
        onBack={session.status === "showcase" ? () => void back() : undefined}
        onToggleChat={() => toggleChat()}
      />

      <main className={"split" + (sizing ? " sizing" : "")} id="content">
        <div className="work" data-rail={railOpen(session.status, Boolean(ui.selection)) ? "on" : "off"}>
          <Rail />
          {session.status === "showcase"
            ? <Showcase />
            : <Stage themeCss={session.themeCss} />}
        </div>
        <ChatHostProvider host={host}>
          <Chat
            turns={chat.turns}
            tip={tip}
            rail={rail}
            setRail={setRail}
            onSizingStart={() => setSizing(true)}
            onSizingEnd={() => setSizing(false)}
            onCollapse={() => toggleChat(false)}
            onOpenShot={setShot}
            readOnly={readOnly || session.conn === "disconnected" || session.conn === "reconnecting" || session.asking}
            readOnlyReason={readOnly
              ? "The session is closed — the design was finalized."
              : session.asking
                ? "Answer the question above to start"
                : session.conn === "reconnecting"
                  ? "Reconnecting to the harness…"
                  : "The harness is gone — nothing you type here can reach it."}
          />
        </ChatHostProvider>
      </main>

      {shot ? <Lightbox shot={shot} onClose={() => setShot(null)} /> : null}
    </div>
  );
}
