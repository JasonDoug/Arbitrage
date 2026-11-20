-- Sprint 1 schema: catalog, price history, watchlist, alerts.
-- Works on plain Postgres 16; uses TimescaleDB hypertables when the extension is available.

DO $$
BEGIN
    CREATE EXTENSION IF NOT EXISTS timescaledb;
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'timescaledb not available, using plain tables';
END $$;

CREATE TABLE IF NOT EXISTS venue (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'unknown',   -- ok | stale | error | unknown
    last_ok_at  TIMESTAMPTZ,
    last_error  TEXT
);

CREATE TABLE IF NOT EXISTS market (
    id               TEXT PRIMARY KEY,             -- '<venue>:<venue_market_id>'
    venue            TEXT NOT NULL REFERENCES venue(id),
    venue_market_id  TEXT NOT NULL,
    event_id         TEXT,
    event_title      TEXT,
    title            TEXT NOT NULL,
    outcome_label    TEXT,
    url              TEXT,
    category         TEXT NOT NULL DEFAULT 'Other',
    tags             TEXT[] NOT NULL DEFAULT '{}',
    rules_text       TEXT,
    rules_hash       TEXT,
    resolution_source TEXT,
    close_ts         TIMESTAMPTZ,
    status           TEXT NOT NULL DEFAULT 'open',
    volume_unit      TEXT NOT NULL DEFAULT 'usd',
    total_volume     DOUBLE PRECISION,
    extra            JSONB NOT NULL DEFAULT '{}',
    -- latest quote (denormalized for fast card rendering)
    yes_bid          DOUBLE PRECISION,
    yes_ask          DOUBLE PRECISION,
    last_price       DOUBLE PRECISION,
    mid              DOUBLE PRECISION,
    spread           DOUBLE PRECISION,
    change_24h       DOUBLE PRECISION,
    volume_24h       DOUBLE PRECISION,
    volume_24h_usd   DOUBLE PRECISION,
    open_interest    DOUBLE PRECISION,
    liquidity_usd    DOUBLE PRECISION,
    quote_ts         TIMESTAMPTZ,
    first_seen       TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen        TIMESTAMPTZ NOT NULL DEFAULT now(),
    backfilled_at    TIMESTAMPTZ,
    search           TSVECTOR GENERATED ALWAYS AS (
        to_tsvector('english', coalesce(title, '') || ' ' || coalesce(event_title, '') || ' ' || coalesce(outcome_label, ''))
    ) STORED,
    UNIQUE (venue, venue_market_id)
);

CREATE INDEX IF NOT EXISTS market_status_idx ON market (status);
CREATE INDEX IF NOT EXISTS market_category_idx ON market (category);
CREATE INDEX IF NOT EXISTS market_vol_idx ON market (volume_24h_usd DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS market_close_idx ON market (close_ts);
CREATE INDEX IF NOT EXISTS market_search_idx ON market USING GIN (search);

CREATE TABLE IF NOT EXISTS price_tick (
    market_id   TEXT NOT NULL,
    ts          TIMESTAMPTZ NOT NULL,
    price       DOUBLE PRECISION NOT NULL,             -- mid if available, else last trade
    yes_bid     DOUBLE PRECISION,
    yes_ask     DOUBLE PRECISION,
    volume      DOUBLE PRECISION,
    source      TEXT NOT NULL DEFAULT 'poll',          -- poll | backfill | ws
    PRIMARY KEY (market_id, ts)
);

DO $$
BEGIN
    PERFORM create_hypertable('price_tick', 'ts', if_not_exists => TRUE, migrate_data => TRUE);
EXCEPTION WHEN undefined_function THEN
    RAISE NOTICE 'create_hypertable unavailable, price_tick stays a plain table';
END $$;

CREATE TABLE IF NOT EXISTS watchlist_item (
    user_id      TEXT NOT NULL DEFAULT 'me',
    market_id    TEXT NOT NULL REFERENCES market(id) ON DELETE CASCADE,
    added_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    added_price  DOUBLE PRECISION,
    notes        TEXT,
    PRIMARY KEY (user_id, market_id)
);

CREATE TABLE IF NOT EXISTS alert_rule (
    id               BIGSERIAL PRIMARY KEY,
    user_id          TEXT NOT NULL DEFAULT 'me',
    market_id        TEXT NOT NULL REFERENCES market(id) ON DELETE CASCADE,
    kind             TEXT NOT NULL CHECK (kind IN ('price_above', 'price_below', 'move')),
    threshold        DOUBLE PRECISION NOT NULL,   -- probability for price_*; absolute move for 'move'
    window_minutes   INTEGER NOT NULL DEFAULT 60, -- for 'move'
    cooldown_minutes INTEGER NOT NULL DEFAULT 60,
    enabled          BOOLEAN NOT NULL DEFAULT TRUE,
    last_fired_at    TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS alert_rule_market_idx ON alert_rule (market_id) WHERE enabled;

CREATE TABLE IF NOT EXISTS notification (
    id          BIGSERIAL PRIMARY KEY,
    user_id     TEXT NOT NULL DEFAULT 'me',
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    market_id   TEXT REFERENCES market(id) ON DELETE CASCADE,
    rule_id     BIGINT REFERENCES alert_rule(id) ON DELETE SET NULL,
    kind        TEXT NOT NULL,
    message     TEXT NOT NULL,
    read        BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS notification_unread_idx ON notification (user_id, read, ts DESC);

CREATE TABLE IF NOT EXISTS schema_migration (
    name        TEXT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
