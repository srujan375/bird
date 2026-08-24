import { useEffect, useState } from "react";
import type { Attachment } from "../board/types";

const human = (n: number) =>
  n < 1024 ? n + " B" : n < 1048576 ? Math.round(n / 1024) + " KB" : (n / 1048576).toFixed(1) + " MB";

/** Full-size look at a screenshot. Escape and a click anywhere both close it,
 *  and it fades out before it leaves the tree. */
export function Lightbox({ shot, onClose }: { shot: Attachment; onClose: () => void }) {
  const [on, setOn] = useState(false);

  useEffect(() => {
    let raf2 = 0;
    const raf1 = requestAnimationFrame(() => { raf2 = requestAnimationFrame(() => setOn(true)); });
    return () => { cancelAnimationFrame(raf1); cancelAnimationFrame(raf2); };
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") { e.preventDefault(); onClose(); } };
    addEventListener("keydown", onKey);
    return () => removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div
      className="lightbox"
      role="dialog"
      aria-label={shot.name}
      data-od-id="image-preview"
      {...(on ? { "data-on": "1" } : {})}
      onClick={onClose}
    >
      <img src={shot.url ?? ""} alt={shot.name} />
      <span className="cap mono">{shot.name} · {human(shot.size)} · click anywhere to close</span>
    </div>
  );
}
