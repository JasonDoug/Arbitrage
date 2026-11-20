-- Sprint 3: top-of-book sizes and paper trading (design doc sections 7 and 10, Mode A).

ALTER TABLE market ADD COLUMN IF NOT EXISTS yes_bid_size DOUBLE PRECISION;
ALTER TABLE market ADD COLUMN IF NOT EXISTS yes_ask_size DOUBLE PRECISION;
ALTER TABLE market ADD COLUMN IF NOT EXISTS result TEXT;          -- 'yes' | 'no' once resolved
ALTER TABLE market ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS paper_account (
    id             BIGSERIAL PRIMARY KEY,
    user_id        TEXT NOT NULL DEFAULT 'me',
    name           TEXT NOT NULL,
    starting_cash  DOUBLE PRECISION NOT NULL DEFAULT 10000,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    reset_at       TIMESTAMPTZ,
    UNIQUE (user_id, name)
);

-- Positions, cost basis, cash and P&L are always derived from fills (the ledger), never stored.
CREATE TABLE IF NOT EXISTS paper_order (
    id           BIGSERIAL PRIMARY KEY,
    account_id   BIGINT NOT NULL REFERENCES paper_account(id) ON DELETE CASCADE,
    market_id    TEXT NOT NULL REFERENCES market(id),
    side         TEXT NOT NULL CHECK (side IN ('YES', 'NO')),
    action       TEXT NOT NULL CHECK (action IN ('buy', 'sell')),
    type         TEXT NOT NULL CHECK (type IN ('market', 'limit')),
    qty          DOUBLE PRECISION NOT NULL CHECK (qty > 0),
    limit_price  DOUBLE PRECISION,
    filled_qty   DOUBLE PRECISION NOT NULL DEFAULT 0,
    avg_price    DOUBLE PRECISION,
    fees         DOUBLE PRECISION NOT NULL DEFAULT 0,
    status       TEXT NOT NULL CHECK (status IN ('open', 'filled', 'partial', 'cancelled', 'rejected')),
    reason       TEXT,
    group_id     TEXT,                -- legs of one arbitrage trade share a group
    book_source  TEXT,                -- 'book' | 'top_of_book' | 'demo'
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS paper_order_open_idx ON paper_order (market_id) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS paper_order_account_idx ON paper_order (account_id, created_at DESC);

CREATE TABLE IF NOT EXISTS paper_fill (
    id          BIGSERIAL PRIMARY KEY,
    order_id    BIGINT REFERENCES paper_order(id) ON DELETE CASCADE,   -- NULL for settlements
    account_id  BIGINT NOT NULL REFERENCES paper_account(id) ON DELETE CASCADE,
    market_id   TEXT NOT NULL REFERENCES market(id),
    side        TEXT NOT NULL,
    action      TEXT NOT NULL CHECK (action IN ('buy', 'sell', 'settle')),
    qty         DOUBLE PRECISION NOT NULL,
    price       DOUBLE PRECISION NOT NULL,
    fee         DOUBLE PRECISION NOT NULL DEFAULT 0,
    liquidity   TEXT NOT NULL DEFAULT 'taker',   -- taker | maker | settlement
    ts          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS paper_fill_account_idx ON paper_fill (account_id, ts);

CREATE TABLE IF NOT EXISTS paper_equity (
    account_id  BIGINT NOT NULL REFERENCES paper_account(id) ON DELETE CASCADE,
    ts          TIMESTAMPTZ NOT NULL,
    equity      DOUBLE PRECISION NOT NULL,
    cash        DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (account_id, ts)
);

INSERT INTO paper_account (name) VALUES ('Main') ON CONFLICT DO NOTHING;
