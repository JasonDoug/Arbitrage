"""AI analysis (design doc section 6).

The model estimates a fair probability; the *recommendation* is then computed from that
estimate against the live asks and fees by fixed rules, so the LLM never decides a trade on
its own. Several samples are combined (median fair value); disagreement between samples
lowers confidence.
"""

from __future__ import annotations

import json
import math
import statistics
from datetime import datetime, timezone
from typing import Any

from predictarb import arb

PROMPT_VERSION = "picks-v1"

EDGE_THRESHOLD = 0.05  # dollars per contract after fees (design doc 6: 5c)
MIN_CONFIDENCE = 60
RISK_LEVELS = ("LOW", "MEDIUM", "HIGH")
EDGE_TYPES = ("news_lag", "base_rate", "cross_venue", "rules_misread", "momentum", "none")

SYSTEM = """You are a careful prediction-market analyst. You estimate the probability that a
contract resolves YES, using only the market data, rules and headlines provided plus your
general knowledge (which may be out of date: say so when it matters). Be calibrated: most
liquid markets are priced roughly right, so only diverge from the market price when you can
name a concrete reason. Read the resolution rules literally. Reply with ONLY a JSON object."""

SCHEMA = """{
  "fair_value": <probability 0-1 that this resolves YES>,
  "confidence": <0-100, how sure you are of fair_value>,
  "risk_level": "LOW" | "MEDIUM" | "HIGH",
  "edge_type": "news_lag" | "base_rate" | "cross_venue" | "rules_misread" | "momentum" | "none",
  "reasoning": "<2-4 sentences>",
  "edge_summary": "<one line: what the market may be missing, or 'no edge'>",
  "key_risks": ["<short>", ...],
  "catalysts": [{"event": "<short>", "date": "<YYYY-MM-DD or empty>"}, ...]
}"""


# ---------------------------------------------------------------- candidate selection


def quant_score(m: dict[str, Any], news_count: int, now: datetime) -> float:
    """0-100 pre-score used to pick which markets get an AI look (no LLM involved)."""
    vol = m.get("volume_24h_usd") or 0.0
    liquidity = min(1.0, math.log10(max(vol, 1.0)) / 6)  # $1M/day -> 1.0
    spread = m.get("spread")
    tight = 1.0 - min(1.0, (spread if spread is not None else 0.06) / 0.06)
    days = ((m["close_ts"] - now).total_seconds() / 86400) if m.get("close_ts") else 90
    timing = 1.0 if 2 <= days <= 45 else 0.6 if days <= 120 else 0.3
    newsy = min(1.0, news_count / 5)
    mid = m.get("mid") or 0.5
    contested = 1.0 - abs(mid - 0.5) * 1.6  # favor prices with room to be wrong
    return round(100 * (0.35 * liquidity + 0.2 * tight + 0.15 * timing + 0.15 * newsy + 0.15 * max(contested, 0)), 2)


# ---------------------------------------------------------------- prompt


def build_prompt(m: dict[str, Any], history: list[dict[str, Any]], headlines: list[dict[str, Any]], now: datetime) -> str:
    def c(p: float | None) -> str:
        return "n/a" if p is None else f"{p * 100:.1f}c"

    lines = [
        f"Today: {now:%Y-%m-%d}",
        f"Venue: {m['venue']}",
        f"Market: {m['title']}" + (f" -- outcome: {m['outcome_label']}" if m.get("outcome_label") else ""),
    ]
    if m.get("event_title"):
        lines.append(f"Event: {m['event_title']}")
    lines += [
        f"Category: {m.get('category')}",
        f"Scheduled end: {m['close_ts']:%Y-%m-%d %H:%M} UTC" if m.get("close_ts") else "Scheduled end: unknown",
        f"Resolution source: {m.get('resolution_source') or 'not stated'}",
        f"YES bid {c(m.get('yes_bid'))} / ask {c(m.get('yes_ask'))}, last {c(m.get('last_price'))}, "
        f"24h change {c(m.get('change_24h'))}, 24h volume ${m.get('volume_24h_usd') or 0:,.0f}",
        "",
        "Rules:",
        (m.get("rules_text") or "not provided")[:2500],
        "",
    ]
    if history:
        lines.append("Daily closing YES price, oldest first: " + ", ".join(f"{h['t']:%m-%d} {h['close'] * 100:.0f}c" for h in history[-14:]))
    if headlines:
        lines.append("")
        lines.append("Recent matched headlines (may be only loosely related):")
        for h in headlines[:10]:
            lines.append(f"- {h['published_at']:%Y-%m-%d} {h['source']}: {h['title']}")
    else:
        lines.append("No recent matched headlines.")
    lines += ["", "Return this JSON:", SCHEMA]
    return "\n".join(lines)


# ---------------------------------------------------------------- combining samples


def _clamp(x: Any, lo: float, hi: float) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(v):
        return None
    return min(hi, max(lo, v))


def normalize_sample(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    fv = _clamp(raw.get("fair_value"), 0.0, 100.0)
    if fv is None:
        return None
    if fv > 1.0:  # model answered in percent
        fv = fv / 100
    risk = str(raw.get("risk_level", "MEDIUM")).upper()
    edge_type = str(raw.get("edge_type", "none")).lower()
    return {
        "fair_value": fv,
        "confidence": _clamp(raw.get("confidence"), 0, 100) or 50.0,
        "risk_level": risk if risk in RISK_LEVELS else "MEDIUM",
        "edge_type": edge_type if edge_type in EDGE_TYPES else "none",
        "reasoning": str(raw.get("reasoning") or "")[:1200],
        "edge_summary": str(raw.get("edge_summary") or "")[:300],
        "key_risks": [str(x)[:200] for x in (raw.get("key_risks") or []) if x][:5],
        "catalysts": [
            {"event": str(c.get("event", ""))[:200], "date": str(c.get("date", ""))[:10]}
            for c in (raw.get("catalysts") or [])
            if isinstance(c, dict) and c.get("event")
        ][:5],
    }


def combine(samples: list[dict[str, Any]]) -> dict[str, Any]:
    """Median fair value; confidence shrinks as samples disagree; text from the sample
    closest to the median."""
    fvs = [s["fair_value"] for s in samples]
    med = statistics.median(fvs)
    spread = max(fvs) - min(fvs)
    agreement = max(0.3, 1 - spread / 0.3)  # 30c of disagreement -> confidence x0.3
    conf = statistics.median(s["confidence"] for s in samples) * agreement
    rep = min(samples, key=lambda s: abs(s["fair_value"] - med))
    risks = [s["risk_level"] for s in samples]
    risk = max(set(risks), key=lambda r: (risks.count(r), RISK_LEVELS.index(r)))  # mode, ties -> riskier
    return {**rep, "fair_value": round(med, 4), "confidence": round(conf, 1), "risk_level": risk, "sample_spread": round(spread, 4)}


def recommend(m: dict[str, Any], fair: float, confidence: float, risk: str, now: datetime) -> dict[str, Any]:
    """Fixed rules (design doc 6): edge after fees >= 5c and confidence >= 60, else AVOID."""
    yes_ask = m.get("yes_ask")
    no_ask = (1 - m["yes_bid"]) if m.get("yes_bid") else None

    def fee(p: float) -> float:
        if m["venue"] == "kalshi":
            return arb.kalshi_fee_per_contract(p)
        if m["venue"] == "polymarket":
            return arb.polymarket_fee_per_contract(p, m.get("category") or "Other")
        return 0.0

    options = []
    if yes_ask and 0 < yes_ask < 1:
        options.append(("BUY_YES", yes_ask, fair - yes_ask - fee(yes_ask)))
    if no_ask and 0 < no_ask < 1:
        options.append(("BUY_NO", no_ask, (1 - fair) - no_ask - fee(no_ask)))
    if not options:
        return {"recommendation": "AVOID", "net_edge": None, "entry_price": None, "why_avoid": "no executable quote"}
    side, price, edge = max(options, key=lambda o: o[2])
    if risk == "HIGH" and edge < 0.10:
        return {"recommendation": "AVOID", "net_edge": round(edge, 4), "entry_price": price, "why_avoid": "high risk without a 10c edge"}
    if edge < EDGE_THRESHOLD:
        return {"recommendation": "AVOID", "net_edge": round(edge, 4), "entry_price": price, "why_avoid": "edge under 5c after fees"}
    if confidence < MIN_CONFIDENCE:
        return {"recommendation": "AVOID", "net_edge": round(edge, 4), "entry_price": price, "why_avoid": "confidence under 60"}
    return {"recommendation": side, "net_edge": round(edge, 4), "entry_price": price, "why_avoid": None}


def opportunity_score(net_edge: float | None, confidence: float, pre_score: float) -> float:
    """Design doc 6 weights, simplified: edge 40, confidence 25, the quant pre-score 35."""
    e = 0.0 if net_edge is None else min(1.0, max(0.0, net_edge) / 0.20)
    return round(40 * e + 25 * confidence / 100 + 0.35 * pre_score, 1)


# ---------------------------------------------------------------- match verification


MATCH_SYSTEM = """You compare two prediction-market contracts from different exchanges and
decide whether they resolve on the same fact. Read both rule sets literally: resolution
source, cutoff time and timezone, thresholds and comparators ("above" vs "at or above"),
tie/postponement handling. Reply with ONLY a JSON object."""

MATCH_SCHEMA = """{
  "relation": "identical" | "inverse" | "different",
  "confidence": <0-100>,
  "differences": ["<each material difference in resolution, short>", ...]
}
identical = YES on A pays exactly when YES on B pays. inverse = YES on A pays exactly when NO on B pays.
different = anything else, including near-matches with different thresholds, dates or sources."""


def build_match_prompt(a: dict[str, Any], b: dict[str, Any]) -> str:
    def side(label: str, m: dict[str, Any]) -> str:
        end = f"{m['close_ts']:%Y-%m-%d %H:%M} UTC" if m.get("close_ts") else "unknown"
        return "\n".join([
            f"Contract {label} ({m['venue']}): {m['title']}" + (f" -- outcome: {m['outcome_label']}" if m.get("outcome_label") else ""),
            f"Scheduled end: {end}; resolution source: {m.get('resolution_source') or 'not stated'}",
            "Rules: " + (m.get("rules_text") or "not provided")[:2000],
        ])

    return f"{side('A', a)}\n\n{side('B', b)}\n\nReturn this JSON:\n{MATCH_SCHEMA}"


def normalize_match(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    rel = str(raw.get("relation", "")).lower()
    if rel not in ("identical", "inverse", "different"):
        return None
    return {
        "relation": rel,
        "confidence": _clamp(raw.get("confidence"), 0, 100) or 50.0,
        "differences": [str(x)[:300] for x in (raw.get("differences") or []) if x][:6],
    }


def dumps(o: Any) -> str:
    return json.dumps(o, default=lambda x: x.isoformat() if isinstance(x, datetime) else str(x))


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
