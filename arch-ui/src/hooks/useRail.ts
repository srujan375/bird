import { useCallback, useEffect, useState } from "react";

export const RAIL_MIN = 320;
export const RAIL_MAX = 620;
const DEFAULT_RAIL = 400;

const read = (key: string) => {
  try { return localStorage.getItem(key); } catch { return null; } /* private mode */
};
const write = (key: string, value: string) => {
  try { localStorage.setItem(key, value); } catch { /* private mode */ }
};

/** Come back to the width and the state you left. */
export function useRail() {
  const [rail, setRailState] = useState(() => {
    const saved = parseFloat(read("arch.rail") || "");
    return saved ? Math.min(RAIL_MAX, Math.max(RAIL_MIN, saved)) : DEFAULT_RAIL;
  });

  const setRail = useCallback((px: number) => {
    const clamped = Math.round(Math.min(RAIL_MAX, Math.max(RAIL_MIN, px)));
    setRailState(clamped);
    write("arch.rail", String(clamped));
    return clamped;
  }, []);

  useEffect(() => {
    document.documentElement.style.setProperty("--rail", rail + "px");
  }, [rail]);

  return { rail, setRail };
}

export const readChatClosed = () => read("arch.chat") === "closed";
export const writeChatOpen = (open: boolean) => write("arch.chat", open ? "open" : "closed");
