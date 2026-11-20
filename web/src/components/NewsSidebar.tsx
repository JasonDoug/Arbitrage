import { useCallback, useEffect, useState } from "react";
import { ago, newsApi, venueLabel, type NewsItem, type Notification } from "../api";

type Scope = "watchlist" | "matched" | "all";
const SCOPES: { id: Scope; label: string; title: string }[] = [
  { id: "watchlist", label: "Watchlist", title: "News matched to markets you watch" },
  { id: "matched", label: "Markets", title: "News matched to any tracked market" },
  { id: "all", label: "All", title: "Every headline from the feeds" },
];

export function NewsSidebar({ onOpenMarket, latest }: { onOpenMarket: (id: string) => void; latest: Notification | null }) {
  const [scope, setScope] = useState<Scope>("watchlist");
  const [items, setItems] = useState<NewsItem[] | null>(null);

  const load = useCallback(() => {
    newsApi
      .list({ scope, limit: 60 })
      .then((r) => setItems(r.items))
      .catch(() => setItems([]));
  }, [scope]);

  useEffect(() => {
    load();
    const t = window.setInterval(load, 60000);
    return () => window.clearInterval(t);
  }, [load]);

  useEffect(() => {
    if (latest?.kind === "news") load();
  }, [latest, load]);

  return (
    <aside className="news-sidebar" aria-label="News">
      <div className="news-head">
        <h2>News</h2>
        <div className="seg" role="tablist">
          {SCOPES.map((s) => (
            <button key={s.id} role="tab" title={s.title} aria-selected={scope === s.id} className={scope === s.id ? "on" : ""} onClick={() => setScope(s.id)}>
              {s.label}
            </button>
          ))}
        </div>
      </div>
      <div className="news-list">
        {items === null && <div className="muted pad">Loading…</div>}
        {items?.length === 0 && (
          <div className="muted pad">
            {scope === "watchlist" ? "No headlines matched to your watched markets yet. Feeds refresh every 5 minutes." : "No headlines yet. Feeds refresh every 5 minutes."}
          </div>
        )}
        {items?.map((n) => <NewsRow key={n.id} n={n} onOpenMarket={onOpenMarket} />)}
      </div>
    </aside>
  );
}

export function NewsRow({ n, onOpenMarket, compact = false }: { n: NewsItem; onOpenMarket?: (id: string) => void; compact?: boolean }) {
  return (
    <article className="news-row">
      <div className="news-meta">
        <span>{n.source}</span>
        <time dateTime={n.published_at} title={new Date(n.published_at).toLocaleString()}>
          {ago(n.published_at)}
        </time>
      </div>
      <a href={n.url} target="_blank" rel="noreferrer" className="news-title">
        {n.title}
      </a>
      {!compact && n.markets.length > 0 && onOpenMarket && (
        <div className="news-markets">
          {n.markets.slice(0, 2).map((m) => (
            <button key={m.id} className="news-chip" onClick={() => onOpenMarket(m.id)} title={`matched on: ${m.terms.join(", ")}`}>
              <span className={`venue-dot venue-${m.venue}`}>{venueLabel[m.venue]?.[0] ?? "?"}</span>
              {m.title}
              {m.outcome_label ? ` — ${m.outcome_label}` : ""}
            </button>
          ))}
          {n.markets.length > 2 && <span className="muted">+{n.markets.length - 2}</span>}
        </div>
      )}
    </article>
  );
}
