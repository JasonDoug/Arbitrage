import { useCallback, useEffect, useState } from "react";
import { activeAccount, cents, paperApi, usd, venueLabel, type PaperAccount, type Portfolio } from "../api";
import { EquityChart } from "../components/EquityChart";

export function PortfolioPage({ onOpenMarket, refreshKey }: { onOpenMarket: (id: string) => void; refreshKey: number }) {
  const [accounts, setAccounts] = useState<PaperAccount[]>([]);
  const [account, setAccount] = useState<number | null>(activeAccount.get());
  const [mark, setMark] = useState<"mid" | "exit">("mid");
  const [data, setData] = useState<Portfolio | null>(null);
  const [tab, setTab] = useState<"positions" | "orders" | "fills">("positions");
  const [error, setError] = useState<string | null>(null);

  const loadAccounts = useCallback(
    () =>
      paperApi.accounts().then((r) => {
        setAccounts(r.items);
        setAccount((cur) => (r.items.some((a) => a.id === cur) ? cur : (r.items[0]?.id ?? null)));
      }),
    [],
  );
  useEffect(() => {
    loadAccounts();
  }, [loadAccounts]);

  const load = useCallback(() => {
    if (account === null) return;
    paperApi
      .portfolio(account, mark)
      .then((d) => {
        setData(d);
        setError(null);
      })
      .catch((e) => setError(String(e)));
  }, [account, mark]);

  useEffect(() => {
    load();
    const t = window.setInterval(load, 15000);
    return () => window.clearInterval(t);
  }, [load, refreshKey]);

  const choose = (id: number) => {
    activeAccount.set(id);
    setAccount(id);
  };

  const newAccount = async () => {
    const name = prompt("Name for the new paper account (e.g. 'Arb only')");
    if (!name) return;
    const cash = Number(prompt("Starting cash ($)", "10000") || 10000);
    const a = await paperApi.createAccount(name, cash);
    await loadAccounts();
    choose(a.id);
  };

  const reset = async () => {
    if (account === null || !confirm("Reset this paper account? All its orders, fills and history are deleted.")) return;
    await paperApi.reset(account);
    load();
    loadAccounts();
  };

  const s = data?.summary;
  const pnlCls = (v: number | null | undefined) => (v === null || v === undefined || Math.abs(v) < 0.005 ? "" : v > 0 ? "pos" : "neg");
  return (
    <div className="page">
      <div className="page-head">
        <h1 className="page-title">
          Portfolio <span className="paper-tag big">PAPER</span>
        </h1>
        <div className="head-actions">
          <select value={account ?? ""} onChange={(e) => choose(Number(e.target.value))} aria-label="Paper account">
            {accounts.map((a) => (
              <option key={a.id} value={a.id}>
                {a.name} · {usd(a.equity, 0)}
              </option>
            ))}
          </select>
          <button className="btn" onClick={newAccount}>
            New account
          </button>
          <button className="btn" onClick={reset}>
            Reset
          </button>
        </div>
      </div>
      <p className="hint">
        Fake money on real markets. Market orders walk the venue's live order book and pay its taker fee; limit orders rest and fill (fee-free) when a later
        quote reaches your price. Positions settle at $1 or $0 when the venue resolves the market.
      </p>
      {error && <div className="error">{error}</div>}
      {s && data && (
        <>
          <div className="summary-bar">
            <Metric label="Equity" value={usd(s.equity)} />
            <Metric label="Total P&L" value={`${usd(s.total_pnl)} (${s.return_pct?.toFixed(2)}%)`} cls={pnlCls(s.total_pnl)} />
            <Metric label="Unrealized" value={usd(s.unrealized)} cls={pnlCls(s.unrealized)} />
            <Metric label="Realized" value={usd(s.realized)} cls={pnlCls(s.realized)} />
            <Metric label="Cash" value={usd(s.cash)} sub={s.reserved_cash ? `${usd(s.reserved_cash)} reserved for limits` : undefined} />
            <Metric label="Fees paid" value={usd(s.fees)} />
            <Metric label="Win rate" value={s.win_rate === null ? "—" : `${s.win_rate}%`} sub={`${s.closed_positions} closed`} />
            <Metric label="Max drawdown" value={data.max_drawdown_pct === null ? "—" : `${data.max_drawdown_pct}%`} cls={pnlCls(data.max_drawdown_pct)} />
          </div>
          <section className="panel">
            <div className="panel-head">
              <h3>Equity</h3>
              <div className="seg" title="How open positions are valued">
                <button className={mark === "mid" ? "on" : ""} onClick={() => setMark("mid")}>
                  mark at mid
                </button>
                <button className={mark === "exit" ? "on" : ""} onClick={() => setMark("exit")}>
                  mark at exit bid
                </button>
              </div>
            </div>
            {data.equity_curve.length > 1 ? (
              <EquityChart points={data.equity_curve} start={s.starting_cash} />
            ) : (
              <div className="chart-empty">The equity curve fills in as the worker takes snapshots (every 5 minutes).</div>
            )}
          </section>
          <div className="seg big tabs-row" role="tablist">
            {(["positions", "orders", "fills"] as const).map((t) => (
              <button key={t} role="tab" aria-selected={tab === t} className={tab === t ? "on" : ""} onClick={() => setTab(t)}>
                {t === "positions" ? `Positions (${s.positions.length})` : t === "orders" ? `Open orders (${data.open_orders.length})` : "History"}
              </button>
            ))}
          </div>
          {tab === "positions" && (
            <Table
              empty="No open positions. Open any market and use the paper ticket in its detail panel."
              head={["Market", "Side", "Qty", "Avg cost", "Mark", "Value", "Unrealized", "Realized", "Ends"]}
              rows={s.positions.map((p) => [
                <MarketCell key="m" title={p.title} outcome={p.outcome_label} venue={p.venue} onClick={() => onOpenMarket(p.market_id)} result={p.result} />,
                <b key="s" className={p.side === "YES" ? "pos" : "neg"}>{p.side}</b>,
                p.qty.toLocaleString(),
                cents(p.avg_cost),
                cents(p.mark),
                usd(p.value),
                <span key="u" className={`chg ${pnlCls(p.unrealized)}`}>{usd(p.unrealized)}</span>,
                <span key="r" className={`chg ${pnlCls(p.realized)}`}>{usd(p.realized)}</span>,
                p.close_ts ? new Date(p.close_ts).toLocaleDateString() : "—",
              ])}
            />
          )}
          {tab === "orders" && (
            <Table
              empty="No resting limit orders."
              head={["Market", "Order", "Limit", "Filled", "Placed", ""]}
              rows={data.open_orders.map((o) => [
                <MarketCell key="m" title={o.title} outcome={o.outcome_label} venue={o.venue} onClick={() => onOpenMarket(o.market_id)} />,
                `${o.action} ${o.qty.toLocaleString()} ${o.side}`,
                cents(o.limit_price),
                `${o.filled_qty.toLocaleString()} / ${o.qty.toLocaleString()}`,
                new Date(o.created_at).toLocaleString(),
                <button key="c" className="link" onClick={() => paperApi.cancel(data.account.id, o.id).then(load)}>
                  cancel
                </button>,
              ])}
            />
          )}
          {tab === "fills" && (
            <Table
              empty="No fills yet."
              head={["Time", "Market", "Fill", "Price", "Fee", "Type"]}
              rows={data.fills.map((f) => [
                new Date(f.ts).toLocaleString(),
                <MarketCell key="m" title={f.title} outcome={f.outcome_label} venue={f.venue} onClick={() => onOpenMarket(f.market_id)} />,
                `${f.action} ${f.qty.toLocaleString()} ${f.side}`,
                cents(f.price),
                usd(f.fee),
                f.group_id ? `${f.liquidity} · arb` : f.liquidity,
              ])}
            />
          )}
        </>
      )}
    </div>
  );
}

function Metric({ label, value, cls = "", sub }: { label: string; value: string; cls?: string; sub?: string }) {
  return (
    <div className="metric">
      <dt>{label}</dt>
      <dd className={`mono chg ${cls}`}>{value}</dd>
      {sub && <small className="muted">{sub}</small>}
    </div>
  );
}

function MarketCell({ title, outcome, venue, onClick, result }: { title: string; outcome: string | null; venue: string; onClick: () => void; result?: string | null }) {
  return (
    <button className="side-line" onClick={onClick}>
      <span className={`venue venue-${venue}`}>{venueLabel[venue] ?? venue}</span>
      <span className="side-title">
        {title}
        {outcome && <em> — {outcome}</em>}
        {result && <span className="badge-warn"> resolved {result.toUpperCase()}</span>}
      </span>
    </button>
  );
}

function Table({ head, rows, empty }: { head: string[]; rows: React.ReactNode[][]; empty: string }) {
  if (!rows.length) return <div className="empty">{empty}</div>;
  return (
    <div className="table-wrap">
      <table className="watch-table">
        <thead>
          <tr>
            {head.map((h, i) => (
              <th key={i} className={i > 1 ? "num" : ""}>
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i}>
              {r.map((c, j) => (
                <td key={j} className={j > 1 ? "num mono" : ""}>
                  {c}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
