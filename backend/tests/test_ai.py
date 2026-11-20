import json
import os
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from predictarb import ai, bus, db
from predictarb.config import Settings
from predictarb.ingest.worker import Ingestor
from predictarb.llm import LLM, BudgetExceeded, LLMError, extract_json
from predictarb.models import Market, Quote

NOW = datetime.now(timezone.utc)
LOCAL = "http://llm.test/v1"


def test_extract_json_variants():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('```json\n{"a": 2}\n```') == {"a": 2}
    assert extract_json('<think>hmm {not json}</think>Sure! {"a": {"b": 3}} hope that helps') == {"a": {"b": 3}}
    with pytest.raises(LLMError):
        extract_json("no json here")


def test_normalize_sample_and_combine():
    assert ai.normalize_sample({"fair_value": 62, "confidence": 140, "risk_level": "low"})["fair_value"] == 0.62
    assert ai.normalize_sample({"fair_value": 0.4})["confidence"] == 50.0
    assert ai.normalize_sample({"confidence": 80}) is None
    assert ai.normalize_sample("nope") is None
    s = [ai.normalize_sample({"fair_value": v, "confidence": 80, "risk_level": r, "reasoning": f"r{v}"})
         for v, r in ((0.60, "LOW"), (0.62, "MEDIUM"), (0.70, "MEDIUM"))]
    c = ai.combine(s)
    assert c["fair_value"] == 0.62 and c["reasoning"] == "r0.62" and c["risk_level"] == "MEDIUM"
    assert c["confidence"] == pytest.approx(80 * (1 - 0.10 / 0.3), abs=0.1), "10c of disagreement shrinks confidence"


def market(bid=0.40, ask=0.42, venue="kalshi", cat="Economics"):
    return {"id": f"{venue}:X", "venue": venue, "category": cat, "yes_bid": bid, "yes_ask": ask,
            "close_ts": NOW + timedelta(days=20)}


def test_recommend_rules():
    m = market()
    assert ai.recommend(m, 0.55, 70, "LOW", NOW)["recommendation"] == "BUY_YES"  # 55 - 42 - fee ~1.7 = 11c
    buy_no = ai.recommend(m, 0.25, 70, "LOW", NOW)
    assert buy_no["recommendation"] == "BUY_NO" and buy_no["entry_price"] == 0.60
    assert ai.recommend(m, 0.45, 90, "LOW", NOW)["why_avoid"] == "edge under 5c after fees"
    assert ai.recommend(m, 0.55, 50, "LOW", NOW)["why_avoid"] == "confidence under 60"
    assert ai.recommend(m, 0.50, 90, "HIGH", NOW)["why_avoid"] == "high risk without a 10c edge"  # ~6c edge
    assert ai.recommend(m, 0.55, 90, "HIGH", NOW)["recommendation"] == "BUY_YES", "~11c clears the high-risk bar"
    assert ai.recommend(market(None, None), 0.9, 90, "LOW", NOW)["why_avoid"] == "no executable quote"


def test_quant_score_bounds():
    m = {**market(), "volume_24h_usd": 1e6, "spread": 0.01, "mid": 0.5}
    assert 0 <= ai.quant_score(m, 5, NOW) <= 100
    assert ai.quant_score(m, 5, NOW) > ai.quant_score({**m, "volume_24h_usd": 100, "spread": 0.06}, 0, NOW)


def test_normalize_match():
    assert ai.normalize_match({"relation": "Identical", "confidence": 90, "differences": ["x"]})["relation"] == "identical"
    assert ai.normalize_match({"relation": "maybe"}) is None


def reply(content: str, model: str = "test-model") -> httpx.Response:
    return httpx.Response(200, json={"model": model, "choices": [{"message": {"content": content}}],
                                     "usage": {"prompt_tokens": 10, "completion_tokens": 5}})


@respx.mock
async def test_llm_fallback_and_retry():
    s = Settings(openrouter_api_key="sk-test", local_llm_url=LOCAL, local_llm_model="qwen", llm_order="openrouter,local")
    respx.post("https://openrouter.ai/api/v1/chat/completions").respond(429, text="rate limited")
    local = respx.post(f"{LOCAL}/chat/completions")
    local.side_effect = [reply("I think it's about 60%"), reply('{"fair_value": 0.6}', "qwen-7b")]
    llm = LLM(s)
    data, model = await llm.chat_json("t", "sys", "user")
    assert data == {"fair_value": 0.6} and model == "local:qwen-7b"
    assert len(local.calls) == 2, "bad JSON gets one retry on the same provider"
    sent = json.loads(local.calls[0].request.content)
    assert sent["model"] == "qwen" and sent["messages"][0]["role"] == "system"
    await llm.aclose()


async def test_llm_not_configured():
    with pytest.raises(LLMError, match="no LLM configured"):
        await LLM(Settings(openrouter_api_key=None, local_llm_url=None)).chat_json("t", "s", "u")


ANALYSIS = {"fair_value": 0.55, "confidence": 75, "risk_level": "LOW", "edge_type": "news_lag",
            "reasoning": "Recent data points to a hold.", "edge_summary": "Market lags the jobs report.",
            "key_risks": ["Surprise CPI"], "catalysts": [{"event": "FOMC decision", "date": "2026-12-09"}]}


async def test_picks_and_match_check_end_to_end(clean_db):
    settings = Settings(database_url=clean_db, redis_url=os.environ["REDIS_URL"], local_llm_url=LOCAL,
                        llm_order="local", ai_samples=2, ai_picks_n=2, openrouter_api_key=None)
    pool = await db.create_pool(clean_db)
    redis = bus.connect(settings.redis_url)
    try:
        for v in ("kalshi", "polymarket"):
            await db.upsert_venue(pool, v, v)
        mk = lambda v, i, title, vol: Market(venue=v, venue_market_id=i, title=title, category="Economics",
                                              close_ts=NOW + timedelta(days=20), rules_text=f"rules {i}",
                                              quote=Quote(venue=v, venue_market_id=i, yes_bid=0.40, yes_ask=0.42,
                                                          last_price=0.41, volume_24h_usd=vol, ts=NOW))
        await db.upsert_markets(pool, [mk("kalshi", "A", "Fed decision in December 2026", 900_000),
                                       mk("kalshi", "B", "CPI above 3% in December", 50_000),
                                       mk("kalshi", "C", "Tiny market", 10)])
        await db.upsert_markets(pool, [mk("polymarket", "1", "Will the Fed hold rates in December 2026?", 500_000)])
        await pool.execute(
            "INSERT INTO market_link (market_a, market_b, score) VALUES ('kalshi:A', 'polymarket:1', 0.8)")
    finally:
        await pool.close()
        await redis.aclose()

    from predictarb.api import main as api_main

    api_main.get_settings.cache_clear()
    os.environ["LOCAL_LLM_URL"] = LOCAL
    try:
        with TestClient(api_main.app) as client:
            assert client.get("/api/ai/picks").json()["configured"] is True
            run = client.post("/api/ai/picks/run").json()
            assert run["started"] is True and run["batch"]["status"] == "queued"
            assert client.post("/api/ai/picks/run").json()["started"] is False, "no double-run while queued"
            batch_id = run["batch"]["id"]
    finally:
        os.environ.pop("LOCAL_LLM_URL")
        api_main.get_settings.cache_clear()

    pool = await db.create_pool(clean_db)
    redis = bus.connect(settings.redis_url)
    ing = Ingestor(settings, pool, redis, [])
    try:
        with respx.mock:
            route = respx.post(f"{LOCAL}/chat/completions")
            route.side_effect = [reply(json.dumps(ANALYSIS))] * 4 + [
                reply('{"relation": "identical", "confidence": 88, "differences": ["Kalshi cutoff is 2pm ET"]}')]
            await ing.run_batch(batch_id)
            assert len(route.calls) == 4, "2 markets x 2 samples"
            assert await ing.verify_links() == 1
        batch = await db.get_batch(pool, batch_id)
        assert batch["status"] == "done" and batch["done"] == 2
        assert batch["market_ids"][0] == "kalshi:A", "highest pre-score first"
        rows = await db.batch_analyses(pool, batch_id)
        assert rows[0]["market_id"] == "polymarket:1", "lower fee -> bigger edge -> ranked first"
        top = next(r for r in rows if r["market_id"] == "kalshi:A")
        assert top["recommendation"] == "BUY_YES" and top["fair_value"] == 0.55 and top["entry_price"] == 0.42
        assert top["net_edge"] == pytest.approx(0.55 - 0.42 - 0.07 * 0.42 * 0.58, abs=1e-4)
        assert top["catalysts"][0]["event"] == "FOMC decision" and top["model"] == "local:test-model"
        assert len(top["samples"]) == 2 and top["inputs"]["yes_ask"] == 0.42
        link = (await db.list_links(pool, "suggested"))[0][0]
        assert link["llm_relation"] == "identical" and link["llm_differences"] == ["Kalshi cutoff is 2pm ET"]
        usage = await db.llm_usage_today(pool)
        assert usage["calls"] == 5 and usage["ok"] == 5

        # Daily budget stops further work.
        ing.s.ai_max_calls_per_day = 5
        with pytest.raises(BudgetExceeded):
            await ing.llm.chat_json("t", "s", "u")
    finally:
        await ing.llm.aclose()
        await pool.close()
        await redis.aclose()

    with TestClient(api_main.app) as client:
        picks = client.get("/api/ai/picks").json()
        assert picks["batch"]["status"] == "done" and len(picks["items"]) == 2
        one = client.get("/api/ai/analysis/kalshi:A").json()["analysis"]
        assert one["reasoning"] == "Recent data points to a hold."
        assert client.post("/api/ai/analyze/kalshi:A").status_code == 409, "API process has no LLM configured"
