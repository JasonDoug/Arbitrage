import { useEffect, useState } from "react";
import { api, cents, closesIn, signedCents, sparkValues, venueLabel, type Market } from "../api";
import { Sparkline } from "../components/Sparkline";
import { useLiveMarket } from "../live";

type Props = { onOpen: (m: Market) => void; refreshKey: number };

export function WatchlistPage({ onOpen, refreshKey }: Props) {
  const [items, setItems] = useState<Market[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = () =>
    api
      .watchlist()
      .then((r) => setItems(r.items))
      .catch((e) => setError(String(e)));

  useEffect(() => {
    load();
  }, [refreshKey]);

  if (error) return <div className="page"><div className="error">{error}</div></div>;
  if (!items) return <div className="page"><div className="loading">Loading…</div></div>;

  return (
    <div className="page">
      <h1 className="page-title">Watchlist</h1>
      {items.length === 0 ? (
        <div className="empty">Nothing watched yet. Star a market card, or add an alert to a market, to track it here.</div>
      ) : (
        <div className="table-wrap">
          <table className="watch-table">
            <thead>
              <tr>
                <th>Market</th>
                <th className="num">YES</th>
                <th className="num">Since added</th>
                <th className="num">24h</th>
                <th>Trend</th>
                <th>Alerts</th>
                <th className="num">Closes</th>
                <th aria-label="Actions" />
              </tr>
            </thead>
            <tbody>
              {items.map((m) => (
                <Row key={m.id} market={m} onOpen={onOpen} onRemove={() => api.unwatch(m.id).then(load)} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function Row({ market, onOpen, onRemove }: { market: Market; onOpen: (m: Market) => void; onRemove: () => void }) {
  const m = useLiveMarket(market);
  const since = m.mid !== null && m.added_price != null ? m.mid - m.added_price : null;
  const cls = (d: number | null) => (d === null ? "" : d > 0.0005 ? "pos" : d < -0.0005 ? "neg" : "");
  const rules = m.alert_rules ?? [];
  return (
    <tr onClick={() => onOpen(market)} className="clickable">
      <td>
        <div className="wt-title">{m.title}</div>
        <div className="wt-sub">
          <span className={`venue venue-${m.venue}`}>{venueLabel[m.venue] ?? m.venue}</span>
          {m.outcome_label && <span>{m.outcome_label}</span>}
          {m.notes && <span className="muted">“{m.notes}”</span>}
        </div>
      </td>
      <td className={`num mono ${m.flash ? `flash-${m.flash}` : ""}`} key={m.quote_ts ?? ""}>
        {cents(m.mid)}
      </td>
      <td className={`num mono chg ${cls(since)}`}>{since === null ? "—" : signedCents(since)}</td>
      <td className={`num mono chg ${cls(m.change_24h)}`}>{signedCents(m.change_24h)}</td>
      <td>
        <Sparkline values={sparkValues(m)} width={80} height={24} />
      </td>
      <td>{rules.length ? `${rules.length} active` : <span className="muted">none</span>}</td>
      <td className="num mono">{closesIn(m.close_ts) === "closed" ? "past end" : closesIn(m.close_ts)}</td>
      <td>
        <button
          className="link"
          onClick={(e) => {
            e.stopPropagation();
            onRemove();
          }}
        >
          remove
        </button>
      </td>
    </tr>
  );
}
