-- Sprint 2c: AI analysis (design doc section 6) and LLM call log.

CREATE TABLE IF NOT EXISTS llm_call (
    id                 BIGSERIAL PRIMARY KEY,
    ts                 TIMESTAMPTZ NOT NULL DEFAULT now(),
    task               TEXT NOT NULL,          -- 'analysis' | 'match' | ...
    provider           TEXT NOT NULL,
    model              TEXT,
    ok                 BOOLEAN NOT NULL,
    latency_ms         INTEGER,
    prompt_tokens      INTEGER,
    completion_tokens  INTEGER,
    error              TEXT
);

CREATE INDEX IF NOT EXISTS llm_call_ts_idx ON llm_call (ts DESC);

CREATE TABLE IF NOT EXISTS ai_batch (
    id          BIGSERIAL PRIMARY KEY,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    kind        TEXT NOT NULL DEFAULT 'picks',      -- 'picks' (top N) | 'single'
    status      TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'done', 'failed')),
    market_ids  TEXT[] NOT NULL DEFAULT '{}',
    done        INTEGER NOT NULL DEFAULT 0,
    error       TEXT
);

CREATE TABLE IF NOT EXISTS analysis (
    id                 BIGSERIAL PRIMARY KEY,
    batch_id           BIGINT REFERENCES ai_batch(id) ON DELETE SET NULL,
    market_id          TEXT NOT NULL REFERENCES market(id) ON DELETE CASCADE,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    status             TEXT NOT NULL DEFAULT 'ok' CHECK (status IN ('ok', 'failed')),
    recommendation     TEXT,          -- BUY_YES | BUY_NO | AVOID
    fair_value         DOUBLE PRECISION,
    confidence         DOUBLE PRECISION,
    risk_level         TEXT,          -- LOW | MEDIUM | HIGH
    net_edge           DOUBLE PRECISION,  -- dollars per contract on the recommended side, after fees
    entry_price        DOUBLE PRECISION,
    opportunity_score  DOUBLE PRECISION,
    edge_type          TEXT,
    reasoning          TEXT,
    edge_summary       TEXT,
    key_risks          JSONB NOT NULL DEFAULT '[]',
    catalysts          JSONB NOT NULL DEFAULT '[]',
    samples            JSONB NOT NULL DEFAULT '[]',   -- each sample's fair value / confidence
    inputs             JSONB NOT NULL DEFAULT '{}',   -- prices and news used (reproducibility, R6.1)
    model              TEXT,
    prompt_version     TEXT,
    error              TEXT
);

CREATE INDEX IF NOT EXISTS analysis_market_idx ON analysis (market_id, created_at DESC);
CREATE INDEX IF NOT EXISTS analysis_batch_idx ON analysis (batch_id);
