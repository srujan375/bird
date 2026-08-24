import { useCallback, useEffect, useRef, useState } from "react";
import { you } from "../board/chat";
import { getUi, nextLocalId } from "../board/ui";
import type { Attachment } from "../board/types";
import { sendBoard, sendInput, useSession } from "../wire/session";
import { useArriving } from "../hooks/useArriving";
import { IconClip, IconDoc, IconWarn, IconX } from "./icons";

const MAX_ATT = 6;
const MAX_BYTES = 10 * 1024 * 1024;

const human = (n: number) =>
  n < 1024 ? n + " B" : n < 1048576 ? Math.round(n / 1024) + " KB" : (n / 1048576).toFixed(1) + " MB";

function Staged({ att, onDrop }: { att: Attachment; onDrop: (id: string) => void }) {
  const arriving = useArriving();
  return (
    <div
      className={"att " + (att.img ? "att-img" : "att-file") + (arriving ? " arriving" : "")}
      data-od-id={"staged-" + att.id}
    >
      {att.img ? (
        <img src={att.url ?? ""} alt={att.name} />
      ) : (
        <>
          <IconDoc />
          <span className="nm">{att.name}</span>
          <span className="sz">{human(att.size)}</span>
        </>
      )}
      <button type="button" className="att-x" aria-label={"Remove " + att.name} onClick={() => onDrop(att.id)}>
        <IconX />
      </button>
    </div>
  );
}

interface Props {
  tip: string;
  disabled: boolean;
  /** why it is disabled — a handed-off design and a dropped connection are
   *  different things and must not wear the same message */
  reason: string;
}

/** What drawing on the board says, once you have stopped drawing.
 *
 *  It is written into the box rather than offered as a separate button so it
 *  is a draft you can see, edit, add to, or delete — the same as anything else
 *  you were about to send. Nothing goes to the model until you send it. */
const drawnMessage = (n: number) =>
  `I've made ${n} ${n === 1 ? "change" : "changes"} on the board, please check.`;

export function Composer({ tip, disabled, reason }: Props) {
  const { pendingEdits } = useSession();
  const [text, setText] = useState("");
  const [atts, setAtts] = useState<Attachment[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [dropping, setDropping] = useState(false);
  const draft = useRef<HTMLTextAreaElement | null>(null);
  const fileInput = useRef<HTMLInputElement | null>(null);
  const errTimer = useRef(0);
  const dragDepth = useRef(0);

  /* Whether what is in the box is still ours to rewrite. The moment you touch
     it, it is yours: a later edit updates the count only if you have not
     started saying something of your own. */
  const ours = useRef(false);
  const current = useRef("");
  current.current = text;

  useEffect(() => {
    if (pendingEdits === 0) {
      if (ours.current) { setText(""); ours.current = false; }
      return;
    }
    if (current.current === "" || ours.current) {
      setText(drawnMessage(pendingEdits));
      ours.current = true;
    }
  }, [pendingEdits]);

  const fail = useCallback((msg: string) => {
    setError(msg);
    clearTimeout(errTimer.current);
    errTimer.current = window.setTimeout(() => setError(null), 7000);
  }, []);

  const grow = useCallback(() => {
    const el = draft.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = Math.min(132, el.scrollHeight) + "px";
  }, []);

  /* Not on mount: measuring an empty field lets its placeholder decide the
     height, which is a two-line box before you have typed anything. */
  const measured = useRef(false);
  useEffect(() => {
    if (!measured.current) { measured.current = true; return; }
    grow();
  }, [text, atts, grow]);

  const addFiles = useCallback((list: FileList | File[] | null) => {
    const all = [...(list || [])];
    if (!all.length) return;
    setError(null);
    const big = all.filter((f) => f.size > MAX_BYTES);
    const ok = all.filter((f) => f.size <= MAX_BYTES);
    setAtts((prev) => {
      const take = ok.slice(0, Math.max(0, MAX_ATT - prev.length));
      const made = take.map((f) => {
        const img = /^image\//.test(f.type);
        return {
          id: nextLocalId("f"),
          name: f.name || (img ? "pasted-image.png" : "file"),
          size: f.size,
          img,
          url: img ? URL.createObjectURL(f) : null,
        } satisfies Attachment;
      });
      if (take.length < ok.length) {
        fail(`${MAX_ATT} files per message is the limit — ${ok.length - take.length} were not attached.`);
      }
      return [...prev, ...made];
    });
    if (big.length) {
      fail(big.length === 1
        ? `${big[0].name} is ${human(big[0].size)}. 10 MB is the limit — send a crop or a link instead.`
        : `${big.length} files are over the 10 MB limit and were not attached.`);
    }
  }, [fail]);

  const dropAtt = (id: string) => {
    setAtts((prev) => {
      const hit = prev.find((a) => a.id === id);
      if (hit?.url) URL.revokeObjectURL(hit.url);
      return prev.filter((a) => a.id !== id);
    });
  };

  const carriesFiles = (e: React.DragEvent) =>
    Boolean(e.dataTransfer) && [...(e.dataTransfer.types || [])].includes("Files");

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    const body = text.trim();
    const sent = atts;
    if (disabled) return;
    if (!body && !sent.length) {
      /* nothing typed, but something drawn — send that instead of an empty
         message. One gesture, one turn. */
      if (pendingEdits > 0) sendBoard();
      return;
    }

    setAtts([]);
    setError(null);
    setText("");
    ours.current = false;

    /* The thumbnails stay in the thread so you can see what you showed it.
       The harness's own transcript echoes the text back on `run_start`, so
       only an attachment-only message needs its own turn here. */
    if (sent.length) you(body || undefined, sent);

    /* Whatever box is live when you press Send is what the message is about.
       Read at submit, not held in state: the selection can change while you
       are still typing, and the one that counts is the one you were looking at
       when you sent it. Notes are not components, so only boxes travel. */
    const sel = getUi().selected;
    const subjects = sel && sel.t === "node" ? [sel.id] : [];

    if (body) {
      sendInput(body, subjects);
    } else {
      /* Files are read by the person, not the harness: this page has no upload
         channel, so say what arrived rather than pretend it was delivered. */
      sendInput(
        `[the user attached ${sent.length} file(s): ${sent.map((a) => a.name).join(", ")}]`,
        subjects,
      );
    }
  };

  /* Typing and drawing go together: whatever is on the board travels with the
     message, so Send is live when either has something in it. */
  const canSend = (Boolean(text.trim()) || atts.length > 0 || pendingEdits > 0) && !disabled;

  return (
    <form className="composer" id="composer" data-od-id="composer" onSubmit={submit}
      onDragEnter={(e) => { if (!carriesFiles(e)) return; e.preventDefault(); if (++dragDepth.current === 1) setDropping(true); }}
      onDragOver={(e) => { if (carriesFiles(e)) e.preventDefault(); }}
      onDragLeave={() => { if (--dragDepth.current <= 0) { dragDepth.current = 0; setDropping(false); } }}
      onDrop={(e) => {
        if (!carriesFiles(e)) return;
        e.preventDefault();
        dragDepth.current = 0;
        setDropping(false);
        addFiles(e.dataTransfer.files);
        draft.current?.focus();
      }}
    >
      <div className={"composer-box" + (dropping ? " dropping" : "")} id="composer-box">
        <p className="drop-note">drop to attach</p>

        <p className="composer-error" id="composer-error" role="alert" hidden={!error} data-od-id="composer-error">
          <IconWarn />
          <span id="composer-error-text">{error}</span>
        </p>

        <div className="tray" id="tray" data-od-id="attachment-tray">
          {atts.map((a) => <Staged key={a.id} att={a} onDrop={dropAtt} />)}
        </div>

        <textarea
          id="draft"
          ref={draft}
          rows={1}
          value={text}
          disabled={disabled}
          placeholder={disabled ? reason : "Say what's wrong, or point at something on the board…"}
          aria-label="Message the architect"
          onChange={(e) => { ours.current = false; setText(e.target.value); }}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              (e.currentTarget.form as HTMLFormElement).requestSubmit();
            }
          }}
          /* a pasted screenshot is the fastest way to show me something */
          onPaste={(e) => {
            const f = e.clipboardData?.files;
            if (f && f.length) { e.preventDefault(); addFiles(f); }
          }}
        />

        <div className="composer-foot">
          <button className="attach" id="btn-attach" type="button"
                  title="Attach files or images" aria-label="Attach files or images"
                  data-od-id="attach-file" disabled={disabled}
                  onClick={() => fileInput.current?.click()}>
            <IconClip />
          </button>
          <span className="tip" id="tip">{tip}</span>
          <button className="send" id="send" type="submit" disabled={!canSend}>Send</button>
        </div>

        <input
          type="file" id="file-input" multiple hidden aria-hidden="true" tabIndex={-1}
          ref={fileInput}
          onChange={(e) => { addFiles(e.target.files); e.target.value = ""; draft.current?.focus(); }}
        />
      </div>
    </form>
  );
}
