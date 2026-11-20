from datetime import datetime, timedelta, timezone

import pytest

from predictarb import arb
from predictarb.matching import suggest, tokenize

NOW = datetime(2026, 11, 20, tzinfo=timezone.utc)
DEC31 = datetime(2026, 12, 31, 23, 59, tzinfo=timezone.utc)


def m(id, title, cat="Crypto", close=DEC31, outcome=None, event=None):
    return {"id": id, "title": title, "outcome_label": outcome, "event_title": event, "category": cat, "close_ts": close}


KALSHI = [
    m("k:btc120", "Bitcoin price on Dec 31, 2026?", outcome="$120,000 or above"),
    m("k:btc150", "Bitcoin price on Dec 31, 2026?", outcome="$150,000 or above"),
    m("k:fedhold", "Fed decision in December 2026", "Economics", datetime(2026, 12, 9, tzinfo=timezone.utc), outcome="Hold"),
    m("k:lvtemp", "Highest temperature in Las Vegas on Nov 21?", "Weather", NOW + timedelta(days=1), outcome="70° or above"),
]
POLY = [
    m("p:btc120", "Will Bitcoin be above $120k on December 31, 2026?"),
    m("p:btc150", "Will Bitcoin be above $150,000 on December 31, 2026?"),
    m("p:fednochange", "Will there be no change in Fed interest rates after the December 2026 meeting?", "Economics",
      datetime(2026, 12, 9, tzinfo=timezone.utc), event="Fed Decision in December?"),
    m("p:nba", "Wizards vs. Bulls", "Sports", NOW + timedelta(hours=8)),
]


def test_tokenize_normalizes():
    assert tokenize("Will BTC hit $120,000 by Dec. 31?") == ["bitcoin", "hit", "120000", "december", "31"]
    assert tokenize("Hike 25bps") == tokenize("increase by 25 basis points") == ["hike", "25", "bps"]
    assert tokenize("Hold") == tokenize("no change") == ["unchanged"]
    assert "120000" in tokenize("above $120k")
    assert "above" in tokenize("above"), "comparators are kept"


def test_suggest_pairs_same_contract_and_respects_thresholds():
    got = {s.a: s for s in suggest(KALSHI, POLY, min_score=0.3)}
    assert got["k:btc120"].b == "p:btc120"
    assert got["k:btc150"].b == "p:btc150", "the threshold number decides between near-identical titles"
    assert got["k:fedhold"].b == "p:fednochange", "Hold == no change"
    assert "k:lvtemp" not in got
    assert all(0 < s.score <= 1 for s in got.values())
    assert any("shared numbers" in r for r in got["k:btc120"].reasons)


def test_suggest_excludes_known_pairs():
    got = suggest(KALSHI, POLY, min_score=0.3, exclude={("k:btc120", "p:btc120")})
    assert all((s.a, s.b) != ("k:btc120", "p:btc120") for s in got)


# ---------------------------------------------------------------- fees


@pytest.mark.parametrize(
    "price,contracts,fee",
    [(0.50, 100, 1.75), (0.10, 100, 0.63), (0.25, 100, 1.32), (0.65, 100, 1.60), (0.01, 1, 0.01), (0.50, 1, 0.02)],
)
def test_kalshi_fee_matches_published_table(price, contracts, fee):
    assert arb.kalshi_fee(price, contracts) == pytest.approx(fee)


def test_polymarket_fee_by_category():
    assert arb.polymarket_fee_per_contract(0.5, "Crypto") == pytest.approx(0.018)
    assert arb.polymarket_fee_per_contract(0.5, "Sports") == pytest.approx(0.0075)
    assert arb.polymarket_fee_per_contract(0.5, "Unknown") == pytest.approx(0.0125), "falls back to Other"
    assert arb.polymarket_fee_per_contract(0.5, "Politics", {"Politics": 0.0}) == 0


# ---------------------------------------------------------------- arbitrage


def side(id, venue, bid, ask, cat="Crypto", close=DEC31, age_s=5):
    return {"id": id, "venue": venue, "yes_bid": bid, "yes_ask": ask, "category": cat, "close_ts": close,
            "quote_ts": NOW - timedelta(seconds=age_s)}


def test_identical_pair_edge_both_directions():
    link = {"id": 1, "relation": "identical"}
    a = side("kalshi:X", "kalshi", 0.38, 0.40)
    b = side("polymarket:Y", "polymarket", 0.62, 0.64)
    opps = {o.direction: o for o in arb.evaluate_link(link, a, b, NOW)}
    o = opps["yes_a_no_b"]  # YES on Kalshi at 40c + NO on Polymarket at 1 - 0.62 = 38c
    assert [(l.side, l.price) for l in o.legs] == [("YES", 0.40), ("NO", 0.38)]
    assert o.cost == pytest.approx(0.78)
    assert o.fees == pytest.approx(0.07 * 0.4 * 0.6 + 0.072 * 0.38 * 0.62)
    assert o.edge == pytest.approx(1 - 0.78 - o.fees)
    assert o.annualized > 0 and o.days_to_close == pytest.approx(42.0, abs=0.1)
    r = opps["no_a_yes_b"]  # NO on Kalshi at 62c + YES on Polymarket at 64c: a loser
    assert r.cost == pytest.approx(1.26) and r.edge < 0


def test_inverse_pair_buys_same_side():
    link = {"id": 2, "relation": "inverse"}
    opps = {o.direction: o for o in arb.evaluate_link(link, side("a", "kalshi", 0.30, 0.32), side("b", "polymarket", 0.60, 0.62), NOW)}
    assert set(opps) == {"yes_a_yes_b", "no_a_no_b"}
    assert opps["yes_a_yes_b"].cost == pytest.approx(0.94)
    assert opps["no_a_no_b"].cost == pytest.approx(0.70 + 0.40)


def test_missing_quotes_and_staleness():
    link = {"id": 3, "relation": "identical"}
    assert arb.evaluate_link(link, side("a", "kalshi", None, None), side("b", "polymarket", 0.5, 0.52), NOW) == []
    links = [{**link, "a": side("a", "kalshi", 0.38, 0.40), "b": side("b", "polymarket", 0.62, 0.64, age_s=600)}]
    assert arb.scan(links, NOW, max_age_s=180) == [], "stale quote on one side -> skipped"
    assert len(arb.scan(links, NOW, max_age_s=900)) == 2
