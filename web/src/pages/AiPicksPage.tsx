import { useCallback, useEffect, useState } from "react";
import { aiApi, venueLabel, type AiBatch, type AiStatus, type Analysis } from "../api";
import { AnalysisBody, RecBadge } from "../components/AnalysisView";

export function AiPicksPage({ onOpenMarket }: { onOpenMarket: (id: string) => void }) {
  const [data, setData] = useState<(AiStatus & { batch: AiBatch | null; items: Analysis[] }) | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);

  const load = useCallback(() => {
    aiApi
      .picks()
      .then((d) => {
        setData(d);
        setError(null);
      })
      .catch((e) => setError(String(e)));
  }, []);

  const running = data?.batch?.status === "queued" || data?.batch?.status === "running";
  useEffect(() => {
    load();
  }, [load]);
  useEffect(() => {
    if (!running) return;
    const t = window.setInterval(load, 3000);
    return () => window.clearInterval(t);
  }, [running, load]);

  const run = async (force: boolean) => {
    setStarting(true);
    try {
      await aiApi.run(force);
      load();
    } catch (e) {
      setError(String(e));
    } finally {
      setStarting(false);
    }
  };

  const b = data?.batch;
  return (
    <div className="page">
      <div className="page-head">
        <h1 className="page-title">AI Picks</h1>
        <div className="head-actions">
          {b && (
            <span className="muted mono small">
              {running ? `analyzing ${b.done}/${b.market_ids.length || "…"}` : b.finished_at ? `last run ${new Date(b.finished_at).toLocaleTimeString()}` : ""}
            </span>
          )}
          <button className="btn primary" disabled={!data?.configured || running || starting} onClick={() => run(true)}>
            {running ? "Analyzing…" : "AI Analyze"}
          </button>
        </div>
      </div>
      <div className="disclaimer">
        Model output, not financial advice. The AI estimates a fair probability; Buy/Avoid comes from fixed rules: at least a 5¢ edge after fees and
        confidence ≥ 60 (10¢ for high-risk picks). Free models can be wrong or out of date.
      </div>
      {error && <div className="error">{error}</div>}
      {data && !data.configured && (
        <div className="empty">
          No LLM configured. Add <code>OPENROUTER_API_KEY=…</code> (and optionally <code>LOCAL_LLM_URL</code>) to <code>.env</code>, then{" "}
          <code>docker compose up -d</code>.
        </div>
      )}
      {data?.configured && (
        <p className="hint">
          Uses {data.providers.map((p) => `${p.name} (${p.model})`).join(" → ")}, {data.samples} samples per market. Picks the 8 most liquid, contested
          markets with fresh quotes, then ranks them by Opportunity Score.
        </p>
      )}
      {b?.status === "failed" && <div className="error">Last run failed: {b.error}</div>}
      {data?.configured && !b && <div className="empty">No analysis yet. Press AI Analyze. A run takes a few minutes on free models.</div>}
      <div className="picks-grid">
        {data?.items.map((a, i) => (
          <article key={a.id} className={`pick-card ${a.status === "failed" ? "failed" : ""}`}>
            <header>
              <span className="rank mono">#{i + 1}</span>
              <span className={`venue venue-${a.venue}`}>{venueLabel[a.venue] ?? a.venue}</span>
              <span className="cat">{a.category}</span>
              {a.status === "ok" && <RecBadge rec={a.recommendation} />}
              {a.opportunity_score !== null && (
                <span className="score mono" title="Opportunity Score: edge 40 · confidence 25 · liquidity, spread, timing, news 35">
                  {a.opportunity_score.toFixed(0)}
                </span>
              )}
            </header>
            <button className="pick-title" onClick={() => onOpenMarket(a.market_id)}>
              {a.title}
              {a.outcome_label && <em> — {a.outcome_label}</em>}
            </button>
            <AnalysisBody a={a} />
          </article>
        ))}
        {running &&
          Array.from({ length: Math.max(0, (b?.market_ids.length || 8) - (data?.items.length ?? 0)) }).map((_, i) => (
            <article key={`p${i}`} className="pick-card placeholder">
              <div className="shimmer" />
              <span className="muted">Waiting for the model…</span>
            </article>
          ))}
      </div>
    </div>
  );
}
