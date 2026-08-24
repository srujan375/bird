import type { Tool } from "../board/types";
import { IconAddBox, IconAddNote, IconSelect } from "./icons";

const TOOLS: { tool: Tool; title: string; label: string; icon: () => JSX.Element }[] = [
  { tool: "select", title: "Select and move (V)", label: "Select and move", icon: IconSelect },
  { tool: "node",   title: "Add a box (N)",       label: "Add a box",       icon: IconAddBox },
  { tool: "note",   title: "Add a note (T)",      label: "Add a note",      icon: IconAddNote },
];

export function Dock({ tool, onPick }: { tool: Tool; onPick: (t: Tool) => void }) {
  return (
    <div className="dock" role="group" aria-label="board tools" data-od-id="tool-dock">
      {TOOLS.map(({ tool: t, title, label, icon: Icon }) => (
        <button
          key={t}
          data-tool={t}
          {...(tool === t ? { "data-on": "1" } : {})}
          title={title}
          aria-label={label}
          onClick={() => onPick(t)}
        >
          <Icon />
        </button>
      ))}
    </div>
  );
}
