"""Shared category taxonomy (design doc R4.4) and mapping from venue labels."""

from __future__ import annotations

import re

CATEGORIES = [
    "Politics",
    "Economics",
    "Crypto",
    "Sports",
    "Weather",
    "Tech",
    "Culture",
    "Other",
]

# Venue-provided category/tag labels -> taxonomy. Keys are lowercase.
_LABEL_MAP = {
    # Kalshi series categories
    "politics": "Politics",
    "elections": "Politics",
    "world": "Politics",
    "economics": "Economics",
    "financials": "Economics",
    "companies": "Economics",
    "crypto": "Crypto",
    "climate and weather": "Weather",
    "weather": "Weather",
    "climate": "Weather",
    "sports": "Sports",
    "science and technology": "Tech",
    "tech": "Tech",
    "technology": "Tech",
    "ai": "Tech",
    "science": "Tech",
    "entertainment": "Culture",
    "culture": "Culture",
    "pop culture": "Culture",
    "music": "Culture",
    "movies": "Culture",
    "social": "Culture",
    "mentions": "Culture",
    # Polymarket tags
    "us politics": "Politics",
    "geopolitics": "Politics",
    "global elections": "Politics",
    "trump": "Politics",
    "economy": "Economics",
    "business": "Economics",
    "finance": "Economics",
    "fed": "Economics",
    "stocks": "Economics",
    "bitcoin": "Crypto",
    "ethereum": "Crypto",
    "nba": "Sports",
    "nfl": "Sports",
    "mlb": "Sports",
    "nhl": "Sports",
    "soccer": "Sports",
    "tennis": "Sports",
    "ufc": "Sports",
    "golf": "Sports",
    "f1": "Sports",
    "esports": "Sports",
}

# Keyword fallback on the title, checked in order.
_KEYWORDS: list[tuple[str, re.Pattern[str]]] = [
    ("Crypto", re.compile(r"\b(bitcoin|btc|ethereum|eth|solana|crypto|xrp|dogecoin|stablecoin)\b", re.I)),
    ("Weather", re.compile(r"\b(temperature|rain|snow|hurricane|weather|heat ?wave|degrees|tornado)\b", re.I)),
    ("Economics", re.compile(r"\b(cpi|inflation|gdp|fed|fomc|rate cut|rate hike|unemployment|jobs report|payrolls|s&p|nasdaq|recession|tariff)\b", re.I)),
    ("Politics", re.compile(r"\b(election|president|senate|house|governor|mayor|congress|primary|nominee|prime minister|parliament|vote|supreme court)\b", re.I)),
    ("Sports", re.compile(r"\b(nba|nfl|mlb|nhl|super bowl|world cup|champions league|playoffs|finals|grand slam|match|vs\.?)\b", re.I)),
    ("Tech", re.compile(r"\b(ai|openai|gpt|apple|google|nvidia|spacex|launch|iphone|model)\b", re.I)),
    ("Culture", re.compile(r"\b(oscar|grammy|emmy|box office|album|movie|song|billboard|netflix|tiktok|celebrity)\b", re.I)),
]


def map_label(label: str | None) -> str | None:
    if not label:
        return None
    return _LABEL_MAP.get(label.strip().lower())


def categorize(labels: list[str | None], title: str = "") -> str:
    """First mapped venue label wins; otherwise fall back to title keywords."""
    for label in labels:
        mapped = map_label(label)
        if mapped:
            return mapped
    for category, pattern in _KEYWORDS:
        if pattern.search(title or ""):
            return category
    return "Other"
