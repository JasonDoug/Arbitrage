from datetime import datetime, timedelta, timezone

from predictarb.categories import categorize
from predictarb.ingest.alerts import evaluate

NOW = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)


def rule(kind, threshold, **kw):
    return {"id": 1, "market_id": "m", "kind": kind, "threshold": threshold, "enabled": True,
            "cooldown_minutes": 60, "window_minutes": 60, "last_fired_at": None, **kw}


def test_categorize():
    assert categorize(["Climate and Weather"], "x") == "Weather"
    assert categorize([None, "NBA"], "x") == "Sports"
    assert categorize([], "Will Bitcoin hit $150k?") == "Crypto"
    assert categorize([], "Will the Fed cut rates in March?") == "Economics"
    assert categorize(["unknown label"], "Something odd") == "Other"


def test_price_above_fires_on_cross_only():
    r = rule("price_above", 0.6)
    assert evaluate(r, 0.61, 0.59, NOW, title="Fed") == "Fed — YES rose to 61.0¢ (above 60.0¢)"
    assert evaluate(r, 0.65, 0.61, NOW) is None, "already above: no repeat"
    assert evaluate(r, 0.55, 0.59, NOW) is None


def test_price_below_and_cooldown():
    r = rule("price_below", 0.3, last_fired_at=NOW - timedelta(minutes=10))
    assert evaluate(r, 0.29, 0.31, NOW) is None, "in cooldown"
    r["last_fired_at"] = NOW - timedelta(minutes=61)
    assert "fell to 29.0" in evaluate(r, 0.29, 0.31, NOW)


def test_move():
    r = rule("move", 0.05, window_minutes=30)
    assert evaluate(r, 0.50, 0.49, NOW, ref_price=0.46) is None
    msg = evaluate(r, 0.40, 0.45, NOW, ref_price=0.46)
    assert "moved down 6.0¢ in 30 min" in msg
    assert evaluate(r, 0.40, 0.45, NOW, ref_price=None) is None


def test_disabled_and_missing_price():
    assert evaluate(rule("price_above", 0.5, enabled=False), 0.6, 0.4, NOW) is None
    assert evaluate(rule("price_above", 0.5), None, 0.4, NOW) is None
