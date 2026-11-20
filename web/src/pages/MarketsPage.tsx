import { useCallback, useEffect, useRef, useState } from "react";
import { api, venueLabel, type Market, type Meta } from "../api";
import { MarketCard } from "../components/MarketCard";

const SORT_LABEL: Record<string, string> = {
  volume: "24h volume",
  change: "Biggest 24h move",
  closing: "Closing soon",
  spread: "Tightest spread",
  liquidity: "Liquidity",
  newest: "Newest",
};
const PAGE = 60;

type Props = { meta: Meta | null; onOpen: (m: Market) => void; refreshKey: number };

export function MarketsPage({ meta, onOpen, refreshKey }: Props) {
  const [category, setCategory] = useState("");
  const [venue, setVenue] = useState("");
  const [sort, setSort] = useState("volume");
  const [q, setQ] = useState("");
  const [query, setQuery] = useState("");
  const [items, setItems] = useState<Market[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const reqId = useRef(0);

  useEffect(() => {
    const t = window.setTimeout(() => setQuery(q.trim()), 300);
    return () => window.clearTimeout(t);
  }, [q]);

  const load = useCallback(
    async (offset: number) => {
      const id = ++reqId.current;
      setLoading(true);
      try {
        const res = await api.markets({ category, venue, q: query, sort, limit: PAGE, offset });
        if (id !== reqId.current) return;
        setTotal(res.total);
        setItems((prev) => (offset === 0 ? res.items : [...prev, ...res.items]));
        setError(null);
      } catch (e) {
        if (id === reqId.current) setError(String(e));
      } finally {
        if (id === reqId.current) setLoading(false);
      }
    },
    [category, venue, query, sort],
  );

  useEffect(() => {
    load(0);
  }, [load, refreshKey]);

  const toggleWatch = async (m: Market) => {
    await (m.watched ? api.unwatch(m.id) : api.watch(m.id));
    setItems((prev) => prev.map((x) => (x.id === m.id ? { ...x, watched: !m.watched } : x)));
  };

  const counts = meta?.category_counts ?? {};
  const allCount = Object.values(counts).reduce((a, b) => a + b, 0);

  return (
    <div className="page">
      <div className="toolbar">
        <div className="chips" role="tablist" aria-label="Categories">
          <button role="tab" aria-selected={category === ""} className={`chip ${category === "" ? "on" : ""}`} onClick={() => setCategory("")}>
            All <span>{allCount}</span>
          </button>
          {(meta?.categories ?? []).map((c) => (
            <button key={c} role="tab" aria-selected={category === c} className={`chip ${category === c ? "on" : ""}`} onClick={() => setCategory(c)} disabled={!counts[c]}>
              {c} <span>{counts[c] ?? 0}</span>
            </button>
          ))}
        </div>
        <div className="filters">
          <input className="search" type="search" placeholder="Search markets…" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search markets" />
          <select value={venue} onChange={(e) => setVenue(e.target.value)} aria-label="Venue">
            <option value="">All venues</option>
            {(meta?.venues ?? []).map((v) => (
              <option key={v.id} value={v.id}>
                {venueLabel[v.id] ?? v.name}
              </option>
            ))}
          </select>
          <select value={sort} onChange={(e) => setSort(e.target.value)} aria-label="Sort by">
            {(meta?.sorts ?? Object.keys(SORT_LABEL)).map((s) => (
              <option key={s} value={s}>
                {SORT_LABEL[s] ?? s}
              </option>
            ))}
          </select>
        </div>
      </div>

      {error && <div className="error">Couldn't load markets: {error}</div>}
      <div className="result-count">
        {loading && items.length === 0 ? "Loading…" : `${total.toLocaleString()} markets`}
      </div>

      {!loading && items.length === 0 && !error ? (
        <div className="empty">
          {allCount === 0 ? "No markets yet. The ingest worker fills the catalog within a minute of starting." : "No markets match these filters."}
        </div>
      ) : (
        <div className="grid">
          {items.map((m) => (
            <MarketCard key={m.id} market={m} onOpen={onOpen} onToggleWatch={toggleWatch} />
          ))}
        </div>
      )}

      {items.length < total && (
        <div className="more">
          <button className="btn" disabled={loading} onClick={() => load(items.length)}>
            {loading ? "Loading…" : `Load more (${(total - items.length).toLocaleString()} left)`}
          </button>
        </div>
      )}
    </div>
  );
}
