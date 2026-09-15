import { createContext, useContext, type ReactNode } from "react";
import type { PickerOption } from "../board/picker";

/**
 * What the conversation needs from the page it sits beside.
 *
 * The chat rail — thread, picker, composer, the seam you drag — is the same
 * on every bird workbench. What differs is where a message goes, what was
 * selected when it was sent, and what "show me" means on that board. Those
 * verbs come in through this seam, so the rail is shared as it is rather than
 * copied, and neither board has to know the other exists.
 *
 * The arch board provides it from `App.tsx`; the design workbench
 * (`design-ui/`) provides its own.
 */
export interface ChatHost {
  /** who is on the other end — the composer's accessible name */
  who: string;
  /** what the empty composer says */
  placeholder: string;
  /** how much the user has drawn that the model has not been shown. Send is
   *  live on this alone, so a batch of gestures goes with no words. */
  pendingEdits: number;
  /** what is selected on the board at the moment Send is pressed: the ids
   *  that travel with the message as context. Read at submit, never held —
   *  the selection can change while you are still typing. */
  subjects: () => string[];
  sendInput: (text: string, subjects: string[]) => void;
  /** send what the user drew, with no words */
  sendBoard: () => void;
  sendAnswer: (host: number, id: string, option: PickerOption) => void;
  /** a "show me" line: bring the things with these ids on screen */
  reveal: (ids: string[]) => void;
  /** whether an answered question may be re-opened, and how. Absent, an
   *  answer is final the moment it is sent. */
  askChange?: {
    allowed: (id: string) => boolean;
    reopen: (host: number, id: string) => void;
  };
  /** whether a turn is running right now. While it is, the composer offers
   *  Stop beside Send — a designer turn that wedges with no way to stop it
   *  from the page is the failure this exists for. */
  running?: boolean;
  /** stop the running turn. Absent, the page has no interrupt route and the
   *  composer never offers Stop. */
  stop?: () => void;
  /** upload an attached file's bytes to the harness; resolves to the
   *  reference text to put in the message (a path the server's ingest flow
   *  recognizes), or null when it could not be delivered. Absent, the page
   *  has no upload channel and attachments stay thumbnails the model is only
   *  told about. */
  upload?: (file: File) => Promise<string | null>;
}

const noop = () => { /* nothing to talk to */ };

/** A rail with nothing behind it — what a component sees outside a provider. */
const silent: ChatHost = {
  who: "Message",
  placeholder: "Say something…",
  pendingEdits: 0,
  subjects: () => [],
  sendInput: noop,
  sendBoard: noop,
  sendAnswer: noop,
  reveal: noop,
};

const Ctx = createContext<ChatHost>(silent);

export function ChatHostProvider({ host, children }: { host: ChatHost; children: ReactNode }) {
  return <Ctx.Provider value={host}>{children}</Ctx.Provider>;
}

export const useChatHost = () => useContext(Ctx);
