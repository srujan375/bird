import { useCallback, useEffect, useState } from "react";
import { openAsk, setChat, useChat } from "./board/chat";
import type { Attachment } from "./board/types";
import { fitNow, nudgeX } from "./board/viewApi";
import { useSession } from "./wire/session";
import { AppBar } from "./components/AppBar";
import { Board } from "./components/Board";
import { Chat } from "./components/Chat";
import { Lightbox } from "./components/Lightbox";
import { readChatClosed, useRail, writeChatOpen } from "./hooks/useRail";

export default function App() {
  const { arch, conn, handedOff, running } = useSession();
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
      /* Digits 1–3 answer the open question from anywhere — but only when the
         user is not typing, and not with a modifier that means something else. */
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      const t = e.target as HTMLElement | null;
      if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
      const ask = openAsk();
      if (!ask) return;
      const n = Number(e.key);
      if (Number.isInteger(n) && n >= 1 && n <= Math.min(3, ask.opts.length)) {
        e.preventDefault();
        const o = ask.opts[n - 1];
        if (ask.onPick) ask.onPick(o); else sendPick(o.label);
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
      />

      <main className={"split" + (sizing ? " sizing" : "")} id="content">
        <Board setTip={setTip} />
        <Chat
          turns={chat.turns}
          tip={tip}
          rail={rail}
          setRail={setRail}
          onSizingStart={() => setSizing(true)}
          onSizingEnd={() => setSizing(false)}
          onCollapse={() => toggleChat(false)}
          onOpenShot={setShot}
          readOnly={handedOff || conn === "disconnected"}
          readOnlyReason={handedOff
            ? "The design was handed off — this board is read-only."
            : "The harness is gone — nothing you type here can reach it."}
        />
      </main>

      {shot ? <Lightbox shot={shot} onClose={() => setShot(null)} /> : null}
    </div>
  );
}
