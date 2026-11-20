import { cents, closesIn, compact, signedCents, venueLabel, type Market } from "../api";
import { useLiveMarket } from "../live";
import { Sparkline } from "./Sparkline";

type Props = { market: Market; onOpen: (m: Market) => void; onToggleWatch: (m: Market) => void };

export function MarketCard({ market, onOpen, onToggleWatch }: Props) {
  const m = useLiveMarket(market);
  const ch = m.change_24h ?? 0;
  const volume = m.volume_24h_usd ?? null;
  return (
    <article
      className="card"
      tabIndex={0}
      onClick={() => onOpen(market)}
      onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && (e.preventDefault(), onOpen(market))}
      aria-label={`${m.title}${m.outcome_label ? ` — ${m.outcome_label}` : ""}`}
    >
      <header className="card-head">
        <span className={`venue venue-${m.venue}`}>{venueLabel[m.venue] ?? m.venue}</span>
        <span className="cat">{m.category}</span>
        <button
          className={`star ${m.watched ? "on" : ""}`}
          title={m.watched ? "Remove from watchlist" : "Add to watchlist"}
          aria-pressed={!!m.watched}
          onClick={(e) => {
            e.stopPropagation();
            onToggleWatch(market);
          }}
        >
          {m.watched ? "★" : "☆"}
        </button>
      </header>
      <h3 className="card-title">{m.title}</h3>
      {m.outcome_label && <div className="outcome">{m.outcome_label}</div>}
      <div className="card-price">
        <div>
          <div className={`yes ${m.flash ? `flash-${m.flash}` : ""}`} key={m.quote_ts ?? ""}>
            {cents(m.mid, 0)}
          </div>
          <div className="price-label">YES</div>
        </div>
        <div className="card-trend">
          <Sparkline values={m.sparkline ?? []} />
          <span className={`chg ${ch > 0.0005 ? "pos" : ch < -0.0005 ? "neg" : ""}`}>{signedCents(m.change_24h)} 24h</span>
        </div>
      </div>
      <footer className="card-foot">
        <span title="24h volume (USD; Kalshi estimated from contracts × price)">Vol {compact(volume, "$")}</span>
        <span title="Bid–ask spread">Spr {cents(m.spread)}</span>
        <span title={m.close_ts ? new Date(m.close_ts).toLocaleString() : ""}>Closes {closesIn(m.close_ts)}</span>
      </footer>
    </article>
  );
}
