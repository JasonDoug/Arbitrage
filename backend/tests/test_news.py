import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import respx
from fastapi.testclient import TestClient

from predictarb import bus, db, news
from predictarb.config import Settings
from predictarb.ingest.worker import Ingestor
from predictarb.models import Market, Quote

FIX = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 11, 20, 18, tzinfo=timezone.utc)

MARKETS = [
    {"id": "kalshi:FED-H", "title": "Fed decision in December 2026", "outcome_label": "Hold", "event_title": None},
    {"id": "polymarket:1", "title": "Will Bitcoin be above $120k on December 31, 2026?", "outcome_label": None, "event_title": None},
    {"id": "polymarket:2", "title": "Wizards vs. Bulls", "outcome_label": "Wizards", "event_title": None},
] + [{"id": f"x:{i}", "title": f"Filler market number {i} about topic{i}", "outcome_label": None, "event_title": None} for i in range(30)]


def test_parse_feed_filters_and_cleans():
    items = news.parse_feed("BBC", (FIX / "feed_bbc.xml").read_bytes(), NOW)
    assert [i.url for i in items] == ["https://www.bbc.co.uk/news/business-1", "https://www.bbc.co.uk/news/business-2"]
    assert items[0].summary == "The Federal Reserve kept its benchmark rate unchanged on Wednesday.", "HTML stripped"
    assert items[0].published_at == datetime(2026, 11, 20, 14, tzinfo=timezone.utc)


def test_dedupe_same_headline_across_feeds():
    a = news.parse_feed("BBC", (FIX / "feed_bbc.xml").read_bytes(), NOW)
    b = news.parse_feed("NPR", (FIX / "feed_npr.xml").read_bytes(), NOW)
    out = news.dedupe(a + b)
    fed = [i for i in out if "Fed" in i.title]
    assert len(fed) == 1 and fed[0].source == "BBC", "earliest copy of a syndicated story wins"
    assert len(out) == 3


def test_match_headlines_to_markets():
    idx = news.MarketIndex.build(MARKETS)
    items = {i.url: i for i in news.parse_feed("BBC", (FIX / "feed_bbc.xml").read_bytes(), NOW)}
    fed = idx.match(items["https://www.bbc.co.uk/news/business-1"])
    assert fed and fed[0][0] == "kalshi:FED-H", "'holds ... no change' matches the Hold outcome"
    assert "unchanged" in fed[0][2]
    btc = idx.match(items["https://www.bbc.co.uk/news/business-2"])
    assert btc and btc[0][0] == "polymarket:1"
    assert idx.match(news.NewsItem(url="u", source="s", title="Weather is nice", summary=None, published_at=NOW)) == []
    etf = idx.match(news.NewsItem(url="u2", source="CoinDesk", title="Bitcoin ETF inflows hit record as BTC tests $120,000 resistance",
                                  summary=None, published_at=NOW))
    assert etf and etf[0][0] == "polymarket:1", "a distinctive threshold number counts as a shared term"


@respx.mock
async def test_fetch_feeds_survives_a_bad_feed(monkeypatch):
    monkeypatch.setattr(news, "MAX_AGE", timedelta(days=3650))
    respx.get("https://a.test/rss").respond(content=(FIX / "feed_bbc.xml").read_bytes())
    respx.get("https://b.test/rss").respond(status_code=403)
    async with httpx.AsyncClient() as c:
        items, errors = await news.fetch_feeds({"A": "https://a.test/rss", "B": "https://b.test/rss"}, c)
    assert len(items) >= 2 and list(errors) == ["B"]


@respx.mock
async def test_news_pipeline_end_to_end(clean_db, monkeypatch):
    monkeypatch.setattr(news, "MAX_AGE", timedelta(days=3650))
    respx.get("https://bbc.test/rss").respond(content=(FIX / "feed_bbc.xml").read_bytes())
    respx.get("https://npr.test/rss").respond(content=(FIX / "feed_npr.xml").read_bytes())
    settings = Settings(database_url=clean_db, redis_url=os.environ["REDIS_URL"],
                        news_feeds={"BBC": "https://bbc.test/rss", "NPR": "https://npr.test/rss"})
    pool = await db.create_pool(clean_db)
    redis = bus.connect(settings.redis_url)
    try:
        await db.upsert_venue(pool, "kalshi", "Kalshi")
        await db.upsert_venue(pool, "polymarket", "Polymarket")
        q = lambda v, i: Quote(venue=v, venue_market_id=i, yes_bid=0.4, yes_ask=0.42, ts=datetime.now(timezone.utc))
        await db.upsert_markets(pool, [Market(venue="kalshi", venue_market_id="FED-H", title="Fed decision in December 2026",
                                              outcome_label="Hold", category="Economics", quote=q("kalshi", "FED-H"))])
        await db.upsert_markets(pool, [Market(venue="polymarket", venue_market_id=m["id"].split(":")[1], title=m["title"],
                                              outcome_label=m["outcome_label"], quote=q("polymarket", m["id"].split(":")[1]))
                                       for m in MARKETS if m["id"].startswith("polymarket")
                                       ] + [Market(venue="polymarket", venue_market_id=f"f{i}", title=f"Filler market number {i} about topic{i}")
                                            for i in range(30)])
        await db.add_watch(pool, "kalshi:FED-H")
        ing = Ingestor(settings, pool, redis, [])
        async with httpx.AsyncClient() as c:
            assert await ing.poll_news(c) == 4, "5 entries, Fed story deduped across feeds"
            assert await ing.poll_news(c) == 0, "already stored"
        notes = await db.list_notifications(pool)
        assert len(notes) == 1 and notes[0]["kind"] == "news" and "Fed holds" in notes[0]["message"]
        assert await redis.xlen(bus.NOTIFICATIONS) == 1
    finally:
        await pool.close()
        await redis.aclose()

    from predictarb.api.main import app

    with TestClient(app) as client:
        watch = client.get("/api/news").json()["items"]
        assert [i["title"][:9] for i in watch] == ["Fed holds"]
        assert watch[0]["markets"][0]["id"] == "kalshi:FED-H" and watch[0]["markets"][0]["terms"]
        assert len(client.get("/api/news", params={"scope": "all"}).json()["items"]) == 4
        by_market = client.get("/api/news", params={"market_id": "polymarket:1"}).json()["items"]
        assert [i["title"][:7] for i in by_market] == ["Bitcoin"]
