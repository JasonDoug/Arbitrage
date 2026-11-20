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
