"""Ingest worker: catalog sync, quote polling, history backfill and alert evaluation.

One process runs every enabled venue. Each venue gets its own catalog and quote loops,
so a slow or failing venue never stalls the others (design doc R3.3).
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg
import redis.asyncio as aioredis

import httpx

from predictarb import ai, arb, bus, db, matching, news
from predictarb.llm import LLM, BudgetExceeded, LLMError
from predictarb.config import Settings, get_settings
from predictarb.ingest import alerts
from predictarb.migrate import migrate
from predictarb.models import Quote
from predictarb.venues import VenueAdapter, build_adapters

log = logging.getLogger("predictarb.ingest")


class Ingestor:
    def __init__(self, settings: Settings, pool: asyncpg.Pool, redis: aioredis.Redis, adapters: list[VenueAdapter]):
        self.s = settings
        self.pool = pool
        self.redis = redis
        self.adapters = {a.venue_id: a for a in adapters}
        self.last_quotes: dict[str, Quote] = {}
        self.catalog_ready = {v: asyncio.Event() for v in self.adapters}
        self.rules: dict[str, list[dict[str, Any]]] = {}
        self.rules_loaded_at = datetime.min.replace(tzinfo=timezone.utc)
        self.titles: dict[str, str] = {}
        self.backfill_failed: dict[str, datetime] = {}
        self.llm = LLM(settings, pool)
        self._stop = asyncio.Event()

    # ------------------------------------------------------------ catalog

    async def sync_catalog(self, adapter: VenueAdapter) -> int:
        started = datetime.now(timezone.utc)
        adapter.keep = await db.kept_ids(self.pool, adapter.venue_id)
        markets = await adapter.fetch_markets()
        new = await db.upsert_markets(self.pool, markets)
        closed = await db.mark_unseen_closed(self.pool, adapter.venue_id, started) if markets else 0
        for m in markets:
            self.titles[m.id] = m.title if not m.outcome_label else f"{m.title} — {m.outcome_label}"
            if m.quote:
                self.last_quotes[m.id] = m.quote
        await db.set_venue_status(self.pool, adapter.venue_id, True)
        log.info("%s catalog: %d markets (%d new, %d no longer listed)", adapter.venue_id, len(markets), new, closed)
        return len(markets)

    async def catalog_loop(self, adapter: VenueAdapter) -> None:
        while not self._stop.is_set():
            try:
                await self.sync_catalog(adapter)
                self.catalog_ready[adapter.venue_id].set()
                wait = self.s.catalog_interval_s
            except Exception as e:  # keep the loop alive; venue may recover
                log.exception("%s catalog sync failed", adapter.venue_id)
                await db.set_venue_status(self.pool, adapter.venue_id, False, str(e))
                wait = 60
            await self._sleep(wait)

    # ------------------------------------------------------------ quotes

    def changed_quotes(self, quotes: list[Quote]) -> list[Quote]:
        changed = []
        for q in quotes:
            if q.id not in self.titles:  # not in our catalog (e.g. filtered out)
                continue
            if not q.same_prices(self.last_quotes.get(q.id)):
                changed.append(q)
        return changed

    async def poll_quotes(self, adapter: VenueAdapter) -> int:
        quotes = await adapter.fetch_quotes()
        changed = self.changed_quotes(quotes)
        if changed:
            await db.apply_quotes(self.pool, changed)
            await bus.publish(
                self.redis,
                bus.QUOTES,
                [
                    {
                        "id": q.id,
                        "yes_bid": q.yes_bid,
                        "yes_ask": q.yes_ask,
                        "last_price": q.last_price,
                        "mid": q.mid,
                        "spread": q.spread,
                        "change_24h": q.change_24h,
                        "ts": q.ts,
                    }
                    for q in changed
                ],
            )
            await self.check_alerts(changed)
        for q in changed:
            self.last_quotes[q.id] = q
        await db.set_venue_status(self.pool, adapter.venue_id, True)
        return len(changed)

    async def quote_loop(self, adapter: VenueAdapter) -> None:
        await self.catalog_ready[adapter.venue_id].wait()
        while not self._stop.is_set():
            await self._sleep(self.s.quote_interval_s)
            try:
                n = await self.poll_quotes(adapter)
                log.debug("%s quotes: %d changed", adapter.venue_id, n)
            except Exception as e:
                log.warning("%s quote poll failed: %s", adapter.venue_id, e)
                await db.set_venue_status(self.pool, adapter.venue_id, False, str(e))

    # ------------------------------------------------------------ alerts

    async def check_alerts(self, quotes: list[Quote]) -> None:
        now = datetime.now(timezone.utc)
        if now - self.rules_loaded_at > timedelta(seconds=15):
            self.rules = await db.enabled_rules_by_market(self.pool)
            self.rules_loaded_at = now
        for q in quotes:
            for rule in self.rules.get(q.id, []):
                prev = self.last_quotes.get(q.id)
                ref = None
                if rule["kind"] == "move":
                    ref = await db.price_at(self.pool, q.id, now - timedelta(minutes=rule["window_minutes"]))
                msg = alerts.evaluate(rule, q.mid, prev.mid if prev else None, now, ref, self.titles.get(q.id, ""))
                if msg:
                    note = await db.fire_rule(self.pool, rule, msg)
                    rule["last_fired_at"] = now
                    await bus.publish(self.redis, bus.NOTIFICATIONS, note)
                    log.info("alert fired: %s", msg)

    # ------------------------------------------------------------ matching & arbitrage

    async def run_matcher(self) -> int:
        side_a, side_b, existing = await db.matcher_inputs(self.pool)
        if not side_a or not side_b:
            return 0
        suggestions = await asyncio.to_thread(
            matching.suggest, side_a, side_b, self.s.match_min_score, 1, existing
        )
        n = await db.insert_suggestions(self.pool, suggestions)
        log.info("matcher: %d kalshi x %d polymarket markets -> %d new suggested links", len(side_a), len(side_b), n)
        return n

    async def match_loop(self) -> None:
        if not {"kalshi", "polymarket"} <= set(self.adapters):
            return
        await asyncio.gather(self.catalog_ready["kalshi"].wait(), self.catalog_ready["polymarket"].wait())
        while not self._stop.is_set():
            try:
                await self.run_matcher()
                await self.verify_links()
            except Exception:
                log.exception("matcher failed")
            await self._sleep(self.s.match_interval_s)

    async def verify_links(self) -> int:
        """Ask the LLM whether top suggested pairs resolve on the same fact. Advisory only:
        a person still confirms every link."""
        if not self.llm.configured or self.s.match_llm_per_run <= 0:
            return 0
        done = 0
        for link in await db.links_needing_llm(self.pool, self.s.match_llm_per_run):
            try:
                raw, model = await self.llm.chat_json("match", ai.MATCH_SYSTEM, ai.build_match_prompt(link["a"], link["b"]), 0.1, 600)
            except BudgetExceeded:
                break
            except LLMError as e:
                log.warning("match check %s failed: %s", link["id"], e)
                continue
            verdict = ai.normalize_match(raw)
            if verdict:
                await db.set_link_llm(self.pool, link["id"], verdict["relation"], verdict["confidence"], verdict["differences"], model)
                done += 1
        if done:
            log.info("LLM checked %d suggested links", done)
        return done

    async def scan_arb(self) -> int:
        links = await db.links_for_arb(self.pool, include_suggested=False)
        found = arb.scan(links, max_age_s=self.s.arb_max_quote_age_s, pm_rates=self.s.polymarket_fee_rates)
        await db.record_arb(self.pool, [o for _, o in found], {l["id"] for l in links})
        return sum(1 for _, o in found if o.edge > 0)

    async def arb_loop(self) -> None:
        await asyncio.gather(*(e.wait() for e in self.catalog_ready.values()))
        while not self._stop.is_set():
            try:
                await self.scan_arb()
            except Exception:
                log.exception("arb scan failed")
            await self._sleep(self.s.arb_interval_s)

    # ------------------------------------------------------------ AI analysis

    async def analyze_market(self, m: dict[str, Any], pre_score: float, batch_id: int | None) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        history = await db.daily_closes(self.pool, m["id"])
        heads = await db.headlines(self.pool, m["id"])
        prompt = ai.build_prompt(m, history, heads, now)
        samples, models, errors = [], set(), []
        for _ in range(max(1, self.s.ai_samples)):
            try:
                raw, model = await self.llm.chat_json("analysis", ai.SYSTEM, prompt, 0.7, 1200)
            except BudgetExceeded:
                raise
            except LLMError as e:
                errors.append(str(e))
                continue
            sample = ai.normalize_sample(raw)
            if sample:
                samples.append(sample)
                models.add(model)
            else:
                errors.append("reply missing fair_value")
        inputs = {
            "yes_bid": m.get("yes_bid"), "yes_ask": m.get("yes_ask"), "last_price": m.get("last_price"),
            "quote_ts": m.get("quote_ts"), "news_ids": [h["id"] for h in heads], "pre_score": pre_score,
        }
        base = {"batch_id": batch_id, "market_id": m["id"], "inputs": inputs, "prompt_version": ai.PROMPT_VERSION,
                "model": ", ".join(sorted(models)) or None}
        if not samples:
            row = {**base, "status": "failed", "error": "; ".join(errors)[:500] or "no usable reply"}
        else:
            c = ai.combine(samples)
            rec = ai.recommend(m, c["fair_value"], c["confidence"], c["risk_level"], now)
            row = {
                **base, "status": "ok", "recommendation": rec["recommendation"], "fair_value": c["fair_value"],
                "confidence": c["confidence"], "risk_level": c["risk_level"], "net_edge": rec["net_edge"],
                "entry_price": rec["entry_price"],
                "opportunity_score": ai.opportunity_score(rec["net_edge"], c["confidence"], pre_score),
                "edge_type": c["edge_type"], "reasoning": c["reasoning"],
                "edge_summary": c["edge_summary"] if rec["recommendation"] != "AVOID" or not rec["why_avoid"]
                else f"{c['edge_summary']} (Avoid: {rec['why_avoid']})".strip(),
                "key_risks": c["key_risks"], "catalysts": c["catalysts"],
                "samples": [{"fair_value": s_["fair_value"], "confidence": s_["confidence"]} for s_ in samples],
            }
        row["inputs"] = json.loads(ai.dumps(row["inputs"]))
        await db.insert_analysis(self.pool, row)
        return row

    async def run_batch(self, batch_id: int) -> None:
        batch = await db.get_batch(self.pool, batch_id)
        if batch is None or batch["status"] not in ("queued",):
            return
        await db.update_batch(self.pool, batch_id, status="running")
        try:
            now = datetime.now(timezone.utc)
            if batch["kind"] == "picks":
                cands = await db.ai_candidates(self.pool)
                scored = sorted(cands, key=lambda c: ai.quant_score(c, c["news_count"], now), reverse=True)[: self.s.ai_picks_n]
                await db.update_batch(self.pool, batch_id, market_ids=[c["id"] for c in scored])
                targets = [(c, ai.quant_score(c, c["news_count"], now)) for c in scored]
            else:
                targets = []
                for mid in batch["market_ids"]:
                    m = await db.market_full(self.pool, mid)
                    if m:
                        targets.append((m, ai.quant_score(m, len(await db.headlines(self.pool, mid, 2)), now)))
            for i, (m, pre) in enumerate(targets, 1):
                await self.analyze_market(m, pre, batch_id)
                await db.update_batch(self.pool, batch_id, done=i)
            await db.update_batch(self.pool, batch_id, status="done", finished_at=datetime.now(timezone.utc))
        except BudgetExceeded as e:
            await db.update_batch(self.pool, batch_id, status="failed", error=str(e), finished_at=datetime.now(timezone.utc))
        except Exception as e:
            log.exception("AI batch %s failed", batch_id)
            await db.update_batch(self.pool, batch_id, status="failed", error=str(e)[:500], finished_at=datetime.now(timezone.utc))

    async def ai_loop(self) -> None:
        # Batches interrupted by a restart: fail the running one, pick up queued ones.
        await self.pool.execute(
            "UPDATE ai_batch SET status='failed', error='interrupted by worker restart', finished_at=now() WHERE status='running'"
        )
        for row in await self.pool.fetch("SELECT id FROM ai_batch WHERE status='queued' ORDER BY id"):
            await self.run_batch(row["id"])
        last_id: str | None = None
        while not self._stop.is_set():
            try:
                if last_id is None:
                    last_id = await bus.latest_id(self.redis, bus.AI_REQUESTS)
                msgs = await bus.read(self.redis, {bus.AI_REQUESTS: last_id}, block_ms=5000)
            except Exception as e:
                log.warning("AI request read failed: %s", e)
                await self._sleep(5)
                continue
            for _, msg_id, payload in msgs:
                last_id = msg_id
                await self.run_batch(int(payload["batch_id"]))

    # ------------------------------------------------------------ news

    async def poll_news(self, client: httpx.AsyncClient) -> int:
        feeds = self.s.news_feeds or news.DEFAULT_FEEDS
        items, errors = await news.fetch_feeds(feeds, client)
        for name, err in errors.items():
            log.warning("news feed %s failed: %s", name, err)
        new = await db.insert_news(self.pool, items)
        if not new:
            return 0
        index = news.MarketIndex.build(await db.markets_for_news(self.pool))
        watched = await db.watched_ids(self.pool)
        links = []
        for nid, item in new:
            for mid, score, terms in index.match(item, self.s.news_min_score):
                links.append((nid, mid, score, terms))
                if mid in watched and score >= self.s.news_alert_score:
                    note = await db.notify(self.pool, mid, "news", f"{item.source}: {item.title}")
                    await bus.publish(self.redis, bus.NOTIFICATIONS, note)
        await db.insert_news_links(self.pool, links)
        log.info("news: %d new items from %d feeds, %d market matches", len(new), len(feeds) - len(errors), len(links))
        return len(new)

    async def news_loop(self) -> None:
        await asyncio.gather(*(e.wait() for e in self.catalog_ready.values()))
        headers = {"User-Agent": "Mozilla/5.0 (compatible; predictarb/0.1; personal news reader)"}
        async with httpx.AsyncClient(timeout=20, headers=headers) as client:
            while not self._stop.is_set():
                try:
                    await self.poll_news(client)
                    await db.prune_news(self.pool, self.s.news_retention_days)
                except Exception:
                    log.exception("news poll failed")
                await self._sleep(self.s.news_interval_s)

    # ------------------------------------------------------------ history backfill

    async def backfill_one(self, row: dict[str, Any]) -> int:
        adapter = self.adapters.get(row["venue"])
        if adapter is None:
            return 0
        points = await adapter.fetch_history(row, self.s.backfill_days)
        return await db.insert_history(self.pool, row["id"], points)

    async def backfill_loop(self) -> None:
        await asyncio.gather(*(e.wait() for e in self.catalog_ready.values()))
        while not self._stop.is_set():
            rows = await db.markets_needing_backfill(self.pool, self.s.backfill_top_n)
            now = datetime.now(timezone.utc)
            done = 0
            for row in rows:
                failed_at = self.backfill_failed.get(row["id"])
                if failed_at and now - failed_at < timedelta(hours=1):
                    continue
                try:
                    await self.backfill_one(row)
                    done += 1
                except Exception as e:
                    self.backfill_failed[row["id"]] = now
                    log.warning("backfill %s failed: %s", row["id"], e)
                if self._stop.is_set():
                    return
            if done:
                log.info("backfilled history for %d markets", done)
            await self._sleep(300)

    async def backfill_request_loop(self) -> None:
        """On-demand backfill when the UI opens a market with no history."""
        last_id: str | None = None
        while not self._stop.is_set():
            try:
                if last_id is None:
                    last_id = await bus.latest_id(self.redis, bus.BACKFILL_REQUESTS)
                msgs = await bus.read(self.redis, {bus.BACKFILL_REQUESTS: last_id}, block_ms=5000)
            except Exception as e:
                log.warning("backfill request read failed: %s", e)
                await self._sleep(5)
                continue
            for _, msg_id, payload in msgs:
                last_id = msg_id
                row = await db.get_market_row(self.pool, payload.get("market_id", ""))
                if row and row.get("backfilled_at") is None:
                    try:
                        n = await self.backfill_one(row)
                        log.info("on-demand backfill %s: %d points", row["id"], n)
                    except Exception as e:
                        log.warning("on-demand backfill %s failed: %s", row["id"], e)

    # ------------------------------------------------------------ lifecycle

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        for a in self.adapters.values():
            await db.upsert_venue(self.pool, a.venue_id, a.venue_name)
        tasks = []
        for a in self.adapters.values():
            tasks.append(asyncio.create_task(self.catalog_loop(a), name=f"catalog:{a.venue_id}"))
            tasks.append(asyncio.create_task(self.quote_loop(a), name=f"quotes:{a.venue_id}"))
        tasks.append(asyncio.create_task(self.backfill_loop(), name="backfill"))
        tasks.append(asyncio.create_task(self.match_loop(), name="matcher"))
        tasks.append(asyncio.create_task(self.arb_loop(), name="arb"))
        tasks.append(asyncio.create_task(self.news_loop(), name="news"))
        tasks.append(asyncio.create_task(self.ai_loop(), name="ai"))
        tasks.append(asyncio.create_task(self.backfill_request_loop(), name="backfill-requests"))
        await self._stop.wait()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def main() -> None:
    s = get_settings()
    await migrate(s.database_url)
    pool = await db.create_pool(s.database_url)
    redis = bus.connect(s.redis_url)
    adapters = build_adapters(s)
    ing = Ingestor(s, pool, redis, adapters)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, ing.stop)
    log.info("ingest starting for venues: %s", ", ".join(ing.adapters))
    try:
        await ing.run()
    finally:
        for a in adapters:
            await a.aclose()
        await ing.llm.aclose()
        await pool.close()
        await redis.aclose()


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(main())


if __name__ == "__main__":
    run()
