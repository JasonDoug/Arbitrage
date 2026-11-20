import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import type { Market, Notification, QuoteUpdate } from "./api";

type Live = {
  quotes: Map<string, QuoteUpdate>;
  connected: boolean;
  lastNotification: Notification | null;
};

const LiveContext = createContext<Live>({ quotes: new Map(), connected: false, lastNotification: null });

const FLUSH_MS = 250; // at most 4 UI updates per second (design doc, section 8)

export function LiveProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<Live>({ quotes: new Map(), connected: false, lastNotification: null });
  const buffer = useRef<Map<string, QuoteUpdate>>(new Map());

  useEffect(() => {
    let ws: WebSocket | null = null;
    let retry = 1000;
    let closed = false;
    let ping: number | undefined;

    const connect = () => {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(`${proto}://${location.host}/ws`);
      ws.onopen = () => {
        retry = 1000;
        setState((s) => ({ ...s, connected: true }));
        ping = window.setInterval(() => ws?.readyState === WebSocket.OPEN && ws.send("ping"), 25000);
      };
      ws.onmessage = (ev) => {
        const msg = JSON.parse(ev.data);
        if (msg.type === "quotes") {
          for (const q of msg.data as QuoteUpdate[]) buffer.current.set(q.id, q);
        } else if (msg.type === "notification") {
          setState((s) => ({ ...s, lastNotification: msg.data as Notification }));
        }
      };
      ws.onclose = () => {
        window.clearInterval(ping);
        setState((s) => ({ ...s, connected: false }));
        if (!closed) setTimeout(connect, (retry = Math.min(retry * 2, 30000)));
      };
    };
    connect();

    const flush = window.setInterval(() => {
      if (buffer.current.size === 0) return;
      const batch = buffer.current;
      buffer.current = new Map();
      setState((s) => {
        const quotes = new Map(s.quotes);
        batch.forEach((q, id) => quotes.set(id, q));
        return { ...s, quotes };
      });
    }, FLUSH_MS);

    return () => {
      closed = true;
      window.clearInterval(flush);
      ws?.close();
    };
  }, []);

  return <LiveContext.Provider value={state}>{children}</LiveContext.Provider>;
}

export const useLive = () => useContext(LiveContext);

/** Overlay the latest streamed quote onto a market row. */
export function useLiveMarket<T extends Market>(m: T): T & { flash: "up" | "down" | null } {
  const { quotes } = useLive();
  const q = quotes.get(m.id);
  const prev = useRef(m.mid);
  const merged = q && (!m.quote_ts || q.ts > m.quote_ts) ? { ...m, ...q, quote_ts: q.ts } : m;
  let flash: "up" | "down" | null = null;
  if (merged.mid !== null && prev.current !== null && merged.mid !== prev.current) {
    flash = merged.mid > prev.current ? "up" : "down";
  }
  useEffect(() => {
    prev.current = merged.mid;
  });
  return { ...merged, flash };
}
