export type Market = {
  id: string;
  venue: string;
  venue_market_id: string;
  event_title: string | null;
  title: string;
  outcome_label: string | null;
  url: string | null;
  category: string;
  tags: string[];
  rules_text?: string | null;
  resolution_source: string | null;
  close_ts: string | null;
  status: string;
  volume_unit: "usd" | "contracts";
  total_volume: number | null;
  yes_bid: number | null;
  yes_ask: number | null;
  last_price: number | null;
  mid: number | null;
  spread: number | null;
  change_24h: number | null;
  volume_24h: number | null;
  volume_24h_usd: number | null;
  open_interest: number | null;
  liquidity_usd: number | null;
  quote_ts: string | null;
  watched?: boolean;
  sparkline?: number[];
  alert_rules?: AlertRule[];
  added_price?: number | null;
  added_at?: string;
  notes?: string | null;
};

export type AlertKind = "price_above" | "price_below" | "move";

export type AlertRule = {
  id: number;
  market_id: string;
  kind: AlertKind;
  threshold: number;
  window_minutes: number;
  cooldown_minutes: number;
  enabled: boolean;
  last_fired_at: string | null;
};

export type Venue = { id: string; name: string; status: string; last_ok_at: string | null; last_error: string | null; open_markets: number };

export type Meta = {
  mode: string;
  read_only: boolean;
  categories: string[];
  category_counts: Record<string, number>;
  venues: Venue[];
  sorts: string[];
};

export type Notification = { id: number; ts: string; market_id: string | null; market_title?: string; kind: string; message: string; read: boolean };

export type HistoryPoint = { t: string; open: number; high: number; low: number; close: number };

export type QuoteUpdate = Pick<Market, "id" | "yes_bid" | "yes_ask" | "last_price" | "mid" | "spread" | "change_24h"> & { ts: string };

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...init });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`${res.status}: ${body.slice(0, 200)}`);
  }
  return res.json() as Promise<T>;
}

const enc = encodeURIComponent;

export const api = {
  meta: (contested = false) => req<Meta>(`/api/meta?contested=${contested}`),
  markets: (p: { category?: string; venue?: string; q?: string; sort?: string; limit?: number; offset?: number; contested?: boolean }) => {
    const qs = new URLSearchParams();
    Object.entries(p).forEach(([k, v]) => v !== undefined && v !== "" && qs.set(k, String(v)));
    return req<{ total: number; items: Market[] }>(`/api/markets?${qs}`);
  },
  market: (id: string) => req<Market>(`/api/markets/${enc(id)}`),
  history: (id: string, range: string) =>
    req<{ points: HistoryPoint[]; backfill_requested: boolean }>(`/api/markets/${enc(id)}/history?range=${range}`),
  watchlist: () => req<{ items: Market[] }>("/api/watchlist"),
  watch: (id: string, notes?: string) => req(`/api/watchlist/${enc(id)}`, { method: "PUT", body: JSON.stringify({ notes: notes ?? null }) }),
  unwatch: (id: string) => req(`/api/watchlist/${enc(id)}`, { method: "DELETE" }),
  createAlert: (body: { market_id: string; kind: AlertKind; threshold: number; window_minutes?: number; cooldown_minutes?: number }) =>
    req<AlertRule>("/api/alerts", { method: "POST", body: JSON.stringify(body) }),
  deleteAlert: (id: number) => req(`/api/alerts/${id}`, { method: "DELETE" }),
  notifications: () => req<{ items: Notification[]; unread: number }>("/api/notifications"),
  readNotifications: () => req("/api/notifications/read", { method: "POST" }),
};

export type LinkSide = Pick<
  Market,
  "id" | "venue" | "title" | "outcome_label" | "event_title" | "category" | "url" | "close_ts" | "yes_bid" | "yes_ask" | "mid" | "quote_ts" | "resolution_source" | "volume_24h_usd"
> & { rules_text?: string | null };

export type Link = {
  id: number;
  status: "suggested" | "confirmed" | "rejected";
  relation: "identical" | "inverse";
  score: number;
  reasons: string[];
  rules_changed: boolean;
  llm_relation: string | null;
  llm_confidence: number | null;
  llm_differences: string[] | null;
  note: string | null;
  a: LinkSide;
  b: LinkSide;
};

export type ArbLeg = { venue: string; market_id: string; side: "YES" | "NO"; price: number; fee: number };

export type ArbItem = {
  link_id: number;
  direction: string;
  legs: ArbLeg[];
  cost: number;
  fees: number;
  edge: number;
  annualized: number | null;
  days_to_close: number | null;
  first_seen: string | null;
  link: { id: number; status: string; relation: string; score: number; rules_changed: boolean };
  a: LinkSide;
  b: LinkSide;
};

export type NewsMarket = { id: string; title: string; outcome_label: string | null; venue: string; score: number; terms: string[] };
export type NewsItem = { id: number; url: string; source: string; title: string; summary: string | null; published_at: string; markets: NewsMarket[] };

export const newsApi = {
  list: (p: { scope?: "watchlist" | "matched" | "all"; market_id?: string; limit?: number }) => {
    const qs = new URLSearchParams();
    Object.entries(p).forEach(([k, v]) => v !== undefined && qs.set(k, String(v)));
    return req<{ items: NewsItem[] }>(`/api/news?${qs}`);
  },
};

export const ago = (iso: string) => {
  const m = (Date.now() - new Date(iso).getTime()) / 60000;
  if (m < 1) return "now";
  if (m < 60) return `${Math.round(m)}m`;
  if (m < 48 * 60) return `${Math.round(m / 60)}h`;
  return `${Math.round(m / 1440)}d`;
};

export type Analysis = {
  id: number;
  market_id: string;
  created_at: string;
  status: "ok" | "failed";
  recommendation: "BUY_YES" | "BUY_NO" | "AVOID" | null;
  fair_value: number | null;
  confidence: number | null;
  risk_level: "LOW" | "MEDIUM" | "HIGH" | null;
  net_edge: number | null;
  entry_price: number | null;
  opportunity_score: number | null;
  edge_type: string | null;
  reasoning: string | null;
  edge_summary: string | null;
  key_risks: string[];
  catalysts: { event: string; date: string }[];
  samples: { fair_value: number; confidence: number }[];
  inputs: { news_ids?: number[]; pre_score?: number };
  model: string | null;
  error: string | null;
  title: string;
  outcome_label: string | null;
  venue: string;
  category: string;
  url: string | null;
  close_ts: string | null;
  yes_bid: number | null;
  yes_ask: number | null;
  mid: number | null;
};

export type AiBatch = { id: number; created_at: string; finished_at: string | null; kind: string; status: "queued" | "running" | "done" | "failed"; market_ids: string[]; done: number; error: string | null };
export type AiStatus = { configured: boolean; providers: { name: string; model: string }[]; samples: number; budget_per_day: number };

export const aiApi = {
  picks: () => req<AiStatus & { batch: AiBatch | null; items: Analysis[] }>("/api/ai/picks"),
  run: (force = false) => req<{ batch: AiBatch; started: boolean }>(`/api/ai/picks/run?force=${force}`, { method: "POST" }),
  analyze: (id: string) => req<{ batch: AiBatch }>(`/api/ai/analyze/${enc(id)}`, { method: "POST" }),
  analysis: (id: string, batchId?: number) =>
    req<AiStatus & { analysis: Analysis | null; batch: AiBatch | null }>(`/api/ai/analysis/${enc(id)}${batchId ? `?batch_id=${batchId}` : ""}`),
};

export const arbApi = {
  links: (p: { status?: string; q?: string; limit?: number; offset?: number }) => {
    const qs = new URLSearchParams();
    Object.entries(p).forEach(([k, v]) => v !== undefined && v !== "" && qs.set(k, String(v)));
    return req<{ items: Link[]; total: number; counts: Record<string, number> }>(`/api/links?${qs}`);
  },
  review: (id: number, body: { status: string; relation?: string; note?: string }) =>
    req(`/api/links/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  opportunities: (p: { include_unconfirmed: boolean; min_edge: number; sort: string }) =>
    req<{ items: ArbItem[]; links_considered: number; links_with_stale_quotes: number; max_quote_age_s: number }>(
      `/api/arb?include_unconfirmed=${p.include_unconfirmed}&min_edge=${p.min_edge}&sort=${p.sort}`,
    ),
};

// ---- formatting

/** Price in cents. Below 1¢ / above 99¢ always show a decimal so 0.4¢ isn't rounded to "0¢". */
export const cents = (p: number | null | undefined, digits = 1) => {
  if (p === null || p === undefined) return "—";
  const c = p * 100;
  const d = digits === 0 && c > 0 && c < 100 && (c < 1 || c > 99 || Math.abs(c - Math.round(c)) >= 0.05) ? 1 : digits;
  return `${c.toFixed(d)}¢`;
};

export const signedCents = (d: number | null | undefined) => {
  if (d === null || d === undefined || Math.abs(d) < 0.0005) return "0.0¢";
  return `${d > 0 ? "+" : "−"}${Math.abs(d * 100).toFixed(1)}¢`;
};

export const compact = (n: number | null | undefined, prefix = "") => {
  if (n === null || n === undefined) return "—";
  const a = Math.abs(n);
  if (a >= 1e9) return `${prefix}${(n / 1e9).toFixed(1)}B`;
  if (a >= 1e6) return `${prefix}${(n / 1e6).toFixed(1)}M`;
  if (a >= 1e3) return `${prefix}${(n / 1e3).toFixed(1)}K`;
  return `${prefix}${n.toFixed(0)}`;
};

export const closesIn = (iso: string | null) => {
  if (!iso) return "—";
  const ms = new Date(iso).getTime() - Date.now();
  if (ms < 0) return "closed";
  const h = ms / 36e5;
  if (h < 1) return `${Math.round(ms / 6e4)}m`;
  if (h < 48) return `${Math.round(h)}h`;
  const d = h / 24;
  return d < 60 ? `${Math.round(d)}d` : `${Math.round(d / 30)}mo`;
};

/** Footer label: "Closes 3h", or "Past end date" for markets still trading after their scheduled end. */
export const closeLabel = (iso: string | null) => {
  const c = closesIn(iso);
  return c === "closed" ? "Past end date" : `Closes ${c}`;
};

/** Hourly closes when we have them; otherwise a two-point line from the venue's 24h change. */
export const sparkValues = (m: { sparkline?: number[]; mid: number | null; change_24h: number | null }) => {
  if (m.sparkline && m.sparkline.length >= 2) return m.sparkline;
  if (m.mid !== null && m.change_24h !== null) return [Math.min(1, Math.max(0, m.mid - m.change_24h)), m.mid];
  return [];
};

export const venueLabel: Record<string, string> = { kalshi: "Kalshi", polymarket: "Polymarket", polymarket_us: "Poly US", demo: "Demo" };
