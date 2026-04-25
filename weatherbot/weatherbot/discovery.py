"""
Discover active Polymarket temperature/weather markets via Gamma and CLOB APIs.
"""
import json
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
    # Extra text fields for richer parsing
    title: Optional[str] = None
    description: Optional[str] = None
    rules: Optional[str] = None
    resolution_source: Optional[str] = None
    # Market microstructure
    order_min_size: Optional[float] = None
    order_price_min_tick_size: Optional[float] = None
    tick_size: Optional[float] = None
    min_order_size: Optional[float] = None
    reward_eligible: Optional[bool] = None
    accepting_orders: Optional[bool] = None
    closed: Optional[bool] = None
    token_mapping_failed: bool = False
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


def normalize_jsonish_list(value: Any) -> list:
    """
    Safely normalise a value that may arrive as a list or a JSON-encoded string.

    Gamma API sometimes returns clobTokenIds (and outcomes) as a raw JSON string
    like '["123456","789012"]' instead of a parsed list.  Iterating such a string
    character-by-character would yield ["[", '"', "1", ...]; this helper prevents
    that by always producing a proper Python list.

    Rules:
    - None / empty  → []
    - already list  → returned as-is
    - str starting with '[' or '{' → json.loads; on failure → []
    - any other type → [value]  (int, float, dict wrapping)
    - individual characters are NEVER returned
    """
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return []
        if stripped[0] in ("[", "{"):
            try:
                parsed = json.loads(stripped)
                return parsed if isinstance(parsed, list) else [parsed]
            except (json.JSONDecodeError, ValueError):
                return []
        # Plain string that is not JSON — do not split, caller decides what to do
        return []
    # int, float, dict, etc.
    return [value]


def _extract_token_ids(market: dict) -> list[str]:
    """
    Extract CLOB token IDs from all known Gamma field shapes.

    Priority:
    1. clobTokenIds / clob_token_ids  (list or JSON string)
    2. tokens[] array of objects with token_id/id keys
    3. outcomePrices dict keys (legacy)
    """
    # 1. Primary field — may be a list or a JSON-encoded string
    raw = market.get("clobTokenIds") or market.get("clob_token_ids")
    tokens = normalize_jsonish_list(raw)

    # Each element may itself be a dict {"token_id": "...", "outcome": "Yes"}
    result: list[str] = []
    for t in tokens:
        if isinstance(t, dict):
            tid = t.get("token_id") or t.get("id") or ""
            if tid:
                result.append(str(tid))
        elif t:
            result.append(str(t))

    if result:
        return result

    # 2. tokens[] array of objects
    token_objs = normalize_jsonish_list(market.get("tokens"))
    for t in token_objs:
        if isinstance(t, dict):
            tid = t.get("token_id") or t.get("id") or ""
            if tid:
                result.append(str(tid))

    if result:
        return result

    # 3. outcomePrices dict keys (legacy Gamma format)
    op = market.get("outcomePrices")
    if isinstance(op, dict):
        return [str(k) for k in op.keys() if k]
    if isinstance(op, str):
        try:
            parsed = json.loads(op)
            if isinstance(parsed, dict):
                return [str(k) for k in parsed.keys() if k]
        except (json.JSONDecodeError, ValueError):
            pass

    return []


def _extract_outcomes(market: dict) -> list[str]:
    outcomes = normalize_jsonish_list(market.get("outcomes"))
    return [str(o) for o in outcomes if o is not None]


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
    token_mapping_failed = (
        bool(token_ids) and bool(outcomes) and len(token_ids) != len(outcomes)
    )

    tags_raw = m.get("tags") or []
    if isinstance(tags_raw, list):
        tags = [str(t) for t in tags_raw]
    else:
        tags = []

    def _safe_float(val) -> Optional[float]:
        try:
            return float(val) if val is not None else None
        except (TypeError, ValueError):
            return None

    def _safe_bool(val) -> Optional[bool]:
        if val is None:
            return None
        return bool(val)

    return RawMarket(
        market_id=market_id,
        event_id=str(m.get("eventId")) if m.get("eventId") else (token_ids[0] if token_ids else None),
        question=question,
        slug=m.get("slug"),
        close_time=m.get("endDate") or m.get("closeTime") or m.get("end_date_iso"),
        outcomes=outcomes,
        token_ids=token_ids,
        token_mapping_failed=token_mapping_failed,
        category=m.get("category"),
        tags=tags,
        volume=_extract_volume(m),
        # Extra text
        title=m.get("title") or m.get("groupItemTitle"),
        description=m.get("description") or m.get("longDescription"),
        rules=m.get("rules") or m.get("resolutionRules"),
        resolution_source=m.get("resolutionSource") or m.get("resolution_source"),
        # Microstructure
        order_min_size=_safe_float(m.get("orderMinSize") or m.get("order_min_size")),
        order_price_min_tick_size=_safe_float(
            m.get("orderPriceMinTickSize") or m.get("order_price_min_tick_size")
        ),
        tick_size=_safe_float(m.get("tickSize") or m.get("tick_size")),
        min_order_size=_safe_float(m.get("minOrderSize") or m.get("min_order_size")),
        reward_eligible=_safe_bool(m.get("rewardEligible") or m.get("reward_eligible")),
        accepting_orders=_safe_bool(m.get("acceptingOrders") or m.get("accepting_orders")),
        closed=_safe_bool(m.get("closed")),
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
