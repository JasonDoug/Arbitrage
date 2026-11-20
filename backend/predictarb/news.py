"""News pipeline (design doc section 9): free RSS feeds -> dedupe -> match to markets.

Matching is keyword-based for now: TF-IDF cosine between a headline (+ summary) and each
market's title/outcome/event text, using the same tokenizer and synonyms as the cross-venue
matcher, and requiring at least two shared informative terms. LLM impact scoring comes later.
"""

from __future__ import annotations

import asyncio
import calendar
import hashlib
import logging
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import feedparser
import httpx

from predictarb.matching import tokenize

log = logging.getLogger(__name__)

# Free feeds, no keys. Override with NEWS_FEEDS (JSON object of name -> URL).
DEFAULT_FEEDS: dict[str, str] = {
    "BBC World": "https://feeds.bbci.co.uk/news/world/rss.xml",
    "BBC Business": "https://feeds.bbci.co.uk/news/business/rss.xml",
    "NPR News": "https://feeds.npr.org/1001/rss.xml",
    "NPR Politics": "https://feeds.npr.org/1014/rss.xml",
    "Guardian US Politics": "https://www.theguardian.com/us-news/us-politics/rss",
    "Guardian World": "https://www.theguardian.com/world/rss",
    "Al Jazeera": "https://www.aljazeera.com/xml/rss/all.xml",
    "Politico": "https://rss.politico.com/politics-news.xml",
    "NYT Politics": "https://rss.nytimes.com/services/xml/rss/nyt/Politics.xml",
    "NYT Business": "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml",
    "CNBC Top News": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114",
    "Federal Reserve": "https://www.federalreserve.gov/feeds/press_all.xml",
    "BLS Releases": "https://www.bls.gov/feed/bls_latest.rss",
    "CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "ESPN": "https://www.espn.com/espn/rss/news",
}

MAX_AGE = timedelta(days=3)  # ignore older entries when a feed is first read
_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


@dataclass
class NewsItem:
    url: str
    source: str
    title: str
    summary: str | None
    published_at: datetime
    title_key: str = ""

    def __post_init__(self) -> None:
        if not self.title_key:
            self.title_key = title_key(self.title)


def title_key(title: str) -> str:
    norm = " ".join(re.findall(r"[a-z0-9]+", title.lower()))
    return hashlib.sha1(norm.encode()).hexdigest()[:20]


def clean(text: str | None, limit: int = 400) -> str | None:
    if not text:
        return None
    t = _WS.sub(" ", _TAGS.sub(" ", text)).strip()
    return (t[: limit - 1] + "…") if len(t) > limit else t or None


def parse_feed(source: str, content: bytes, now: datetime | None = None) -> list[NewsItem]:
    now = now or datetime.now(timezone.utc)
    parsed = feedparser.parse(content)
    out = []
    for e in parsed.entries:
        url = e.get("link")
        title = clean(e.get("title"), 300)
        if not url or not title:
            continue
        stamp = e.get("published_parsed") or e.get("updated_parsed")
        published = datetime.fromtimestamp(calendar.timegm(stamp), tz=timezone.utc) if stamp else now
        if published > now + timedelta(minutes=5):
            published = now  # feeds sometimes post-date entries
        if now - published > MAX_AGE:
            continue
        out.append(NewsItem(url=url, source=source, title=title, summary=clean(e.get("summary")), published_at=published))
    return out


async def fetch_feeds(feeds: dict[str, str], client: httpx.AsyncClient) -> tuple[list[NewsItem], dict[str, str]]:
    """Fetch all feeds concurrently. Returns (items, {feed: error}) - one bad feed never stops the rest."""
    errors: dict[str, str] = {}

    async def one(name: str, url: str) -> list[NewsItem]:
        try:
            r = await client.get(url, follow_redirects=True)
            r.raise_for_status()
            return parse_feed(name, r.content)
        except Exception as e:  # network errors, 403s, malformed XML
            errors[name] = str(e)[:200]
            return []

    results = await asyncio.gather(*(one(n, u) for n, u in feeds.items()))
    return dedupe([i for batch in results for i in batch]), errors


def dedupe(items: list[NewsItem]) -> list[NewsItem]:
    """Same story syndicated across feeds (same URL or same headline): keep the earliest."""
    keys: set[str] = set()
    out = []
    for i in sorted(items, key=lambda x: x.published_at):
        if i.url in keys or i.title_key in keys:
            continue
        keys.update((i.url, i.title_key))
        out.append(i)
    return out


# ---------------------------------------------------------------- matching to markets


@dataclass
class MarketIndex:
    """TF-IDF index over open markets for headline matching."""

    idf: dict[str, float] = field(default_factory=dict)
    vecs: dict[str, dict[str, float]] = field(default_factory=dict)
    postings: dict[str, list[str]] = field(default_factory=dict)

    @classmethod
    def build(cls, markets: list[dict[str, Any]]) -> "MarketIndex":
        docs = {}
        for m in markets:
            text = " ".join(x for x in (m.get("title"), m.get("outcome_label"), m.get("event_title")) if x)
            docs[m["id"]] = tokenize(text)
        n = len(docs) or 1
        df: Counter[str] = Counter()
        for toks in docs.values():
            df.update(set(toks))
        idf = {t: math.log(n / c) + 1 for t, c in df.items()}
        common = max(n * 0.03, 15)
        postings: dict[str, list[str]] = defaultdict(list)
        vecs = {}
        for mid, toks in docs.items():
            vecs[mid] = _unit({t: (1 + math.log(c)) * idf[t] for t, c in Counter(toks).items()})
            for t in set(toks):
                # Numbers count when they're distinctive (thresholds like 120000); short ones
                # (31, 25) are too generic. Common ones like years are dropped by `common`.
                if df[t] <= common and (not t.replace(".", "").isdigit() or len(t) >= 3):
                    postings[t].append(mid)
        return cls(idf=idf, vecs=vecs, postings=dict(postings))

    def match(self, item: NewsItem, min_score: float = 0.25, top: int = 5) -> list[tuple[str, float, list[str]]]:
        toks = tokenize(f"{item.title} {item.summary or ''}")
        tf = Counter(t for t in toks if t in self.idf)
        if not tf:
            return []
        vec = _unit({t: (1 + math.log(c)) * self.idf[t] for t, c in tf.items()})
        hits: Counter[str] = Counter()
        for t in tf:
            for mid in self.postings.get(t, ()):
                hits[mid] += 1
        out = []
        for mid, shared in hits.items():
            if shared < 2:
                continue
            mv = self.vecs[mid]
            score = sum(v * mv.get(t, 0.0) for t, v in vec.items())
            if score >= min_score:
                terms = sorted((t for t in tf if t in mv and not t.isdigit()), key=lambda t: -self.idf[t])[:5]
                out.append((mid, round(score, 4), terms))
        out.sort(key=lambda x: -x[1])
        return out[:top]


def _unit(vec: dict[str, float]) -> dict[str, float]:
    norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
    return {t: v / norm for t, v in vec.items()}
