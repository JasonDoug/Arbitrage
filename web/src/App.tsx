import { useCallback, useEffect, useState } from "react";
import { api, venueLabel, type Market, type Meta, type Notification } from "./api";
import { MarketDrawer } from "./components/MarketDrawer";
import { NewsSidebar } from "./components/NewsSidebar";
import { useLive } from "./live";
import { ArbitragePage } from "./pages/ArbitragePage";
import { MarketsPage } from "./pages/MarketsPage";
import { WatchlistPage } from "./pages/WatchlistPage";

type Page = "markets" | "watchlist" | "arb";

const NAV: { id: Page | string; label: string; soon?: string }[] = [
  { id: "markets", label: "Markets" },
  { id: "watchlist", label: "Watchlist" },
  { id: "arb", label: "Arbitrage" },
  { id: "ai", label: "AI Picks", soon: "Sprint 2" },
  { id: "portfolio", label: "Portfolio", soon: "Sprint 3" },
  { id: "sim", label: "Simulation", soon: "Sprint 3+" },
];

export default function App() {
  const [page, setPage] = useState<Page>(() => {
    const h = location.hash.slice(1);
    return h === "watchlist" || h === "arb" ? h : "markets";
  });
  const [meta, setMeta] = useState<Meta | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const [contested, setContestedState] = useState(() => {
    try {
      return localStorage.getItem("hideNearCertain") !== "0";
    } catch {
      return true;
    }
  });
  const setContested = (v: boolean) => {
    setContestedState(v);
    try {
      localStorage.setItem("hideNearCertain", v ? "1" : "0");
    } catch {
      /* storage unavailable: keep in memory only */
    }
  };
  const { connected, lastNotification } = useLive();

  useEffect(() => {
    const load = () => api.meta(contested).then(setMeta).catch(() => undefined);
    load();
    const t = window.setInterval(load, 60000);
    return () => window.clearInterval(t);
  }, [contested]);

  useEffect(() => {
    location.hash = page === "markets" ? "" : page;
  }, [page]);

  const [showNews, setShowNewsState] = useState(() => {
    try {
      return localStorage.getItem("showNews") !== "0";
    } catch {
      return true;
    }
  });
  const toggleNews = () => {
    setShowNewsState((v) => {
      try {
        localStorage.setItem("showNews", v ? "0" : "1");
      } catch {
        /* in-memory only */
      }
      return !v;
    });
  };
  const onOpen = useCallback((m: Market) => setOpen(m.id), []);
  const onChanged = useCallback(() => setRefreshKey((k) => k + 1), []);

  return (
    <div className="app">
      <div className="mode-band" role="status">
        <span className="mode-dot" /> LIVE DATA · READ-ONLY — no orders can be placed from this app
      </div>
      <header className="topbar">
        <div className="brand">
          predict<span>arb</span>
        </div>
        <div className="venues">
          {(meta?.venues ?? []).map((v) => (
            <span key={v.id} className={`venue-status ${v.status}`} title={v.last_error ?? `last ok ${v.last_ok_at ? new Date(v.last_ok_at).toLocaleTimeString() : "never"}`}>
              <i /> {venueLabel[v.id] ?? v.name} <b>{v.open_markets.toLocaleString()}</b>
            </span>
          ))}
          <span className={`venue-status ${connected ? "ok" : "error"}`} title="Live price stream">
            <i /> {connected ? "Streaming" : "Reconnecting…"}
          </span>
        </div>
        <button className={`btn news-toggle ${showNews ? "on" : ""}`} onClick={toggleNews} aria-pressed={showNews}>
          News
        </button>
        <Bell latest={lastNotification} onOpenMarket={(id) => setOpen(id)} />
      </header>
      <div className="body">
        <nav className="sidenav" aria-label="Main">
          {NAV.map((n) => (
            <button
              key={n.id}
              className={`nav-item ${page === n.id ? "on" : ""}`}
              disabled={!!n.soon}
              aria-current={page === n.id ? "page" : undefined}
              onClick={() => !n.soon && setPage(n.id as Page)}
            >
              {n.label}
              {n.soon && <em>{n.soon}</em>}
            </button>
          ))}
        </nav>
        <main>
          {page === "markets" && (
            <MarketsPage meta={meta} onOpen={onOpen} refreshKey={refreshKey} contested={contested} setContested={setContested} />
          )}
          {page === "watchlist" && <WatchlistPage onOpen={onOpen} refreshKey={refreshKey} />}
          {page === "arb" && <ArbitragePage onOpenMarket={(id) => setOpen(id)} />}
        </main>
        {showNews && <NewsSidebar onOpenMarket={(id) => setOpen(id)} latest={lastNotification} />}
      </div>
      {open && <MarketDrawer marketId={open} onClose={() => setOpen(null)} onChanged={onChanged} />}
      <Toasts latest={lastNotification} onOpenMarket={(id) => setOpen(id)} />
    </div>
  );
}

function Bell({ latest, onOpenMarket }: { latest: Notification | null; onOpenMarket: (id: string) => void }) {
  const [items, setItems] = useState<Notification[]>([]);
  const [unread, setUnread] = useState(0);
  const [show, setShow] = useState(false);

  const load = () =>
    api
      .notifications()
      .then((r) => {
        setItems(r.items);
        setUnread(r.unread);
      })
      .catch(() => undefined);

  useEffect(() => {
    load();
  }, [latest]);

  const toggle = () => {
    setShow((s) => !s);
    if (!show && unread) api.readNotifications().then(() => setUnread(0));
  };

  return (
    <div className="bell-wrap">
      <button className="bell" onClick={toggle} aria-label={`Notifications, ${unread} unread`} aria-expanded={show}>
        🔔{unread > 0 && <span className="bell-count">{unread}</span>}
      </button>
      {show && (
        <div className="bell-menu">
          {items.length === 0 ? (
            <div className="muted pad">No alerts yet.</div>
          ) : (
            items.map((n) => (
              <button
                key={n.id}
                className={`bell-item ${n.read ? "" : "unread"}`}
                onClick={() => {
                  if (n.market_id) onOpenMarket(n.market_id);
                  setShow(false);
                }}
              >
                <span>{n.message}</span>
                <time>{new Date(n.ts).toLocaleString()}</time>
              </button>
            ))
          )}
        </div>
      )}
    </div>
  );
}

function Toasts({ latest, onOpenMarket }: { latest: Notification | null; onOpenMarket: (id: string) => void }) {
  const [toasts, setToasts] = useState<Notification[]>([]);
  useEffect(() => {
    if (!latest) return;
    setToasts((t) => [...t, latest].slice(-4));
    const id = latest.id;
    const timer = window.setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 8000);
    return () => window.clearTimeout(timer);
  }, [latest]);
  return (
    <div className="toasts" aria-live="polite">
      {toasts.map((t) => (
        <button key={t.id} className="toast" onClick={() => t.market_id && onOpenMarket(t.market_id)}>
          <strong>{t.kind === "news" ? "News on a watched market" : "Price alert"}</strong>
          <span>{t.message}</span>
        </button>
      ))}
    </div>
  );
}
