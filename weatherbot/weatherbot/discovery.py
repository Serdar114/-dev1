"""
Discover active Polymarket temperature/weather markets via Gamma and CLOB APIs.
"""
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"

WEATHER_TAGS = {"weather", "temperature", "climate"}
WEATHER_KEYWORDS = [
    "temperature", "high temp", "low temp", "daily high", "daily low",
    "degrees", "fahrenheit", "celsius", "°f", "°c", "heat", "cold snap",
]
PRECIP_KEYWORDS = [
    "rain", "rainfall", "precipitation", "snow", "snowfall", "inches of rain",
    "mm of rain", "wet", "flood",
]

TEMP_MARKET_TYPES = {"daily_high_temperature", "daily_low_temperature"}


@dataclass
class RawMarket:
    market_id: str
    event_id: Optional[str]
    question: str
    slug: Optional[str]
    close_time: Optional[str]
    outcomes: list[str]
    token_ids: list[str]
    category: Optional[str]
    tags: list[str]
    volume: Optional[float]
    liquidity: Optional[float]
    active: bool
    raw: dict[str, Any] = field(default_factory=dict)


def _get(url: str, params: dict, timeout: int = 15, retries: int = 3, backoff: float = 2.0) -> Optional[dict | list]:
    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            logger.warning("GET %s attempt %d/%d failed: %s", url, attempt + 1, retries, exc)
            if attempt < retries - 1:
                time.sleep(backoff * (2 ** attempt))
    return None


def _is_weather_market(market: dict) -> bool:
    question = (market.get("question") or "").lower()
    slug = (market.get("slug") or "").lower()
    tags = [t.lower() for t in (market.get("tags") or [])]
    group_slug = (market.get("groupItemTitle") or market.get("groupItemTag") or "").lower()

    for kw in WEATHER_KEYWORDS:
        if kw in question or kw in slug or kw in group_slug:
            return True

    for tag in tags:
        if any(wt in tag for wt in WEATHER_TAGS):
            return True

    return False


def _is_precipitation_only(market: dict) -> bool:
    question = (market.get("question") or "").lower()
    slug = (market.get("slug") or "").lower()
    for kw in PRECIP_KEYWORDS:
        if kw in question or kw in slug:
            return True
    return False


def _extract_token_ids(market: dict) -> list[str]:
    tokens = market.get("clob_token_ids") or market.get("clobTokenIds") or []
    if not tokens:
        outcomes_raw = market.get("outcomePrices") or {}
        tokens = list(outcomes_raw.keys())
    return [str(t) for t in tokens if t]


def _extract_outcomes(market: dict) -> list[str]:
    outcomes = market.get("outcomes") or []
    if isinstance(outcomes, str):
        import json
        try:
            outcomes = json.loads(outcomes)
        except Exception:
            outcomes = [outcomes]
    return [str(o) for o in outcomes]


def _extract_volume(market: dict) -> Optional[float]:
    for key in ("volume", "volumeNum", "volume24hr"):
        v = market.get(key)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return None


def _extract_liquidity(market: dict) -> Optional[float]:
    for key in ("liquidity", "liquidityNum"):
        v = market.get(key)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return None


def _parse_raw_market(m: dict) -> Optional[RawMarket]:
    market_id = str(m.get("id") or m.get("conditionId") or "")
    if not market_id:
        return None

    question = str(m.get("question") or "")
    if not question:
        return None

    active = bool(m.get("active", True)) and not bool(m.get("closed", False)) and not bool(m.get("archived", False))

    token_ids = _extract_token_ids(m)
    outcomes = _extract_outcomes(m)

    tags_raw = m.get("tags") or []
    if isinstance(tags_raw, list):
        tags = [str(t) for t in tags_raw]
    else:
        tags = []

    return RawMarket(
        market_id=market_id,
        event_id=str(m.get("clob_token_ids", [None])[0]) if not m.get("eventId") else str(m.get("eventId")),
        question=question,
        slug=m.get("slug"),
        close_time=m.get("endDate") or m.get("closeTime") or m.get("end_date_iso"),
        outcomes=outcomes,
        token_ids=token_ids,
        category=m.get("category"),
        tags=tags,
        volume=_extract_volume(m),
        liquidity=_extract_liquidity(m),
        active=active,
        raw=m,
    )


def discover_gamma_markets(max_markets: int = 200, offset: int = 0) -> list[RawMarket]:
    """Fetch markets from Gamma API, filter for active weather/temperature markets."""
    results: list[RawMarket] = []
    page_size = 100
    current_offset = offset

    while len(results) < max_markets:
        params = {
            "active": "true",
            "closed": "false",
            "limit": min(page_size, max_markets - len(results)),
            "offset": current_offset,
            "tag": "weather",
        }
        data = _get(f"{GAMMA_BASE}/markets", params=params)
        if not data:
            logger.warning("Gamma API returned no data at offset %d", current_offset)
            break

        if isinstance(data, dict):
            items = data.get("data") or data.get("markets") or []
        elif isinstance(data, list):
            items = data
        else:
            items = []

        if not items:
            logger.info("No more markets from Gamma at offset %d", current_offset)
            break

        for m in items:
            if not isinstance(m, dict):
                continue
            if not _is_weather_market(m):
                continue
            rm = _parse_raw_market(m)
            if rm is not None:
                results.append(rm)
            if len(results) >= max_markets:
                break

        if len(items) < page_size:
            break
        current_offset += page_size

    logger.info("Gamma weather discovery: found %d markets", len(results))
    return results


def discover_gamma_events(max_markets: int = 200) -> list[RawMarket]:
    """Fetch via events endpoint which often has better weather grouping."""
    results: list[RawMarket] = []
    page_size = 100
    offset = 0

    while len(results) < max_markets:
        params = {
            "active": "true",
            "closed": "false",
            "limit": min(page_size, max_markets),
            "offset": offset,
            "tag": "weather",
        }
        data = _get(f"{GAMMA_BASE}/events", params=params)
        if not data:
            break

        if isinstance(data, dict):
            items = data.get("data") or data.get("events") or []
        elif isinstance(data, list):
            items = data
        else:
            items = []

        if not items:
            break

        for event in items:
            if not isinstance(event, dict):
                continue
            markets_in_event = event.get("markets") or []
            for m in markets_in_event:
                if not isinstance(m, dict):
                    continue
                if not m.get("eventId"):
                    m["eventId"] = event.get("id")
                rm = _parse_raw_market(m)
                if rm is not None and rm.active:
                    results.append(rm)
                if len(results) >= max_markets:
                    break
            if len(results) >= max_markets:
                break

        if len(items) < page_size:
            break
        offset += page_size

    logger.info("Gamma events discovery: found %d markets", len(results))
    return results


def deduplicate_markets(markets: list[RawMarket]) -> list[RawMarket]:
    seen: set[str] = set()
    out: list[RawMarket] = []
    for m in markets:
        if m.market_id not in seen:
            seen.add(m.market_id)
            out.append(m)
    return out


def classify_markets(markets: list[RawMarket]) -> dict[str, list[RawMarket]]:
    """Split markets into temperature candidates vs precipitation vs unknown."""
    temp_markets: list[RawMarket] = []
    precip_markets: list[RawMarket] = []
    unknown_markets: list[RawMarket] = []

    for m in markets:
        if _is_precipitation_only(m):
            precip_markets.append(m)
        elif _is_weather_market(m):
            temp_markets.append(m)
        else:
            unknown_markets.append(m)

    return {
        "temperature": temp_markets,
        "precipitation": precip_markets,
        "unknown": unknown_markets,
    }


def discover_all(max_markets: int = 200) -> list[RawMarket]:
    """Main entrypoint: run both discovery paths and deduplicate."""
    markets_a = discover_gamma_markets(max_markets=max_markets)
    markets_b = discover_gamma_events(max_markets=max_markets)
    all_markets = deduplicate_markets(markets_a + markets_b)
    logger.info("Total deduplicated weather markets: %d", len(all_markets))
    return all_markets
