"""Cross-venue market matcher (design doc section 4), text-only first pass.

Pairs each Kalshi market with the Polymarket market most likely to resolve on the same
fact, using TF-IDF cosine over title + outcome + event text, with adjustments for numbers
(thresholds, years), end-date distance and category. Every pair is only a *suggestion*:
it must be confirmed in the review queue before the arbitrage tab treats it as real.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

STOPWORDS = set(
    """a an and any are as at be been before between by can did do does during each end for from
    get has have how if in into is it its will would of on or other over per same so than that the
    their them then there these this to under up us was were what when where which who whom why
    with within yes no next most than market markets price after day days
    week month year happen happens occur occurs reach reaches""".split()
    # Comparators (above/below/over/under/more/less/least) are kept on purpose: they decide
    # which side of a threshold a contract pays on.
)
MONTHS = {
    "jan": "january", "feb": "february", "mar": "march", "apr": "april", "jun": "june", "jul": "july",
    "aug": "august", "sep": "september", "sept": "september", "oct": "october", "nov": "november", "dec": "december",
}
_TOKEN = re.compile(r"[a-z0-9]+(?:\.[0-9]+)?")
_DIGIT_WORD = re.compile(r"(\d)([a-z])")

# Venues word the same outcome differently ("Hold" vs "no change"); map common
# prediction-market phrasings onto one token before scoring.
PHRASES = [
    (re.compile(r"\bno change\b|\bunchanged\b|\bhold(s|ing)?\b|\bpause\b|\bmaintain(s|ed)?\b"), " unchanged "),
    (re.compile(r"\bbasis points?\b|\bbps?\b"), " bps "),
    (re.compile(r"\bincrease(s|d)?\b|\braise(s|d)?\b|\bhike(s|d)?\b"), " hike "),
    (re.compile(r"\bdecrease(s|d)?\b|\blower(s|ed)?\b|\bcut(s)?\b"), " cut "),
    (re.compile(r"\bdemocratic\b|\bdemocrats?\b|\bdem\b"), " democrat "),
    (re.compile(r"\brepublicans?\b|\bgop\b"), " republican "),
    (re.compile(r"\bwinner\b|\bwins\b|\bwinning\b"), " win "),
    (re.compile(r"\bfederal reserve\b|\bfomc\b"), " fed "),
    (re.compile(r"\bbtc\b"), " bitcoin "),
    (re.compile(r"\beth\b"), " ethereum "),
]


def normalize_number(tok: str) -> str:
    """'120,000' / '120k' / '$120000' style tokens arrive split; normalize what we can."""
    if tok.endswith("k") and tok[:-1].replace(".", "").isdigit():
        return str(int(float(tok[:-1]) * 1000))
    if tok.endswith("m") and tok[:-1].replace(".", "").isdigit():
        return str(int(float(tok[:-1]) * 1_000_000))
    return tok


def tokenize(text: str) -> list[str]:
    text = (text or "").lower().replace(",", "").replace("$", " ").replace("%", " pct ")
    text = _DIGIT_WORD.sub(lambda m: m.group(1) + " " + m.group(2) if m.group(2) not in "km" else m.group(0), text)
    for pattern, repl in PHRASES:
        text = pattern.sub(repl, text)
    out = []
    for tok in _TOKEN.findall(text):
        tok = normalize_number(tok)
        tok = MONTHS.get(tok, tok)
        if tok in STOPWORDS or (len(tok) < 2 and not tok.isdigit()):
            continue
        if len(tok) > 4 and tok.endswith("s") and not tok.isdigit():
            tok = tok[:-1]
        out.append(tok)
    return out


def numbers(tokens: Iterable[str]) -> set[str]:
    return {t for t in tokens if t.replace(".", "").isdigit()}


@dataclass
class Doc:
    id: str
    tokens: list[str]
    category: str
    close_ts: datetime | None
    rules_hash: str | None = None
    vec: dict[str, float] = field(default_factory=dict)


def market_doc(m: dict[str, Any]) -> Doc:
    text = " ".join(x for x in (m.get("title"), m.get("outcome_label"), m.get("event_title")) if x)
    return Doc(id=m["id"], tokens=tokenize(text), category=m.get("category") or "Other", close_ts=m.get("close_ts"),
               rules_hash=m.get("rules_hash"))


def _vectorize(docs: list[Doc], idf: dict[str, float]) -> None:
    for d in docs:
        tf = Counter(d.tokens)
        vec = {t: (1 + math.log(c)) * idf.get(t, 0.0) for t, c in tf.items()}
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        d.vec = {t: v / norm for t, v in vec.items()}


def cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(v * b.get(t, 0.0) for t, v in a.items())


@dataclass
class Suggestion:
    a: str
    b: str
    score: float
    reasons: list[str]


def score_pair(a: Doc, b: Doc) -> tuple[float, list[str]]:
    base = cosine(a.vec, b.vec)
    reasons = [f"text similarity {base:.2f}"]
    score = base
    na, nb = numbers(a.tokens), numbers(b.tokens)
    if na and nb:
        if na & nb:
            score *= 1.1
            reasons.append(f"shared numbers {sorted(na & nb)[:4]}")
        else:
            score *= 0.55  # same topic, different threshold/year: likely a different contract
            reasons.append("numbers differ")
    if a.close_ts and b.close_ts:
        days = abs((a.close_ts - b.close_ts).total_seconds()) / 86400
        if days <= 3:
            reasons.append("end dates within 3 days")
        elif days <= 14:
            score *= 0.9
            reasons.append(f"end dates {days:.0f} days apart")
        else:
            score *= 0.7
            reasons.append(f"end dates {days:.0f} days apart")
    if a.category != b.category:
        score *= 0.85
        reasons.append(f"categories differ ({a.category} / {b.category})")
    return min(score, 1.0), reasons


def suggest(
    side_a: list[dict[str, Any]],
    side_b: list[dict[str, Any]],
    min_score: float = 0.5,
    per_market: int = 1,
    exclude: set[tuple[str, str]] | None = None,
) -> list[Suggestion]:
    """Best `per_market` candidates on side B for each market on side A."""
    exclude = exclude or set()
    docs_a = [market_doc(m) for m in side_a]
    docs_b = [market_doc(m) for m in side_b]
    all_docs = docs_a + docs_b
    df: Counter[str] = Counter()
    for d in all_docs:
        df.update(set(d.tokens))
    n = len(all_docs) or 1
    idf = {t: math.log(n / c) + 1 for t, c in df.items()}
    _vectorize(all_docs, idf)

    # Inverted index on informative tokens (skip the most common ~1%) to avoid an all-pairs scan.
    common = max(n * 0.02, 10)
    index: dict[str, list[int]] = defaultdict(list)
    for i, d in enumerate(docs_b):
        for t in set(d.tokens):
            if df[t] <= common:
                index[t].append(i)

    out: list[Suggestion] = []
    for a in docs_a:
        hits: Counter[int] = Counter()
        for t in set(a.tokens):
            if df[t] <= common:
                for i in index.get(t, ()):
                    hits[i] += 1
        candidates = [i for i, c in hits.items() if c >= 2]
        scored = []
        for i in candidates:
            b = docs_b[i]
            if (a.id, b.id) in exclude:
                continue
            s, why = score_pair(a, b)
            if s >= min_score:
                scored.append((s, b.id, why))
        scored.sort(reverse=True)
        for s, bid, why in scored[:per_market]:
            out.append(Suggestion(a.id, bid, round(s, 4), why))
    return out
