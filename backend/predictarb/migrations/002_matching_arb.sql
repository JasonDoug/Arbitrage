-- Sprint 2a: cross-venue market links (design doc R4.2) and arbitrage episodes (R5.5).

CREATE TABLE IF NOT EXISTS market_link (
    id               BIGSERIAL PRIMARY KEY,
    market_a         TEXT NOT NULL REFERENCES market(id) ON DELETE CASCADE,  -- Kalshi side
    market_b         TEXT NOT NULL REFERENCES market(id) ON DELETE CASCADE,  -- Polymarket side
    status           TEXT NOT NULL DEFAULT 'suggested'
                     CHECK (status IN ('suggested', 'confirmed', 'rejected')),
    -- identical: YES on A == YES on B; inverse: YES on A == NO on B
    relation         TEXT NOT NULL DEFAULT 'identical'
                     CHECK (relation IN ('identical', 'inverse')),
    score            DOUBLE PRECISION NOT NULL,             -- text-match score 0..1
    reasons          JSONB NOT NULL DEFAULT '[]',           -- why the matcher paired them
    -- optional LLM review (Sprint 2c)
    llm_relation     TEXT,
    llm_confidence   DOUBLE PRECISION,
    llm_differences  JSONB,
    llm_model        TEXT,
    rules_hash_a     TEXT,
    rules_hash_b     TEXT,
    note             TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewed_at      TIMESTAMPTZ,
    UNIQUE (market_a, market_b)
);

CREATE INDEX IF NOT EXISTS market_link_status_idx ON market_link (status, score DESC);

CREATE TABLE IF NOT EXISTS arb_opportunity (
    id           BIGSERIAL PRIMARY KEY,
    link_id      BIGINT NOT NULL REFERENCES market_link(id) ON DELETE CASCADE,
    direction    TEXT NOT NULL,          -- e.g. 'yes_a_no_b'
    first_seen   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen    TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at    TIMESTAMPTZ,            -- NULL while the edge is still open
    best_edge    DOUBLE PRECISION NOT NULL,
    last_edge    DOUBLE PRECISION NOT NULL,
    last_cost    DOUBLE PRECISION NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS arb_open_idx ON arb_opportunity (link_id, direction) WHERE closed_at IS NULL;
