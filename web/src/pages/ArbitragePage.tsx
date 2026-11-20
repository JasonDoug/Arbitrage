import { useCallback, useEffect, useState } from "react";
import { arbApi, cents, closesIn, venueLabel, type ArbItem, type Link, type LinkSide } from "../api";

type Tab = "opportunities" | "review";

export function ArbitragePage({ onOpenMarket }: { onOpenMarket: (id: string) => void }) {
  const [tab, setTab] = useState<Tab>("opportunities");
  const [counts, setCounts] = useState<Record<string, number>>({});
  return (
    <div className="page">
      <div className="page-head">
        <h1 className="page-title">Arbitrage</h1>
        <div className="seg big" role="tablist">
          <button role="tab" aria-selected={tab === "opportunities"} className={tab === "opportunities" ? "on" : ""} onClick={() => setTab("opportunities")}>
            Opportunities
          </button>
          <button role="tab" aria-selected={tab === "review"} className={tab === "review" ? "on" : ""} onClick={() => setTab("review")}>
            Match review {counts.suggested ? <span className="pill">{counts.suggested}</span> : null}
          </button>
        </div>
      </div>
      {tab === "opportunities" ? (
        <Opportunities onOpenMarket={onOpenMarket} onReview={() => setTab("review")} counts={counts} setCounts={setCounts} />
      ) : (
        <Review onOpenMarket={onOpenMarket} setCounts={setCounts} />
      )}
    </div>
  );
}

// ---------------------------------------------------------------- opportunities

function Opportunities({
  onOpenMarket,
  onReview,
  counts,
  setCounts,
}: {
  onOpenMarket: (id: string) => void;
  onReview: () => void;
  counts: Record<string, number>;
  setCounts: (c: Record<string, number>) => void;
}) {
  const [includeUnconfirmed, setIncludeUnconfirmed] = useState(false);
  const [minEdge, setMinEdge] = useState("0");
  const [sort, setSort] = useState("edge");
  const [data, setData] = useState<Awaited<ReturnType<typeof arbApi.opportunities>> | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    arbApi
      .opportunities({ include_unconfirmed: includeUnconfirmed, min_edge: Number(minEdge) / 100, sort })
      .then((d) => {
        setData(d);
        setError(null);
      })
      .catch((e) => setError(String(e)));
    arbApi.links({ limit: 1 }).then((r) => setCounts(r.counts)).catch(() => undefined);
  }, [includeUnconfirmed, minEdge, sort, setCounts]);

  useEffect(() => {
    load();
    const t = window.setInterval(load, 10000);
    return () => window.clearInterval(t);
  }, [load]);

  return (
    <>
      <div className="filters">
        <label className="toggle" title="Also show pairs nobody has confirmed yet. Their edge may not be real.">
          <input type="checkbox" checked={includeUnconfirmed} onChange={(e) => setIncludeUnconfirmed(e.target.checked)} />
          Include unconfirmed matches
        </label>
        <label className="cents-input" title="Minimum net edge per contract, after fees">
          min edge
          <input type="number" step={0.5} value={minEdge} onChange={(e) => setMinEdge(e.target.value)} aria-label="Minimum edge in cents" />¢
        </label>
        <select value={sort} onChange={(e) => setSort(e.target.value)} aria-label="Sort by">
          <option value="edge">Net edge</option>
          <option value="annualized">Annualized return</option>
        </select>
      </div>
      <p className="hint">
        Edge = $1 payout − both asks − both venues' taker fees, per contract, at top of book (order-book depth is not checked yet). Refreshes every 10 s.
        {data && ` ${data.links_considered} linked pairs considered; ${data.links_with_stale_quotes} skipped for quotes older than ${data.max_quote_age_s} s.`}
      </p>
      {error && <div className="error">{error}</div>}
      {data && data.links_considered === 0 && (
        <div className="empty">
          No confirmed cross-venue pairs yet.{" "}
          {counts.suggested ? (
            <button className="link-accent" onClick={onReview}>
              Review {counts.suggested} suggested matches →
            </button>
          ) : (
            "The matcher runs after both catalogs load, then every 30 minutes."
          )}
        </div>
      )}
      {data && data.links_considered > 0 && data.items.length === 0 && <div className="empty">No pairs above the minimum edge right now.</div>}
      {data && data.items.length > 0 && (
        <div className="table-wrap">
          <table className="watch-table arb-table">
            <thead>
              <tr>
                <th>Pair</th>
                <th>Trade</th>
                <th className="num">Cost</th>
                <th className="num">Fees</th>
                <th className="num">Net edge</th>
                <th className="num">Annualized</th>
                <th className="num">Closes</th>
                <th className="num">Open for</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((o) => (
                <ArbRow key={`${o.link_id}-${o.direction}`} o={o} onOpenMarket={onOpenMarket} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

function sinceLabel(iso: string | null) {
  if (!iso) return "new";
  const m = (Date.now() - new Date(iso).getTime()) / 60000;
  if (m < 1) return "<1m";
  if (m < 60) return `${Math.round(m)}m`;
  return `${(m / 60).toFixed(1)}h`;
}

function ArbRow({ o, onOpenMarket }: { o: ArbItem; onOpenMarket: (id: string) => void }) {
  const cls = o.edge > 0.0005 ? "pos" : o.edge < -0.0005 ? "neg" : "";
  return (
    <tr>
      <td>
        <SideLine s={o.a} onOpen={onOpenMarket} />
        <SideLine s={o.b} onOpen={onOpenMarket} />
        <div className="wt-sub">
          {o.link.status !== "confirmed" && <span className="badge-warn">unconfirmed</span>}
          {o.link.relation === "inverse" && <span className="badge-warn">inverse</span>}
          {o.link.rules_changed && <span className="badge-warn">rules changed since review</span>}
        </div>
      </td>
      <td className="mono legs">
        {o.legs.map((l) => (
          <div key={l.market_id}>
            Buy <b className={l.side === "YES" ? "pos" : "neg"}>{l.side}</b> {venueLabel[l.venue] ?? l.venue} @ {cents(l.price)}
          </div>
        ))}
      </td>
      <td className="num mono">{cents(o.cost)}</td>
      <td className="num mono">{cents(o.fees, 2)}</td>
      <td className={`num mono chg ${cls}`}>
        <strong>{cents(o.edge, 2)}</strong>
      </td>
      <td className={`num mono chg ${cls}`}>{o.annualized === null ? "—" : `${(o.annualized * 100).toFixed(0)}%`}</td>
      <td className="num mono">{o.days_to_close === null ? "—" : `${Math.round(o.days_to_close)}d`}</td>
      <td className="num mono">{sinceLabel(o.first_seen)}</td>
    </tr>
  );
}

function SideLine({ s, onOpen }: { s: LinkSide; onOpen: (id: string) => void }) {
  return (
    <button className="side-line" onClick={() => onOpen(s.id)} title="Open market">
      <span className={`venue venue-${s.venue}`}>{venueLabel[s.venue] ?? s.venue}</span>
      <span className="side-title">
        {s.title}
        {s.outcome_label && <em> — {s.outcome_label}</em>}
      </span>
    </button>
  );
}

// ---------------------------------------------------------------- match review

function Review({ onOpenMarket, setCounts }: { onOpenMarket: (id: string) => void; setCounts: (c: Record<string, number>) => void }) {
  const [status, setStatus] = useState<"suggested" | "confirmed" | "rejected">("suggested");
  const [q, setQ] = useState("");
  const [data, setData] = useState<{ items: Link[]; total: number; counts: Record<string, number> } | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    arbApi
      .links({ status, q: q.trim(), limit: 50 })
      .then((d) => {
        setData(d);
        setCounts(d.counts);
      })
      .catch((e) => setError(String(e)));
  }, [status, q, setCounts]);

  useEffect(() => {
    const t = window.setTimeout(load, 250);
    return () => window.clearTimeout(t);
  }, [load]);

  const act = async (l: Link, s: string, relation?: string) => {
    await arbApi.review(l.id, { status: s, relation });
    setData((d) => (d ? { ...d, items: d.items.filter((x) => x.id !== l.id), total: d.total - 1 } : d));
    load();
  };

  const counts = data?.counts ?? {};
  return (
    <>
      <div className="filters">
        <div className="seg" role="tablist">
          {(["suggested", "confirmed", "rejected"] as const).map((s) => (
            <button key={s} role="tab" aria-selected={status === s} className={status === s ? "on" : ""} onClick={() => setStatus(s)}>
              {s} <span>{counts[s] ?? 0}</span>
            </button>
          ))}
        </div>
        <input className="search" type="search" placeholder="Filter by title…" value={q} onChange={(e) => setQ(e.target.value)} />
      </div>
      <p className="hint">
        The matcher pairs markets by wording, numbers and end dates. Only you can decide they resolve on the same fact. Check both rule sets and resolution
        sources before confirming. <b>Identical</b>: YES on both pays together. <b>Inverse</b>: YES on Kalshi pays when NO on Polymarket pays.
      </p>
      {error && <div className="error">{error}</div>}
      {data && data.items.length === 0 && <div className="empty">Nothing here.</div>}
      <div className="review-list">
        {data?.items.map((l) => (
          <ReviewCard key={l.id} l={l} onOpenMarket={onOpenMarket} onAct={act} />
        ))}
      </div>
    </>
  );
}

function ReviewCard({ l, onOpenMarket, onAct }: { l: Link; onOpenMarket: (id: string) => void; onAct: (l: Link, s: string, r?: string) => void }) {
  const [open, setOpen] = useState(false);
  return (
    <article className="review-card">
      <header>
        <span className="score" title={l.reasons.join(" · ")}>
          match {(l.score * 100).toFixed(0)}%
        </span>
        <span className="muted reasons">{l.reasons.join(" · ")}</span>
        {l.rules_changed && l.status !== "suggested" && <span className="badge-warn">rules changed since review</span>}
      </header>
      <div className="pair">
        <PairSide s={l.a} open={open} onOpen={onOpenMarket} />
        <PairSide s={l.b} open={open} onOpen={onOpenMarket} />
      </div>
      <footer>
        <button className="link" onClick={() => setOpen((o) => !o)}>
          {open ? "Hide rules" : "Compare rules"}
        </button>
        <div className="actions">
          {l.status !== "confirmed" && (
            <>
              <button className="btn primary" onClick={() => onAct(l, "confirmed", "identical")}>
                Confirm identical
              </button>
              <button className="btn" onClick={() => onAct(l, "confirmed", "inverse")}>
                Confirm inverse
              </button>
            </>
          )}
          {l.status !== "rejected" && (
            <button className="btn" onClick={() => onAct(l, "rejected")}>
              Reject
            </button>
          )}
          {l.status !== "suggested" && (
            <button className="btn" onClick={() => onAct(l, "suggested")}>
              Back to queue
            </button>
          )}
        </div>
      </footer>
    </article>
  );
}

function PairSide({ s, open, onOpen }: { s: LinkSide; open: boolean; onOpen: (id: string) => void }) {
  return (
    <div className="pair-side">
      <div className="drawer-tags">
        <span className={`venue venue-${s.venue}`}>{venueLabel[s.venue] ?? s.venue}</span>
        <span className="cat">{s.category}</span>
      </div>
      <button className="pair-title" onClick={() => onOpen(s.id)}>
        {s.title}
      </button>
      {s.outcome_label && <div className="outcome">{s.outcome_label}</div>}
      <div className="pair-stats mono">
        YES {cents(s.yes_bid)} / {cents(s.yes_ask)} · ends {s.close_ts ? new Date(s.close_ts).toLocaleDateString() : "—"} ({closesIn(s.close_ts)})
      </div>
      {open && (
        <div className="pair-rules">
          {s.resolution_source && (
            <p className="source">
              Source: <strong>{s.resolution_source}</strong>
            </p>
          )}
          <p className="rules">{s.rules_text || "No rules text."}</p>
        </div>
      )}
    </div>
  );
}
