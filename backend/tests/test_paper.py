import os
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from predictarb import books, bus, db, paper
from predictarb.config import Settings
from predictarb.ingest.worker import Ingestor
from predictarb.models import Market, Quote

T0 = datetime(2026, 11, 20, tzinfo=timezone.utc)


# ---------------------------------------------------------------- books


def test_parse_books_and_no_side():
    pm = books.parse_polymarket("p", {"bids": [{"price": "0.44", "size": "200"}, {"price": "0.45", "size": "100"}],
                                      "asks": [{"price": "0.47", "size": "250"}, {"price": "0.46", "size": "150"}]})
    assert pm.yes_bids == [(0.45, 100.0), (0.44, 200.0)] and pm.yes_asks == [(0.46, 150.0), (0.47, 250.0)], "sorted best-first"
    assert pm.asks("NO") == [(0.55, 100.0), (0.56, 200.0)], "NO ask = 1 - YES bid"
    assert pm.bids("NO") == [(0.54, 150.0), (0.53, 250.0)]
    k = books.parse_kalshi("k", {"orderbook_fp": {"yes_dollars": [["0.4000", "10.00"]], "no_dollars": [["0.5800", "5.00"], ["0.5700", "9.00"]]}})
    assert k.yes_bids == [(0.4, 10.0)] and k.yes_asks == [(0.42, 5.0), (0.43, 9.0)], "Kalshi NO bids are YES asks"


def test_walk_and_fees():
    ladder = [(0.46, 150.0), (0.47, 250.0), (0.60, 1000.0)]
    f = books.walk(ladder, 300, limit=0.47)
    assert f.qty == 300 and f.levels == [(0.46, 150.0), (0.47, 150.0)] and f.avg_price == pytest.approx(0.465)
    assert books.walk(ladder, 1000, limit=0.47).qty == 400, "stops at the limit"
    assert books.walk([(0.5, 10.0), (0.4, 10.0)], 15, limit=0.45, buying=False).qty == 10
    assert books.taker_fee("kalshi", [(0.5, 100.0)], "x") == 1.75
    assert books.taker_fee("kalshi", [(0.5, 1.0)], "x") == 0.02, "rounded up per order"
    assert books.taker_fee("polymarket", [(0.5, 100.0)], "Sports") == pytest.approx(0.75)


def test_best_pair_size_stops_when_edge_runs_out():
    # Buy YES at Kalshi + NO at Polymarket. Edge shrinks level by level.
    yes = [(0.40, 100.0), (0.44, 100.0), (0.50, 100.0)]
    no = [(0.50, 50.0), (0.52, 300.0)]
    r = books.best_pair_size(yes, no, "kalshi", "polymarket", "Crypto", "Crypto")
    # level 1: 1-.40-.50-fees(.0168+.018)= .065 x 50; level 2: .40+.52 = .92 -> .045 x 50; then .44+.52 = .96 -> ~.0049 x100; .50+.52>1 stops
    assert r["qty"] == 200 and 0 < r["edge_dollars"] < 0.065 * 200


# ---------------------------------------------------------------- ledger


def fill(i, action, qty, price, fee=0.0, side="YES", m="k:A", minutes=0):
    return {"id": i, "market_id": m, "side": side, "action": action, "qty": qty, "price": price, "fee": fee,
            "ts": T0 + timedelta(minutes=minutes)}


def test_replay_average_cost_and_realized():
    led = paper.replay(1000, [fill(1, "buy", 100, 0.40, 1.68), fill(2, "buy", 100, 0.50, 1.75, minutes=1),
                              fill(3, "sell", 50, 0.60, 0.0, minutes=2)])
    pos = led.positions[("k:A", "YES")]
    assert pos.qty == 150
    assert pos.avg_cost == pytest.approx((40 + 1.68 + 50 + 1.75) / 200)
    assert pos.realized == pytest.approx((0.60 - pos.avg_cost) * 50)
    assert led.cash == pytest.approx(1000 - 41.68 - 51.75 + 30)
    settled = paper.replay(1000, [fill(1, "buy", 10, 0.40), fill(2, "settle", 10, 1.0, minutes=1)])
    assert settled.cash == pytest.approx(1006) and settled.closed_trades == [pytest.approx(6.0)]


def test_summarize_marks_and_drawdown():
    acct = {"starting_cash": 1000.0}
    mk = {"k:A": {"yes_bid": 0.45, "yes_ask": 0.47, "mid": 0.46, "title": "A", "venue": "kalshi"}}
    s = paper.summarize(acct, [fill(1, "buy", 100, 0.40)], mk, [])
    assert s["positions_value"] == 46.0 and s["equity"] == 1006.0 and s["unrealized"] == 6.0
    assert paper.summarize(acct, [fill(1, "buy", 100, 0.40)], mk, [], mark="exit")["equity"] == 1005.0
    no = paper.summarize(acct, [fill(1, "buy", 100, 0.50, side="NO")], mk, [])
    assert no["positions"][0]["mark"] == pytest.approx(0.54), "NO mark = 1 - YES mid"
    assert paper.max_drawdown([100, 120, 90, 130]) == -25.0


def test_plan_rules():
    m = {"id": "k:A", "venue": "kalshi", "category": "x", "status": "open"}
    book = books.Book("k:A", yes_bids=[(0.45, 100.0)], yes_asks=[(0.47, 100.0), (0.70, 1000.0)])
    req = lambda **kw: paper.OrderRequest(**{"account_id": 1, "market_id": "k:A", "side": "YES", "action": "buy",
                                             "type": "market", "qty": 300, **kw})
    p = paper.plan(req(), m, book, held=0, available_cash=10_000)
    assert p["fill_qty"] == 100 and p["unfilled_cancelled"] == 200, "fat-finger guard: 70c is >10c through 47c"
    lim = paper.plan(req(type="limit", limit_price=0.46), m, book, 0, 10_000)
    assert lim["fill_qty"] == 0 and lim["rest_qty"] == 300 and lim["max_loss"] == pytest.approx(138.0)
    with pytest.raises(paper.OrderRejected, match="available"):
        paper.plan(req(qty=100), m, book, 0, available_cash=10)
    with pytest.raises(paper.OrderRejected, match="you hold 5"):
        paper.plan(req(action="sell", qty=10), m, book, held=5, available_cash=0)
    with pytest.raises(paper.OrderRejected, match="not open"):
        paper.plan(req(), {**m, "status": "closed"}, book, 0, 10_000)


def test_crossing():
    o = {"action": "buy", "side": "YES", "limit_price": 0.46}
    assert paper.crossing(o, {"yes_ask": 0.47}) is None
    assert paper.crossing(o, {"yes_ask": 0.46, "yes_ask_size": 30}) == 30
    assert paper.crossing({**o, "side": "NO", "limit_price": 0.55}, {"yes_bid": 0.45}) == float("inf"), "unknown size"
    assert paper.crossing({"action": "sell", "side": "YES", "limit_price": 0.5}, {"yes_bid": 0.51}) == float("inf")


# ---------------------------------------------------------------- end to end

CLOB = "https://clob.paper.test"
GAMMA = "https://gamma.paper.test"
KAL = "https://kalshi.paper.test/v2"


async def test_paper_trading_end_to_end(clean_db, monkeypatch):
    monkeypatch.setenv("POLYMARKET_CLOB_URL", CLOB)
    monkeypatch.setenv("POLYMARKET_GAMMA_URL", GAMMA)
    monkeypatch.setenv("KALSHI_BASE_URL", KAL)
    from predictarb.api import main as api_main
    api_main.get_settings.cache_clear()
    settings = Settings(database_url=clean_db, redis_url=os.environ["REDIS_URL"], polymarket_clob_url=CLOB,
                        polymarket_gamma_url=GAMMA, kalshi_base_url=KAL)
    now = datetime.now(timezone.utc)
    pool = await db.create_pool(clean_db)
    try:
        for v in ("kalshi", "polymarket"):
            await db.upsert_venue(pool, v, v)
        await db.upsert_markets(pool, [Market(
            venue="polymarket", venue_market_id="77", title="Will Bitcoin be above $120k on December 31, 2026?",
            category="Crypto", close_ts=now + timedelta(days=30), extra={"clob_token_ids": ["111", "222"]},
            quote=Quote(venue="polymarket", venue_market_id="77", yes_bid=0.45, yes_ask=0.46, ts=now))])
        await db.upsert_markets(pool, [Market(
            venue="kalshi", venue_market_id="KXBTC", title="Bitcoin price on Dec 31, 2026?", outcome_label="$120,000 or above",
            category="Crypto", close_ts=now + timedelta(days=30),
            quote=Quote(venue="kalshi", venue_market_id="KXBTC", yes_bid=0.38, yes_ask=0.40, yes_bid_size=500, yes_ask_size=300, ts=now))])
        await pool.execute("INSERT INTO market_link (market_a, market_b, score, status) VALUES ('kalshi:KXBTC', 'polymarket:77', 0.9, 'confirmed')")
        acct = await pool.fetchval("SELECT id FROM paper_account WHERE name='Main'")
    finally:
        await pool.close()

    pm_book = {"bids": [{"price": "0.45", "size": "100"}, {"price": "0.44", "size": "500"}],
               "asks": [{"price": "0.46", "size": "150"}, {"price": "0.47", "size": "250"}]}
    with respx.mock(assert_all_called=False) as rx:
        rx.route(host="testserver").pass_through()
        rx.get(f"{CLOB}/book").respond(json=pm_book)
        rx.get(f"{KAL}/markets/KXBTC/orderbook").respond(401)  # -> falls back to top of book
        with TestClient(api_main.app) as c:
            book = c.get("/api/markets/polymarket:77/book").json()
            assert book["source"] == "book" and book["yes_asks"][0] == [0.46, 150.0]
            assert c.get("/api/markets/kalshi:KXBTC/book").json()["source"] == "top_of_book"

            order = {"account_id": acct, "market_id": "polymarket:77", "side": "YES", "action": "buy", "qty": 200}
            pv = c.post("/api/paper/preview", json=order).json()
            assert pv["ok"] and pv["fill_qty"] == 200 and pv["avg_price"] == pytest.approx((0.46 * 150 + 0.47 * 50) / 200)
            fee = 0.072 * (150 * 0.46 * 0.54 + 50 * 0.47 * 0.53)
            assert pv["fee"] == pytest.approx(fee, abs=1e-4)
            r = c.post("/api/paper/orders", json=order)
            assert r.status_code == 201 and r.json()["status"] == "filled"

            bad = c.post("/api/paper/orders", json={**order, "action": "sell", "qty": 500})
            assert bad.status_code == 422 and "you hold 200" in bad.json()["detail"]
            assert c.post("/api/paper/preview", json={**order, "qty": 10_000_000}).status_code == 422, "pydantic bound"

            # Resting limit buy on Kalshi NO at 58c (NO ask is 1 - 0.38 = 62c now).
            lim = c.post("/api/paper/orders", json={"account_id": acct, "market_id": "kalshi:KXBTC", "side": "NO",
                                                    "action": "buy", "type": "limit", "qty": 100, "limit_price": 0.58}).json()
            assert lim["status"] == "open" and lim["fill_qty"] == 0

            # Depth-aware arb row: YES Kalshi @ 40c (300 at top of book) + NO Polymarket @ 55c (100 deep, then 56c).
            arb_rows = c.get("/api/arb").json()["items"]
            assert arb_rows and arb_rows[0]["depth"]["qty"] == 300
            pf = c.get("/api/paper/portfolio", params={"account_id": acct}).json()
            assert pf["summary"]["reserved_cash"] == 58.0 and len(pf["open_orders"]) == 1
            assert pf["summary"]["positions"][0]["qty"] == 200

    # Worker: a quote crosses the resting order, then the Polymarket market resolves YES.
    pool = await db.create_pool(clean_db)
    redis = bus.connect(settings.redis_url)
    ing = Ingestor(settings, pool, redis, [])
    try:
        ing.titles["kalshi:KXBTC"] = "BTC"
        n = await ing.check_resting_orders([Quote(venue="kalshi", venue_market_id="KXBTC", yes_bid=0.43, yes_ask=0.45,
                                                  yes_bid_size=60, ts=now)])
        assert n == 1
        o = await pool.fetchrow("SELECT * FROM paper_order WHERE market_id='kalshi:KXBTC'")
        assert o["filled_qty"] == 60 and o["status"] == "open", "partial fill limited by top-of-book size"
        await pool.execute("UPDATE market SET close_ts = now() - interval '1 hour' WHERE id='polymarket:77'")
        with respx.mock:
            respx.get(f"{GAMMA}/markets/77").respond(json={"closed": True, "outcomePrices": "[\"1\", \"0\"]"})
            assert await ing.settle() == 1
        _, fills, orders = await paper.account_state(pool, acct)
        s = paper.summarize({"starting_cash": 10_000.0}, fills, await db.markets_by_ids(pool, ["polymarket:77", "kalshi:KXBTC"]), orders)
        btc = [f for f in fills if f["action"] == "settle"]
        assert btc and btc[0]["price"] == 1.0 and btc[0]["qty"] == 200
        expected_cash = 10_000 - (0.46 * 150 + 0.47 * 50) - fee + 200 - 0.58 * 60
        assert s["cash"] == pytest.approx(expected_cash, abs=0.01)
        await ing.snapshot_equity()
        assert await pool.fetchval("SELECT count(*) FROM paper_equity") == 1
    finally:
        await ing.fetcher.aclose()
        await ing.llm.aclose()
        await pool.close()
        await redis.aclose()
        api_main.get_settings.cache_clear()
