import { useCallback, useEffect, useMemo, useState } from "react";
import { openAsk, selectAsk, setChat, useChat } from "./board/chat";
import { rowAt } from "./board/picker";
import type { Attachment } from "./board/types";
import { flash, getUi } from "./board/ui";
import { fitNow, frame, nudgeX } from "./board/viewApi";
import { ChatHostProvider, type ChatHost } from "./chat/host";
import { sendAnswer, sendBoard, sendInput, useSession } from "./wire/session";
import { AppBar } from "./components/AppBar";
import { Board } from "./components/Board";
import { Chat } from "./components/Chat";
import { Lightbox } from "./components/Lightbox";
import { readChatClosed, useRail, writeChatOpen } from "./hooks/useRail";

export default function App() {
  const { arch, conn, handedOff, running, pendingEdits, research } = useSession();
  const chat = useChat();
  const { rail, setRail } = useRail();

  const [chatOpen, setChatOpen] = useState(() => !readChatClosed());
  /* Suppress the width transition for the first frame when we open already
     closed — restoring a state is not the same as being put away. */
  const [sizing, setSizing] = useState(() => readChatClosed());
  const [shot, setShot] = useState<Attachment | null>(null);
  const [exportLabel, setExportLabel] = useState("Export board");
  const [tip, setTip] = useState("Select a box to pin your note to it");

  useEffect(() => { setChat({ open: chatOpen }); }, [chatOpen]);

  /* What the shared rail needs from this board. Notes are not components, so
     only a selected box travels as a subject; "show me" frames the boxes a
     line names and halos them. */
  const host = useMemo<ChatHost>(() => ({
    who: "Message the architect",
    placeholder: "Say what's wrong, or point at something on the board…",
    pendingEdits,
    subjects: () => {
      const sel = getUi().selected;
      return sel && sel.t === "node" ? [sel.id] : [];
    },
    sendInput,
    sendBoard,
    sendAnswer,
    reveal: (ids) => { frame(ids, 120, 1.15); flash(ids); },
  }), [pendingEdits]);

  useEffect(() => {
    if (!sizing) return;
    const raf = requestAnimationFrame(() => { setSizing(false); fitNow(64); });
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

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "\\") { e.preventDefault(); toggleChat(); return; }
      /* Digits choose a row of the open question from anywhere, and Enter
         confirms it — but only when the user is not typing, and not with a
         modifier that means something else. Choosing is deliberately not
         answering: the confirm button is the answer, here as on the page. */
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      const t = e.target as HTMLElement | null;
      if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
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
    addEventListener("keydown", onKey);
    return () => removeEventListener("keydown", onKey);
  }, [toggleChat]);

  /** The appbar's one quiet job: what the board is for, and where the argument
   *  has got to. Both derived — neither can lag behind the design. */
  const goal = arch?.brief.goal || "Architecture session";
  const approaches = Object.values(arch?.approaches ?? {});
  const live = approaches.filter((a) => a.status === "active");
  const lost = approaches.filter((a) => a.status === "greyed");
  const sub = (() => {
    if (conn === "reconnecting") return "reconnecting to the harness…";
    if (conn === "disconnected") return "the harness disconnected";
    if (handedOff) return "handed off · read-only";
    if (!arch || !Object.keys(arch.nodes).length) return running ? "thinking…" : "nothing on the board yet";
    if (lost.length && live.length === 1) {
      return `${live[0].name} taken · ${lost.length} on the record as not taken`;
    }
    if (approaches.length) return `${live.length} approaches on the board`;
    return `${Object.keys(arch.nodes).length} boxes`;
  })();

  const onExport = () => {
    if (!arch) return;
    const payload = JSON.stringify(arch, null, 2);
    try {
      const url = URL.createObjectURL(new Blob([payload], { type: "application/json" }));
      const a = document.createElement("a");
      a.href = url;
      a.download = "arch-board.json";
      a.click();
      URL.revokeObjectURL(url);
      setExportLabel("Exported");
    } catch {
      setExportLabel("Export blocked here");
    }
    setTimeout(() => setExportLabel("Export board"), 1800);
  };

  return (
    <div className="app" data-chat={chatOpen ? "open" : "closed"}>
      <AppBar
        goal={goal}
        sub={sub}
        chatOpen={chatOpen}
        unread={chat.unread}
        exportLabel={exportLabel}
        onExport={onExport}
        onToggleChat={() => toggleChat()}
        waiting={chat.pendingAsk ? 1 : 0}
      />

      <main className={"split" + (sizing ? " sizing" : "")} id="content">
        <Board setTip={setTip} />
        <ChatHostProvider host={host}>
        <Chat
          turns={chat.turns}
          tip={running && research.some((r) => !r.done) ? "Type to interrupt and go with what it has." : tip}
          rail={rail}
          setRail={setRail}
          onSizingStart={() => setSizing(true)}
          onSizingEnd={() => setSizing(false)}
          onCollapse={() => toggleChat(false)}
          onOpenShot={setShot}
          readOnly={handedOff || conn === "disconnected" || conn === "reconnecting"}
          readOnlyReason={handedOff
            ? "The design was handed off — this board is read-only."
            : conn === "reconnecting"
              ? "Reconnecting to the harness…"
              : "The harness is gone — nothing you type here can reach it."}
        />
        </ChatHostProvider>
      </main>

      {shot ? <Lightbox shot={shot} onClose={() => setShot(null)} /> : null}
    </div>
  );
}
