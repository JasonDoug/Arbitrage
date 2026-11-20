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
  meta: () => req<Meta>("/api/meta"),
  markets: (p: { category?: string; venue?: string; q?: string; sort?: string; limit?: number; offset?: number }) => {
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

// ---- formatting

export const cents = (p: number | null | undefined, digits = 1) => (p === null || p === undefined ? "—" : `${(p * 100).toFixed(digits)}¢`);

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

export const venueLabel: Record<string, string> = { kalshi: "Kalshi", polymarket: "Polymarket", polymarket_us: "Poly US", demo: "Demo" };
