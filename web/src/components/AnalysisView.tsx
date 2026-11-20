import { cents, type Analysis } from "../api";

const REC: Record<string, { label: string; cls: string }> = {
  BUY_YES: { label: "Buy YES", cls: "rec-yes" },
  BUY_NO: { label: "Buy NO", cls: "rec-no" },
  AVOID: { label: "Avoid", cls: "rec-avoid" },
};

const EDGE_LABEL: Record<string, string> = {
  news_lag: "News lag",
  base_rate: "Base rate",
  cross_venue: "Cross-venue",
  rules_misread: "Rules misread",
  momentum: "Momentum",
  none: "No clear edge",
};

export function RecBadge({ rec }: { rec: Analysis["recommendation"] }) {
  const r = REC[rec ?? "AVOID"];
  return <span className={`rec ${r.cls}`}>{r.label}</span>;
}

export function ConfidenceBar({ value }: { value: number }) {
  const v = Math.max(0, Math.min(100, value));
  return (
    <div className="conf" title={`Confidence ${v.toFixed(0)} / 100 (lowered when samples disagree)`}>
      <div className="conf-track">
        <div className={`conf-fill ${v >= 60 ? "ok" : "low"}`} style={{ width: `${v}%` }} />
        <div className="conf-mark" style={{ left: "60%" }} />
      </div>
      <span className="mono">{v.toFixed(0)}</span>
    </div>
  );
}

export function RiskChip({ risk }: { risk: Analysis["risk_level"] }) {
  const r = risk ?? "MEDIUM";
  return <span className={`risk risk-${r.toLowerCase()}`}>{r === "MEDIUM" ? "Medium" : r === "LOW" ? "Low" : "High"} risk</span>;
}

/** Body shared by the AI Picks cards and the market drawer. */
export function AnalysisBody({ a }: { a: Analysis }) {
  if (a.status === "failed") return <div className="error">Analysis failed: {a.error}</div>;
  const side = a.recommendation === "BUY_NO" ? "NO" : "YES";
  const fairSide = a.fair_value === null ? null : side === "NO" ? 1 - a.fair_value : a.fair_value;
  return (
    <div className="analysis">
      <div className="analysis-metrics">
        <div>
          <dt>AI fair value (YES)</dt>
          <dd className="mono">{cents(a.fair_value)}</dd>
        </div>
        <div>
          <dt>{a.recommendation === "AVOID" ? "Best side" : `${side} ask`}</dt>
          <dd className="mono">
            {cents(a.entry_price)}
            {fairSide !== null && a.recommendation !== "AVOID" && <span className="muted"> vs {cents(fairSide)} fair</span>}
          </dd>
        </div>
        <div>
          <dt>Net edge after fees</dt>
          <dd className={`mono chg ${a.net_edge !== null && a.net_edge > 0 ? "pos" : "neg"}`}>{cents(a.net_edge, 1)}</dd>
        </div>
        <div>
          <dt>Edge type</dt>
          <dd>{EDGE_LABEL[a.edge_type ?? "none"] ?? a.edge_type}</dd>
        </div>
      </div>
      <div className="analysis-row">
        <ConfidenceBar value={a.confidence ?? 0} />
        <RiskChip risk={a.risk_level} />
      </div>
      {a.edge_summary && <p className="edge-summary">{a.edge_summary}</p>}
      {a.reasoning && <p className="reasoning">{a.reasoning}</p>}
      {a.key_risks.length > 0 && (
        <div className="analysis-list">
          <h4>Key risks</h4>
          <ul>
            {a.key_risks.map((r) => (
              <li key={r}>{r}</li>
            ))}
          </ul>
        </div>
      )}
      {a.catalysts.length > 0 && (
        <div className="analysis-list">
          <h4>Catalysts</h4>
          <ul>
            {a.catalysts.map((c) => (
              <li key={c.event}>
                {c.date && <span className="mono muted">{c.date} </span>}
                {c.event}
              </li>
            ))}
          </ul>
        </div>
      )}
      <footer className="analysis-foot muted">
        {new Date(a.created_at).toLocaleString()} · {a.model ?? "unknown model"} · {a.samples.length} sample{a.samples.length === 1 ? "" : "s"}
        {a.samples.length > 1 && ` (fair ${a.samples.map((s) => (s.fair_value * 100).toFixed(0)).join(" / ")}¢)`}
      </footer>
    </div>
  );
}
