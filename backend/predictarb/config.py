from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All settings come from environment variables (or a .env file)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://predictarb:predictarb@localhost:5432/predictarb"
    redis_url: str = "redis://localhost:6379/0"

    # Comma-separated list of enabled venues: kalshi, polymarket, polymarket_us, demo
    venues: str = "kalshi,polymarket"

    kalshi_base_url: str = "https://api.elections.kalshi.com/trade-api/v2"
    polymarket_gamma_url: str = "https://gamma-api.polymarket.com"
    polymarket_clob_url: str = "https://clob.polymarket.com"
    polymarket_us_gateway_url: str = "https://gateway.polymarket.us"

    # Polymarket has thousands of thin markets; keep the top N by 24h volume.
    polymarket_max_markets: int = 3000
    # Kalshi: skip multivariate combo markets (parlays) by default.
    kalshi_exclude_mve: bool = True
    # Kalshi lists ~200k open markets, mostly untraded strike ladders. Keep only markets
    # with trading activity (24h volume or open interest), top N by 24h dollar volume.
    kalshi_max_markets: int = 5000
    # Tickers per quote request when polling only the tracked markets.
    kalshi_quote_batch: int = 100

    # Cross-venue matching and arbitrage
    match_interval_s: int = 1800
    match_min_score: float = 0.5
    arb_interval_s: int = 30
    arb_max_quote_age_s: int = 180  # quotes older than this aren't used for arb
    # Polymarket taker fee rate by our category, e.g. '{"Crypto": 0.072, "Sports": 0.03}'.
    # Unset = built-in defaults (predictarb/arb.py).
    polymarket_fee_rates: dict[str, float] | None = None

    # News
    news_interval_s: int = 300
    news_feeds: dict[str, str] | None = None  # JSON {"name": "url"}; unset = built-in list
    news_min_score: float = 0.25
    news_alert_score: float = 0.4  # notify when a watched market gets a match this strong
    news_retention_days: int = 14

    catalog_interval_s: int = 600
    quote_interval_s: int = 20
    backfill_top_n: int = 150
    backfill_days: int = 30

    api_host: str = "0.0.0.0"
    api_port: int = 8000
    cors_origins: str = "http://localhost:5173"

    @property
    def venue_list(self) -> list[str]:
        return [v.strip() for v in self.venues.split(",") if v.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
