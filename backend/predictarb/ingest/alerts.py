"""Price alert rule evaluation (pure functions, easy to test)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any


def in_cooldown(rule: dict[str, Any], now: datetime) -> bool:
    fired = rule.get("last_fired_at")
    return fired is not None and now - fired < timedelta(minutes=rule.get("cooldown_minutes") or 0)


def evaluate(
    rule: dict[str, Any],
    price: float | None,
    prev_price: float | None,
    now: datetime,
    ref_price: float | None = None,
    title: str = "",
) -> str | None:
    """Return an alert message if the rule fires, else None.

    price_above / price_below fire on a *crossing* (previous price on the other side),
    so a rule doesn't re-fire every poll while the price sits beyond the threshold.
    'move' fires when |price - price window_minutes ago| >= threshold.
    """
    if price is None or not rule.get("enabled", True) or in_cooldown(rule, now):
        return None
    t = float(rule["threshold"])
    kind = rule["kind"]
    name = title or rule["market_id"]
    if kind == "price_above":
        if price >= t and (prev_price is None or prev_price < t):
            return f"{name} — YES rose to {price * 100:.1f}¢ (above {t * 100:.1f}¢)"
    elif kind == "price_below":
        if price <= t and (prev_price is None or prev_price > t):
            return f"{name} — YES fell to {price * 100:.1f}¢ (below {t * 100:.1f}¢)"
    elif kind == "move":
        if ref_price is not None and abs(price - ref_price) >= t:
            direction = "up" if price > ref_price else "down"
            return (
                f"{name} — moved {direction} {abs(price - ref_price) * 100:.1f}¢ in "
                f"{rule.get('window_minutes', 60)} min (now {price * 100:.1f}¢)"
            )
    return None
