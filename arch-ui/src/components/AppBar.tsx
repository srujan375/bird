import { IconChat } from "./icons";

interface Props {
  goal: string;
  sub: string;
  chatOpen: boolean;
  unread: boolean;
  exportLabel: string;
  onExport: () => void;
  onToggleChat: () => void;
  /** questions waiting in the dock while the rail is away */
  waiting?: number;
}

export function AppBar({ goal, sub, chatOpen, unread, exportLabel, onExport, onToggleChat, waiting = 0 }: Props) {
  return (
    <header className="appbar" data-od-id="appbar">
      <h1 className="goal" id="goal" title={goal} data-od-id="session-goal">{goal}</h1>
      <span className="sub" id="sub">{sub}</span>
      <span className="spacer" />
      <button className="bar-btn" id="btn-export" data-od-id="export-board" onClick={onExport}>
        {exportLabel === "Export board"
          ? <>Export<span className="wide"> board</span></>
          : exportLabel}
      </button>
      <button
        className="bar-btn chat-toggle"
        id="btn-chat"
        type="button"
        aria-expanded={chatOpen}
        aria-controls="chat"
        title={(chatOpen ? "Hide" : "Show") + " the conversation ⌘\\"}
        data-od-id="toggle-chat"
        {...(unread ? { "data-unread": "1" } : {})}
        onClick={onToggleChat}
      >
        <IconChat />
        <span>Chat</span>
        {!chatOpen && waiting > 0
          ? <span className="waiting" data-od-id="chat-waiting">{waiting} question{waiting > 1 ? "s" : ""}</span>
          : null}
        <i className="unread" aria-hidden="true" />
      </button>
    </header>
  );
}
