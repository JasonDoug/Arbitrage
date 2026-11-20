"""Ingest worker: catalog sync, quote polling, history backfill and alert evaluation.

One process runs every enabled venue. Each venue gets its own catalog and quote loops,
so a slow or failing venue never stalls the others (design doc R3.3).
"""

from __future__ import annotations

import asyncio
import logging
import signal
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg
import redis.asyncio as aioredis

from predictarb import arb, bus, db, matching
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
            except Exception:
                log.exception("matcher failed")
            await self._sleep(self.s.match_interval_s)

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
        await pool.close()
        await redis.aclose()


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(main())


if __name__ == "__main__":
    run()
