from predictarb.config import Settings
from predictarb.venues.base import VenueAdapter
from predictarb.venues.demo import DemoAdapter
from predictarb.venues.kalshi import KalshiAdapter
from predictarb.venues.polymarket import PolymarketAdapter
from predictarb.venues.polymarket_us import PolymarketUSAdapter

REGISTRY: dict[str, type[VenueAdapter]] = {
    "kalshi": KalshiAdapter,
    "polymarket": PolymarketAdapter,
    "polymarket_us": PolymarketUSAdapter,
    "demo": DemoAdapter,
}


def build_adapters(settings: Settings) -> list[VenueAdapter]:
    unknown = [v for v in settings.venue_list if v not in REGISTRY]
    if unknown:
        raise ValueError(f"unknown venues in VENUES: {unknown}; choose from {sorted(REGISTRY)}")
    return [REGISTRY[v](settings) for v in settings.venue_list]
