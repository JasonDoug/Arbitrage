import { useCallback, useEffect, useState } from "react";
import { api, cents, closesIn, compact, signedCents, venueLabel, type AlertKind, type HistoryPoint, type Market } from "../api";
import { useLiveMarket } from "../live";
import { PriceChart } from "./PriceChart";

const RANGES = ["1D", "1W", "1M", "ALL"] as const;

type Props = { marketId: string; onClose: () => void; onChanged: () => void };

export function MarketDrawer({ marketId, onClose, onChanged }: Props) {
  const [market, setMarket] = useState<Market | null>(null);
  const [range, setRange] = useState<(typeof RANGES)[number]>("1W");
  const [points, setPoints] = useState<HistoryPoint[]>([]);
  const [loadingHistory, setLoadingHistory] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const reload = useCallback(() => api.market(marketId).then(setMarket).catch((e) => setError(String(e))), [marketId]);

  useEffect(() => {
    setMarket(null);
    reload();
  }, [reload]);

  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    const load = (attempt: number) => {
      setLoadingHistory(true);
      api
        .history(marketId, range)
        .then((h) => {
          if (cancelled) return;
          setPoints(h.points);
          setLoadingHistory(false);
          // History is being fetched from the venue for the first time: poll a few times.
          if (h.backfill_requested && attempt < 4) timer = window.setTimeout(() => load(attempt + 1), 3000);
        })
        .catch(() => !cancelled && setLoadingHistory(false));
    };
    load(0);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [marketId, range]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="drawer-backdrop" onClick={onClose}>
      <aside className="drawer" role="dialog" aria-modal="true" aria-label="Market details" onClick={(e) => e.stopPropagation()}>
        <button className="drawer-close" onClick={onClose} aria-label="Close">
          ✕
        </button>
        {error && <div className="error">{error}</div>}
        {!market ? (
          <div className="loading">Loading…</div>
        ) : (
          <DrawerBody
            market={market}
            range={range}
            setRange={setRange}
            points={points}
            loadingHistory={loadingHistory}
            onChanged={() => {
              reload();
              onChanged();
            }}
          />
        )}
      </aside>
    </div>
  );
}

function DrawerBody({
  market,
  range,
  setRange,
  points,
  loadingHistory,
  onChanged,
}: {
  market: Market;
  range: (typeof RANGES)[number];
  setRange: (r: (typeof RANGES)[number]) => void;
  points: HistoryPoint[];
  loadingHistory: boolean;
  onChanged: () => void;
}) {
  const m = useLiveMarket(market);
  const toggleWatch = async () => {
    await (m.watched ? api.unwatch(m.id) : api.watch(m.id));
    onChanged();
  };
  const ch = m.change_24h ?? 0;
  return (
    <>
      <div className="drawer-head">
        <div className="drawer-tags">
          <span className={`venue venue-${m.venue}`}>{venueLabel[m.venue] ?? m.venue}</span>
          <span className="cat">{m.category}</span>
          {m.venue === "demo" && <span className="badge-demo">DEMO DATA</span>}
        </div>
        <h2>{m.title}</h2>
        {m.outcome_label && <div className="outcome big">{m.outcome_label}</div>}
        {m.event_title && <div className="muted">{m.event_title}</div>}
      </div>

      <div className="quote-row">
        <div className="quote-main">
          <span className="yes-big">{cents(m.mid)}</span>
          <span className={`chg ${ch > 0.0005 ? "pos" : ch < -0.0005 ? "neg" : ""}`}>{signedCents(m.change_24h)} 24h</span>
        </div>
        <div className="quote-actions">
          <button className={`btn ${m.watched ? "" : "primary"}`} onClick={toggleWatch}>
            {m.watched ? "★ Watching" : "☆ Watch"}
          </button>
          {m.url && (
            <a className="btn" href={m.url} target="_blank" rel="noreferrer">
              Open on {venueLabel[m.venue] ?? m.venue} ↗
            </a>
          )}
        </div>
      </div>

      <dl className="stats">
        <Stat label="YES bid" value={cents(m.yes_bid)} />
        <Stat label="YES ask" value={cents(m.yes_ask)} />
        <Stat label="Last" value={cents(m.last_price)} />
        <Stat label="Spread" value={cents(m.spread)} />
        <Stat label="24h volume" value={m.volume_unit === "contracts" ? `${compact(m.volume_24h)} ct` : compact(m.volume_24h_usd, "$")} />
        <Stat label="Total volume" value={m.volume_unit === "contracts" ? `${compact(m.total_volume)} ct` : compact(m.total_volume, "$")} />
        <Stat label={m.venue === "kalshi" ? "Open interest" : "Liquidity"} value={m.venue === "kalshi" ? `${compact(m.open_interest)} ct` : compact(m.liquidity_usd, "$")} />
        <Stat
          label="Scheduled end"
          value={m.close_ts ? `${new Date(m.close_ts).toLocaleDateString()} (${closesIn(m.close_ts) === "closed" ? "passed, still trading" : closesIn(m.close_ts)})` : "—"}
        />
      </dl>

      <section className="panel">
        <div className="panel-head">
          <h3>Price history</h3>
          <div className="seg" role="tablist">
            {RANGES.map((r) => (
              <button key={r} role="tab" aria-selected={r === range} className={r === range ? "on" : ""} onClick={() => setRange(r)}>
                {r}
              </button>
            ))}
          </div>
        </div>
        {points.length > 1 ? (
          <PriceChart points={points} />
        ) : (
          <div className="chart-empty">{loadingHistory ? "Loading history…" : "No history yet. Prices are recorded as they change, and older history is fetched from the venue."}</div>
        )}
      </section>

      <AlertEditor market={m} onChanged={onChanged} />

      <section className="panel">
        <h3>Resolution rules</h3>
        {m.resolution_source && (
          <p className="source">
            Resolution source: <strong>{m.resolution_source}</strong>
          </p>
        )}
        <p className="rules">{m.rules_text || "No rules text provided by the venue."}</p>
      </section>
    </>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="stat">
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

const KIND_LABEL: Record<AlertKind, string> = { price_above: "Price rises above", price_below: "Price falls below", move: "Moves by at least" };

function AlertEditor({ market, onChanged }: { market: Market; onChanged: () => void }) {
  const [kind, setKind] = useState<AlertKind>("price_above");
  const [value, setValue] = useState(() => String(Math.min(99, Math.round((market.mid ?? 0.5) * 100) + 5)));
  const [windowMin, setWindowMin] = useState("60");
  const [err, setErr] = useState<string | null>(null);
  const rules = market.alert_rules ?? [];

  const add = async () => {
    const v = Number(value);
    if (!(v > 0 && v < 100)) return setErr("Enter a value between 1 and 99 cents");
    setErr(null);
    try {
      await api.createAlert({ market_id: market.id, kind, threshold: v / 100, window_minutes: Number(windowMin) || 60 });
      onChanged();
    } catch (e) {
      setErr(String(e));
    }
  };

  return (
    <section className="panel">
      <h3>Price alerts</h3>
      {rules.length > 0 && (
        <ul className="rules-list">
          {rules.map((r) => (
            <li key={r.id}>
              <span>
                {KIND_LABEL[r.kind]} <strong>{(r.threshold * 100).toFixed(1)}¢</strong>
                {r.kind === "move" && ` in ${r.window_minutes} min`}
              </span>
              <span className="muted">{r.last_fired_at ? `last fired ${new Date(r.last_fired_at).toLocaleString()}` : "not fired yet"}</span>
              <button className="link" onClick={() => api.deleteAlert(r.id).then(onChanged)} aria-label="Delete alert">
                remove
              </button>
            </li>
          ))}
        </ul>
      )}
      <div className="alert-form">
        <select value={kind} onChange={(e) => setKind(e.target.value as AlertKind)} aria-label="Alert type">
          {Object.entries(KIND_LABEL).map(([k, l]) => (
            <option key={k} value={k}>
              {l}
            </option>
          ))}
        </select>
        <label className="cents-input">
          <input type="number" min={1} max={99} step={0.5} value={value} onChange={(e) => setValue(e.target.value)} aria-label="Threshold in cents" />¢
        </label>
        {kind === "move" && (
          <select value={windowMin} onChange={(e) => setWindowMin(e.target.value)} aria-label="Time window">
            <option value="15">in 15 min</option>
            <option value="60">in 1 hour</option>
            <option value="240">in 4 hours</option>
            <option value="1440">in 24 hours</option>
          </select>
        )}
        <button className="btn primary" onClick={add}>
          Add alert
        </button>
      </div>
      {err && <div className="error">{err}</div>}
    </section>
  );
}
