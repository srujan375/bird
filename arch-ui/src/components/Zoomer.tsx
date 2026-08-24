import { IconMinus, IconPlus } from "./icons";

interface Props {
  level: number;
  onIn: () => void;
  onOut: () => void;
  onFit: () => void;
  onTidy: () => void;
  /** false when nothing has been moved by hand, or the design is read-only —
   *  there is nothing for a re-arrangement to undo */
  canTidy: boolean;
}

export function Zoomer({ level, onIn, onOut, onFit, onTidy, canTidy }: Props) {
  return (
    <div className="zoomer" role="group" aria-label="zoom" data-od-id="zoom-controls">
      <button id="z-out" title="Zoom out" aria-label="Zoom out" onClick={onOut}><IconMinus /></button>
      <span className="lvl" id="z-lvl">{level}%</span>
      <button id="z-in" title="Zoom in" aria-label="Zoom in" onClick={onIn}><IconPlus /></button>
      <span className="sep" />
      <button id="z-fit" title="Fit the board" onClick={onFit}>Fit</button>
      {/* Arranging around a box somebody placed is not something the layout can
          do — a chosen position is chosen precisely so nothing else moves it.
          This is how you hand the board back. */}
      <button id="z-tidy" title="Re-arrange every box you have moved"
              onClick={onTidy} disabled={!canTidy}>Tidy</button>
    </div>
  );
}
