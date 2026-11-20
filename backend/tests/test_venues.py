import httpx
import pytest
import respx

from predictarb.config import Settings
from predictarb.venues.base import HttpVenue, VenueError
from predictarb.venues.kalshi import KalshiAdapter, parse_candles
from predictarb.venues.polymarket import PolymarketAdapter

K = "https://kalshi.test/trade-api/v2"
G = "https://gamma.test"
C = "https://clob.test"


@pytest.fixture
def settings():
    return Settings(
        kalshi_base_url=K, polymarket_gamma_url=G, polymarket_clob_url=C, polymarket_max_markets=1000
    )


@respx.mock
async def test_kalshi_catalog(settings, fixture):
    respx.get(f"{K}/series").respond(json=fixture("kalshi_series.json"))
    respx.get(f"{K}/events").respond(json=fixture("kalshi_events.json"))
    markets = await KalshiAdapter(settings).fetch_markets()

    by_id = {m.venue_market_id: m for m in markets}
    assert "KXMVECOMBO-1" not in by_id, "multivariate combos are excluded"
    hold = by_id["KXFEDDECISION-26DEC-H0"]
    assert hold.id == "kalshi:KXFEDDECISION-26DEC-H0"
    assert hold.title == "Fed decision in December 2026"
    assert hold.outcome_label == "Hold"
    assert hold.category == "Economics"
    assert hold.resolution_source == "Federal Reserve"
    assert hold.volume_unit == "contracts"
    assert hold.rules_text.startswith("If the Federal Reserve holds")
    q = hold.quote
    assert (q.yes_bid, q.yes_ask, q.last_price) == (0.61, 0.63, 0.62)
    assert q.mid == 0.62 and q.spread == 0.02
    assert q.change_24h == pytest.approx(0.04)
    assert q.volume_24h_usd == pytest.approx(12000 * 0.62)

    cut = by_id["KXFEDDECISION-26DEC-C25"].quote
    assert cut.yes_bid is None and cut.yes_ask is None and cut.last_price is None, "empty book -> None"

    lv = by_id["KXHIGHLV-26NOV21-B70"]
    assert lv.category == "Weather", "series category wins over missing event category"
    assert lv.url == "https://kalshi.com/markets/kxhighlv"


@respx.mock
async def test_kalshi_series_failure_falls_back(settings, fixture):
    respx.get(f"{K}/series").respond(status_code=404)
    respx.get(f"{K}/events").respond(json=fixture("kalshi_events.json"))
    markets = await KalshiAdapter(settings).fetch_markets()
    lv = next(m for m in markets if m.venue_market_id == "KXHIGHLV-26NOV21-B70")
    assert lv.category == "Weather", "title keyword fallback"


@respx.mock
async def test_kalshi_quotes_paginate(settings, fixture):
    m = fixture("kalshi_events.json")["events"][0]["markets"]
    route = respx.get(f"{K}/markets")
    route.side_effect = [
        httpx.Response(200, json={"markets": [m[0]], "cursor": "abc"}),
        httpx.Response(200, json={"markets": [m[1]], "cursor": ""}),
    ]
    quotes = await KalshiAdapter(settings).fetch_quotes()
    assert [q.venue_market_id for q in quotes] == ["KXFEDDECISION-26DEC-H0", "KXFEDDECISION-26DEC-C25"]
    assert route.calls[1].request.url.params["cursor"] == "abc"
    assert route.calls[0].request.url.params["mve_filter"] == "exclude"


def test_kalshi_candles(fixture):
    pts = parse_candles(fixture("kalshi_candles.json")["candlesticks"])
    assert [p.price for p in pts] == [0.56, 0.59, 0.59]  # close, then bid/ask mid, then previous
    assert pts[1].yes_bid == 0.58 and pts[2].yes_bid is None


@respx.mock
async def test_polymarket_catalog(settings, fixture):
    route = respx.get(f"{G}/markets").respond(json=fixture("polymarket_markets.json"))
    markets = await PolymarketAdapter(settings).fetch_markets()
    assert [m.venue_market_id for m in markets] == ["512345", "512999"], "closed and AMM-only markets dropped"
    btc, vgk = markets
    assert btc.category == "Crypto"
    assert btc.url == "https://polymarket.com/event/bitcoin-price-dec-31"
    assert btc.extra["clob_token_ids"] == ["111", "222"]
    assert btc.event_title == "Bitcoin price on December 31?"
    assert btc.outcome_label is None
    assert (btc.quote.yes_bid, btc.quote.yes_ask, btc.quote.mid) == (0.41, 0.42, 0.415)
    assert btc.quote.change_24h == -0.035 and btc.quote.volume_24h_usd == 95321.2
    assert vgk.category == "Sports" and vgk.extra["neg_risk"] is True
    params = route.calls[0].request.url.params
    assert params["closed"] == "false" and params["order"] == "volume24hr" and params["include_tag"] == "true"


@respx.mock
async def test_polymarket_history(settings, fixture):
    route = respx.get(f"{C}/prices-history").respond(json=fixture("polymarket_history.json"))
    pts = await PolymarketAdapter(settings).fetch_history(
        {"venue_market_id": "512345", "extra": {"clob_token_ids": ["111", "222"]}}, days=30
    )
    assert [p.price for p in pts] == [0.4, 0.41, 0.415]
    assert route.calls[0].request.url.params["market"] == "111"


@respx.mock
async def test_http_retries_then_raises(monkeypatch):
    async def no_sleep(_):
        return None

    monkeypatch.setattr("predictarb.venues.base.asyncio.sleep", no_sleep)
    route = respx.get("https://x.test/a")
    route.side_effect = [httpx.Response(429), httpx.Response(503), httpx.Response(200, json={"ok": 1})]
    http = HttpVenue("https://x.test", rate_per_s=1000)
    assert await http.get_json("/a") == {"ok": 1}

    respx.get("https://x.test/b").respond(status_code=400, text="bad")
    with pytest.raises(VenueError, match="400"):
        await http.get_json("/b")
