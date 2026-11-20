import { useEffect, useMemo, useState } from "react";
import { activeAccount, cents, paperApi, usd, type Book, type Market, type OrderIn, type OrderPlan, type PaperAccount } from "../api";

/** Order book (YES view) plus a paper-trading ticket. */
export function TradePanel({ market, onFilled }: { market: Market; onFilled: () => void }) {
  const [book, setBook] = useState<Book | null>(null);
  const [accounts, setAccounts] = useState<PaperAccount[]>([]);
  const [account, setAccount] = useState<number | null>(activeAccount.get());

  useEffect(() => {
    let alive = true;
    const load = () => paperApi.book(market.id).then((b) => alive && setBook(b)).catch(() => undefined);
    load();
    const t = window.setInterval(load, 5000);
    return () => {
      alive = false;
      window.clearInterval(t);
    };
  }, [market.id]);

  useEffect(() => {
    paperApi.accounts().then((r) => {
      setAccounts(r.items);
      if (!r.items.some((a) => a.id === account) && r.items[0]) setAccount(r.items[0].id);
    });
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <section className="panel paper-panel">
      <div className="panel-head">
        <h3>
          Order book & paper trade <span className="paper-tag">PAPER</span>
        </h3>
        {book && book.source !== "book" && (
          <span className="muted small" title="The venue's full book wasn't available, so only the best bid/ask and their sizes are used.">
            top of book only
          </span>
        )}
      </div>
      <div className="trade-grid">
        <BookView book={book} />
        {account !== null ? (
          <Ticket market={market} book={book} accountId={account} accounts={accounts} setAccount={(id) => (activeAccount.set(id), setAccount(id))} onFilled={onFilled} />
        ) : (
          <div className="muted">Loading account…</div>
        )}
      </div>
    </section>
  );
}

function BookView({ book }: { book: Book | null }) {
  if (!book) return <div className="muted">Loading book…</div>;
  const asks = [...book.yes_asks.slice(0, 8)].reverse();
  const bids = book.yes_bids.slice(0, 8);
  const max = Math.max(1, ...asks.map((l) => l[1]), ...bids.map((l) => l[1]));
  if (!asks.length && !bids.length) return <div className="muted">Empty book.</div>;
  return (
    <div className="book">
      <div className="book-head mono">
        <span>YES price</span>
        <span>Contracts</span>
      </div>
      {asks.map(([p, s]) => (
        <div key={`a${p}`} className="book-row ask mono">
          <i style={{ width: `${(s / max) * 100}%` }} />
          <span>{cents(p)}</span>
          <span>{s.toLocaleString(undefined, { maximumFractionDigits: 0 })}</span>
        </div>
      ))}
      <div className="book-spread mono">
        spread {asks.length && bids.length ? cents(book.yes_asks[0][0] - book.yes_bids[0][0]) : "—"}
      </div>
      {bids.map(([p, s]) => (
        <div key={`b${p}`} className="book-row bid mono">
          <i style={{ width: `${(s / max) * 100}%` }} />
          <span>{cents(p)}</span>
          <span>{s.toLocaleString(undefined, { maximumFractionDigits: 0 })}</span>
        </div>
      ))}
    </div>
  );
}

function Ticket({
  market,
  book,
  accountId,
  accounts,
  setAccount,
  onFilled,
}: {
  market: Market;
  book: Book | null;
  accountId: number;
  accounts: PaperAccount[];
  setAccount: (id: number) => void;
  onFilled: () => void;
}) {
  const [side, setSide] = useState<"YES" | "NO">("YES");
  const [action, setAction] = useState<"buy" | "sell">("buy");
  const [type, setType] = useState<"market" | "limit">("market");
  const [qty, setQty] = useState("100");
  const [limit, setLimit] = useState("");
  const [plan, setPlan] = useState<OrderPlan | null>(null);
  const [result, setResult] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const order: OrderIn | null = useMemo(() => {
    const q = Number(qty);
    if (!(q > 0)) return null;
    const lp = type === "limit" ? Number(limit) / 100 : null;
    if (type === "limit" && !(lp && lp > 0 && lp < 1)) return null;
    return { account_id: accountId, market_id: market.id, side, action, type, qty: q, limit_price: lp };
  }, [accountId, market.id, side, action, type, qty, limit]);

  useEffect(() => {
    setResult(null);
    if (!order) return setPlan(null);
    const t = window.setTimeout(() => paperApi.preview(order).then(setPlan).catch((e) => setPlan({ ok: false, error: String(e) } as OrderPlan)), 250);
    return () => window.clearTimeout(t);
  }, [order, book]);

  // Prefill the limit price with the current best price for the chosen side.
  useEffect(() => {
    if (type !== "limit" || limit || !book) return;
    const ladder = action === "buy" ? (side === "YES" ? book.yes_asks : book.yes_bids.map(([p, s]) => [1 - p, s])) : side === "YES" ? book.yes_bids : book.yes_asks.map(([p, s]) => [1 - p, s]);
    if (ladder[0]) setLimit(((ladder[0][0] as number) * 100).toFixed(1));
  }, [type, side, action, book, limit]);

  const submit = async () => {
    if (!order) return;
    setBusy(true);
    try {
      const r = await paperApi.place(order);
      setResult(
        r.status === "open"
          ? `Resting: ${r.rest_qty.toLocaleString()} @ ${cents(r.limit_used)}${r.fill_qty ? ` (${r.fill_qty} filled now)` : ""}`
          : `${r.status === "filled" ? "Filled" : "Partly filled"} ${r.fill_qty.toLocaleString()} @ ${cents(r.avg_price)} · fee ${usd(r.fee)}`,
      );
      onFilled();
    } catch (e) {
      setResult(String(e).replace(/^Error: \d+: /, ""));
    } finally {
      setBusy(false);
    }
  };

  const ok = plan && plan.ok !== false;
  return (
    <div className="ticket">
      <div className="ticket-row">
        <div className="seg">
          {(["buy", "sell"] as const).map((a) => (
            <button key={a} className={action === a ? "on" : ""} onClick={() => setAction(a)}>
              {a === "buy" ? "Buy" : "Sell"}
            </button>
          ))}
        </div>
        <div className="seg">
          {(["YES", "NO"] as const).map((s) => (
            <button key={s} className={side === s ? `on ${s === "YES" ? "yes" : "no"}` : ""} onClick={() => setSide(s)}>
              {s}
            </button>
          ))}
        </div>
        <div className="seg">
          {(["market", "limit"] as const).map((t) => (
            <button key={t} className={type === t ? "on" : ""} onClick={() => (setType(t), setLimit(""))}>
              {t}
            </button>
          ))}
        </div>
      </div>
      <div className="ticket-row">
        <label className="cents-input wide">
          qty <input type="number" min={1} step={1} value={qty} onChange={(e) => setQty(e.target.value)} aria-label="Contracts" />
        </label>
        {type === "limit" && (
          <label className="cents-input wide">
            limit <input type="number" min={0.1} max={99.9} step={0.1} value={limit} onChange={(e) => setLimit(e.target.value)} aria-label="Limit price in cents" />¢
          </label>
        )}
        <select value={accountId} onChange={(e) => setAccount(Number(e.target.value))} aria-label="Paper account">
          {accounts.map((a) => (
            <option key={a.id} value={a.id}>
              {a.name}
            </option>
          ))}
        </select>
      </div>
      <dl className="preview mono">
        {plan?.ok === false ? (
          <div className="error">{plan.error}</div>
        ) : ok ? (
          <>
            <div><dt>Fills now</dt><dd>{plan.fill_qty.toLocaleString()} @ {plan.avg_price ? cents(plan.avg_price) : "—"}</dd></div>
            <div><dt>Fee</dt><dd>{usd(plan.fee)}</dd></div>
            {action === "buy" ? (
              <>
                <div><dt>Max loss</dt><dd>{usd(plan.max_loss)}</dd></div>
                <div><dt>Payout if {side}</dt><dd>{usd(plan.max_payout)}</dd></div>
              </>
            ) : (
              <div><dt>Proceeds</dt><dd>{usd(plan.proceeds)}</dd></div>
            )}
            {plan.rest_qty > 0 && <div><dt>Rests</dt><dd>{plan.rest_qty.toLocaleString()} @ {cents(plan.limit_used)}</dd></div>}
            {plan.unfilled_cancelled > 0 && <div className="warn"><dt>Not fillable</dt><dd>{plan.unfilled_cancelled.toLocaleString()} (cancelled)</dd></div>}
            <div><dt>Position after</dt><dd>{plan.position_after.toLocaleString()} {side}</dd></div>
          </>
        ) : (
          <div className="muted">Enter a quantity{type === "limit" ? " and limit price" : ""}.</div>
        )}
      </dl>
      <button className={`btn primary ${side === "NO" ? "danger" : ""}`} disabled={!ok || busy} onClick={submit}>
        Paper {action} {side}
      </button>
      {result && <div className="ticket-result">{result}</div>}
    </div>
  );
}
