-- Sprint 2b: news items and their links to markets (design doc section 9).

CREATE TABLE IF NOT EXISTS news_item (
    id            BIGSERIAL PRIMARY KEY,
    url           TEXT NOT NULL UNIQUE,
    title_key     TEXT NOT NULL,        -- normalized headline, dedupes the same story across feeds
    source        TEXT NOT NULL,
    title         TEXT NOT NULL,
    summary       TEXT,
    published_at  TIMESTAMPTZ NOT NULL,
    ingested_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS news_title_key_idx ON news_item (title_key);
CREATE INDEX IF NOT EXISTS news_published_idx ON news_item (published_at DESC);

CREATE TABLE IF NOT EXISTS news_market (
    news_id        BIGINT NOT NULL REFERENCES news_item(id) ON DELETE CASCADE,
    market_id      TEXT NOT NULL REFERENCES market(id) ON DELETE CASCADE,
    score          DOUBLE PRECISION NOT NULL,
    matched_terms  TEXT[] NOT NULL DEFAULT '{}',
    PRIMARY KEY (news_id, market_id)
);

CREATE INDEX IF NOT EXISTS news_market_market_idx ON news_market (market_id);
