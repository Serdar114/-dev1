"""
Discover active Polymarket temperature/weather markets via Gamma and CLOB APIs.
"""
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"

WEATHER_TAGS = {"weather", "temperature", "climate"}

# These words in question/slug/title reliably indicate a temperature market
WEATHER_KEYWORDS = [
    "temperature", "high temp", "low temp", "daily high", "daily low",
    "degrees", "fahrenheit", "celsius", "°f", "°c", "heat", "cold snap",
    "highest temp", "lowest temp", "max temp", "min temp",
]

# Slug/title sub-strings that unambiguously mark weather events
WEATHER_SLUG_PATTERNS = [
    "highest-temperature-in-",
    "lowest-temperature-in-",
    "daily-high-temperature",
    "daily-low-temperature",
    "highest temperature in",
    "lowest temperature in",
    "daily high temperature",
    "daily low temperature",
    "temperature in ",
    "precipitation in ",
    "rainfall in ",
]

# Precipitation keywords — explicit only; "wet" and "flood" removed (too generic)
PRECIP_KEYWORDS = [
    "rainfall", "precipitation", "snowfall",
    "inches of rain", "mm of rain", "inches of precip",
    " rain ", "rain?", "it rain", "will rain",
    " snow ", "snow?", "it snow",
]

TEMP_MARKET_TYPES = {"daily_high_temperature", "daily_low_temperature"}

# Strict daily city temperature event patterns
DAILY_TEMP_SLUG_PATTERNS = [
    "highest-temperature-in-",
    "lowest-temperature-in-",
]
DAILY_TEMP_TITLE_PATTERNS = [
    "highest temperature in",
    "lowest temperature in",
]

# Date month patterns used by is_daily_temperature_event
_DATE_MONTH_SLUG = [
    "on-january-", "on-february-", "on-march-", "on-april-",
    "on-may-", "on-june-", "on-july-", "on-august-",
    "on-september-", "on-october-", "on-november-", "on-december-",
]
_DATE_MONTH_TEXT = [
    "on january", "on february", "on march", "on april",
    "on may", "on june", "on july", "on august",
    "on september", "on october", "on november", "on december",
]

_MONTH_NUM: dict[str, int] = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}


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
    # Polymarket: first clobTokenId = Yes, second = No
    yes_token_id: Optional[str] = None
    no_token_id: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class WeatherEventSummary:
    """Lightweight event summary returned by discover_weather_events_only()."""
    event_id: str
    slug: Optional[str]
    title: str
    n_markets: int
    parsed_city: Optional[str]
    parsed_date: Optional[str]
    close_time: Optional[str]


@dataclass
class TemperatureDiscoveryResult:
    """Result of discover_daily_temperature_only() with full counts and market list."""
    raw_events_seen: int = 0
    raw_markets_seen: int = 0
    daily_temperature_events_found: int = 0
    filtered_other_weather: int = 0
    filtered_non_temperature: int = 0
    summaries: list[WeatherEventSummary] = field(default_factory=list)
    future_watchlist: list[WeatherEventSummary] = field(default_factory=list)
    markets: list[RawMarket] = field(default_factory=list)
    first30_raw_event_slugs: list[str] = field(default_factory=list)
    first30_raw_market_slugs: list[str] = field(default_factory=list)


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


_TAG_TEXT_KEYS = ("name", "label", "slug", "title", "id")


def normalize_text_items(value: Any) -> list[str]:
    """
    Safely extract lowercase strings from a Gamma tag/category field.

    Gamma may return these fields as strings, lists of strings, lists of dicts,
    plain dicts, or even None/numbers.  This helper handles every shape and
    never raises.

    - None              → []
    - str               → [str.lower()]
    - dict              → text extracted from name/label/slug/title/id keys
    - list (any mix)    → each element handled by the rules above
    - int/float/bool    → []  (ignored silently)
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value.lower()] if value.strip() else []
    if isinstance(value, dict):
        return _text_from_dict(value)
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            if item is None:
                continue
            if isinstance(item, str):
                if item.strip():
                    result.append(item.lower())
            elif isinstance(item, dict):
                result.extend(_text_from_dict(item))
            # int/float/bool/other → skip silently
        return result
    # int, float, bool, etc.
    return []


def _text_from_dict(d: dict) -> list[str]:
    """Extract non-empty string values from known tag-dict text keys."""
    result: list[str] = []
    for key in _TAG_TEXT_KEYS:
        v = d.get(key)
        if isinstance(v, str) and v.strip():
            result.append(v.lower())
    return result


def _is_weather_market(market: dict) -> bool:
    """Return True if the market looks like a weather/temperature market.

    Never raises — bad field shapes are silently ignored.
    """
    try:
        question = (market.get("question") or "").lower()
        slug = (market.get("slug") or "").lower()
        title = (market.get("title") or market.get("groupItemTitle") or "").lower()
        group_slug = (market.get("groupItemTag") or "").lower()

        combined = f"{question} {slug} {title} {group_slug}"

        for pat in WEATHER_SLUG_PATTERNS:
            if pat in combined:
                return True

        for kw in WEATHER_KEYWORDS:
            if kw in combined:
                return True

        # Check all tag/category fields with robust normalization
        tag_texts: list[str] = []
        for field_name in ("tags", "category", "categories", "topic", "topics"):
            tag_texts.extend(normalize_text_items(market.get(field_name)))

        for tag in tag_texts:
            if any(wt in tag for wt in WEATHER_TAGS):
                return True

        return False
    except Exception:
        logger.debug("_is_weather_market: unexpected field shape, returning False", exc_info=True)
        return False


def is_weather_candidate(question: str, slug: str = "") -> bool:
    """
    Public helper: does this question/slug contain any temperature or weather signal?

    Used as a last-resort non-weather filter in the processing pipeline —
    markets that pass discovery but have zero weather signal are skipped
    before being logged to observations.jsonl.
    """
    text = (question + " " + slug).lower()
    for kw in WEATHER_KEYWORDS:
        if kw in text:
            return True
    for pat in WEATHER_SLUG_PATTERNS:
        if pat in text:
            return True
    for kw in PRECIP_KEYWORDS:
        if kw in text:
            return True
    return False


def is_daily_temperature_event(title: str, slug: str, question: str = "") -> bool:
    """
    Return True ONLY for daily city temperature events with a specific calendar date.

    Requirements:
      1. slug contains 'highest-temperature-in-' or 'lowest-temperature-in-'
         OR title/question contains 'highest temperature in' or 'lowest temperature in'
      2. A specific calendar month appears (e.g. 'on-april-', 'on April')

    Strictly excludes:
      - "Where will 2026 rank among the hottest years on record?"
      - "Min Arctic sea ice extent this summer?"
      - "SpaceX Starship fully reusable before 2027?"
      - "New COVID variant of concern before 2027?"
    """
    slug_l = (slug or "").lower()
    title_l = (title or "").lower()
    question_l = (question or "").lower()
    text = f"{title_l} {question_l}"

    # 1. Temperature pattern
    has_temp = (
        any(p in slug_l for p in DAILY_TEMP_SLUG_PATTERNS)
        or any(p in text for p in DAILY_TEMP_TITLE_PATTERNS)
    )
    if not has_temp:
        return False

    # 2. Specific date (month) pattern
    has_date = (
        any(p in slug_l for p in _DATE_MONTH_SLUG)
        or any(p in text for p in _DATE_MONTH_TEXT)
    )
    return has_date


def _extract_event_date(slug: str, title: str, close_time: Optional[str] = None) -> Optional[date]:
    """
    Extract the specific target date from a daily temperature event.
    Slug pattern: 'highest-temperature-in-seoul-on-april-27-2026'
    Title pattern: 'Highest Temperature in Seoul on April 27, 2026'
    """
    # Slug: on-month-day-year
    m = re.search(r"on-(\w+)-(\d{1,2})-(\d{4})", (slug or "").lower())
    if m:
        month = _MONTH_NUM.get(m.group(1))
        if month:
            try:
                return date(int(m.group(3)), month, int(m.group(2)))
            except ValueError:
                pass

    # Title/question: "on April 27, 2026" or "on April 27"
    m = re.search(r"on\s+(\w+)\s+(\d{1,2})(?:[,\s]+(\d{4}))?", (title or "").lower())
    if m:
        month = _MONTH_NUM.get(m.group(1))
        if month:
            day = int(m.group(2))
            year_str = m.group(3)
            if not year_str and close_time:
                # Infer year from close_time ISO string
                try:
                    year_str = close_time[:4]
                except (TypeError, IndexError):
                    pass
            if year_str:
                try:
                    return date(int(year_str), month, day)
                except ValueError:
                    pass
    return None


def _is_precipitation_only(market: dict) -> bool:
    """
    Return True only when explicit precipitation words are present.
    Generic words like 'wet' and 'flood' are intentionally excluded.
    """
    question = (market.get("question") or "").lower()
    slug = (market.get("slug") or "").lower()
    text = question + " " + slug
    for kw in PRECIP_KEYWORDS:
        if kw in text:
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

    tags = normalize_text_items(m.get("tags"))

    def _safe_float(val) -> Optional[float]:
        try:
            return float(val) if val is not None else None
        except (TypeError, ValueError):
            return None

    def _safe_bool(val) -> Optional[bool]:
        if val is None:
            return None
        return bool(val)

    yes_token_id = token_ids[0] if len(token_ids) >= 1 else None
    no_token_id = token_ids[1] if len(token_ids) >= 2 else None

    return RawMarket(
        market_id=market_id,
        event_id=str(m.get("eventId")) if m.get("eventId") else None,
        question=question,
        slug=m.get("slug"),
        close_time=m.get("endDate") or m.get("closeTime") or m.get("end_date_iso"),
        outcomes=outcomes,
        token_ids=token_ids,
        token_mapping_failed=token_mapping_failed,
        yes_token_id=yes_token_id,
        no_token_id=no_token_id,
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


def _extract_markets_from_event(event: dict) -> list[RawMarket]:
    """
    Extract all RawMarket objects from a Gamma event dict.
    Merges event-level title/description/rules/close_time into each child so
    the parser has the richest possible text and all children have a valid date.
    """
    event_id = str(event.get("id") or "")
    event_title = event.get("title") or event.get("name") or ""
    event_desc = event.get("description") or ""
    event_rules = event.get("resolutionRules") or event.get("rules") or ""
    event_close = (
        event.get("endDate") or event.get("closeTime")
        or event.get("end_date_iso") or event.get("startDate")
    )

    results: list[RawMarket] = []
    for m in (event.get("markets") or []):
        if not isinstance(m, dict):
            continue
        # Merge event metadata without mutating the original dict
        merged: dict = dict(m)
        if event_id and not merged.get("eventId"):
            merged["eventId"] = event_id
        if event_title and not merged.get("title"):
            merged["title"] = event_title
        if event_desc and not merged.get("description"):
            merged["description"] = event_desc
        if event_rules and not merged.get("rules"):
            merged["rules"] = event_rules
        # Propagate event close_time so children can infer year for partial dates
        if event_close and not merged.get("endDate") and not merged.get("closeTime"):
            merged["endDate"] = event_close

        rm = _parse_raw_market(merged)
        if rm is not None:
            results.append(rm)
    return results


def fetch_event_by_slug(slug: str) -> list[RawMarket]:
    """
    Fetch all markets for a Polymarket event by its slug.

    Endpoint priority (per Polymarket docs):
      1. GET /events/slug/{slug}   — canonical slug lookup (correct endpoint)
      2. GET /events?slug={slug}   — list query fallback
      3. GET /markets?slug={slug}  — direct market slug fallback
    """
    if not slug:
        return []

    # 1. Canonical slug endpoint
    data = _get(f"{GAMMA_BASE}/events/slug/{slug}", params={})
    if data:
        if isinstance(data, dict) and data.get("markets"):
            results = _extract_markets_from_event(data)
            if results:
                logger.info("fetch_event_by_slug %r → %d markets via /events/slug/", slug, len(results))
                return results
        elif isinstance(data, list):
            results = []
            for ev in data:
                if isinstance(ev, dict):
                    results.extend(_extract_markets_from_event(ev))
            if results:
                logger.info("fetch_event_by_slug %r → %d markets via /events/slug/ (list)", slug, len(results))
                return results

    # 2. Events list query
    data = _get(f"{GAMMA_BASE}/events", params={"slug": slug})
    if data:
        events = data if isinstance(data, list) else (data.get("data") or data.get("events") or [])
        results = []
        for ev in events:
            if isinstance(ev, dict):
                results.extend(_extract_markets_from_event(ev))
        if results:
            logger.info("fetch_event_by_slug %r → %d markets via /events?slug=", slug, len(results))
            return results

    # 3. Markets query (single-outcome or direct market slug)
    data = _get(f"{GAMMA_BASE}/markets", params={"slug": slug})
    if data:
        items = data if isinstance(data, list) else (data.get("data") or data.get("markets") or [])
        results = []
        for m in items:
            if isinstance(m, dict):
                rm = _parse_raw_market(m)
                if rm is not None:
                    results.append(rm)
        if results:
            logger.info("fetch_event_by_slug %r → %d markets via /markets?slug=", slug, len(results))
            return results

    logger.warning("fetch_event_by_slug %r: no markets found on any endpoint", slug)
    return []


def discover_by_slug(slug: str) -> list[RawMarket]:
    """
    Targeted discovery: fetch all markets for a specific event slug.
    Bypasses broad discovery for testing and single-market validation.
    """
    logger.info("Targeted discovery: slug=%r", slug)
    results = fetch_event_by_slug(slug)
    logger.info("discover_by_slug %r: found %d markets", slug, len(results))
    return results


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
            if not _is_weather_market(event):
                continue
            for rm in _extract_markets_from_event(event):
                if rm.active:
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


def _page_weather_events(
    max_events: int,
    tag: Optional[str] = None,
) -> list[dict]:
    """
    Paginate Gamma /events?active=true&closed=false and return raw event dicts
    that pass the weather filter.  No tag restriction by default — broader net.
    """
    results: list[dict] = []
    page_size = 100
    offset = 0

    while len(results) < max_events:
        params: dict = {
            "active": "true",
            "closed": "false",
            "limit": min(page_size, max_events - len(results)),
            "offset": offset,
        }
        if tag:
            params["tag"] = tag

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
            slug = event.get("slug") or ""
            title = event.get("title") or event.get("name") or ""
            if is_weather_candidate(title, slug=slug) or _is_weather_market(event):
                results.append(event)
            if len(results) >= max_events:
                break

        if len(items) < page_size:
            break
        offset += page_size

    logger.info("_page_weather_events tag=%r: found %d weather events", tag, len(results))
    return results


def _make_event_summary(event: dict, markets: list[RawMarket]) -> WeatherEventSummary:
    """Build a WeatherEventSummary from an event dict and its extracted markets."""
    from .parser import parse_market  # local import avoids circular at module level

    event_id = str(event.get("id") or "")
    slug = event.get("slug")
    title = event.get("title") or event.get("name") or ""
    close_time = event.get("endDate") or event.get("closeTime") or event.get("end_date_iso")

    parsed_city: Optional[str] = None
    parsed_date: Optional[str] = None
    if markets:
        rep = markets[0]
        try:
            pm = parse_market(
                rep.market_id, rep.question,
                title=rep.title, rules=rep.rules, close_time=rep.close_time,
            )
            parsed_city = pm.city
            parsed_date = str(pm.parsed_target_date) if pm.parsed_target_date else None
        except Exception:
            pass

    return WeatherEventSummary(
        event_id=event_id,
        slug=slug,
        title=title,
        n_markets=len(markets),
        parsed_city=parsed_city,
        parsed_date=parsed_date,
        close_time=close_time,
    )


def discover_weather_events_broad(max_events: int = 100) -> list[RawMarket]:
    """
    Broad weather discovery: fetch active events without tag filter,
    apply weather keyword filter, extract all child markets.
    Used by --discover-weather --max-events N.
    """
    events = _page_weather_events(max_events=max_events)
    tagged = _page_weather_events(max_events=max_events, tag="weather")

    seen_event_ids: set[str] = set()
    all_events: list[dict] = []
    for ev in events + tagged:
        eid = str(ev.get("id") or "")
        if eid and eid not in seen_event_ids:
            seen_event_ids.add(eid)
            all_events.append(ev)

    markets: list[RawMarket] = []
    seen_market_ids: set[str] = set()
    for event in all_events:
        for rm in _extract_markets_from_event(event):
            if rm.market_id not in seen_market_ids:
                seen_market_ids.add(rm.market_id)
                markets.append(rm)

    logger.info(
        "discover_weather_events_broad: %d events → %d markets",
        len(all_events), len(markets),
    )
    return markets


def discover_weather_events_only(max_events: int = 100) -> list[WeatherEventSummary]:
    """
    Broad weather discovery returning event-level summaries only.
    No orderbook/model fetch. Used by --discover-weather-only.
    """
    events = _page_weather_events(max_events=max_events)
    tagged = _page_weather_events(max_events=max_events, tag="weather")

    seen_event_ids: set[str] = set()
    all_events: list[dict] = []
    for ev in events + tagged:
        eid = str(ev.get("id") or "")
        if eid and eid not in seen_event_ids:
            seen_event_ids.add(eid)
            all_events.append(ev)

    summaries: list[WeatherEventSummary] = []
    for event in all_events:
        markets = _extract_markets_from_event(event)
        if markets:
            summaries.append(_make_event_summary(event, markets))

    logger.info("discover_weather_events_only: %d event summaries", len(summaries))
    return summaries


# ── Strict daily temperature discovery ───────────────────────────────────────

def _paginate_raw_events(
    max_pages: int,
    extra_params: Optional[dict] = None,
) -> list[dict]:
    """
    Paginate GET /events?active=true&closed=false up to max_pages pages.
    extra_params (e.g. {"q": "highest temperature"}) are merged into params.
    Returns deduplicated raw event dicts.
    """
    items: list[dict] = []
    seen_ids: set[str] = set()
    page_size = 100
    offset = 0

    for _ in range(max_pages):
        params: dict = {
            "active": "true",
            "closed": "false",
            "limit": page_size,
            "offset": offset,
        }
        if extra_params:
            params.update(extra_params)

        data = _get(f"{GAMMA_BASE}/events", params=params)
        if not data:
            break

        page = data if isinstance(data, list) else (data.get("data") or data.get("events") or [])
        if not page:
            break

        for item in page:
            eid = str(item.get("id") or "")
            if eid and eid not in seen_ids:
                seen_ids.add(eid)
                items.append(item)

        if len(page) < page_size:
            break
        offset += page_size

    return items


def _paginate_raw_markets(max_pages: int) -> list[dict]:
    """
    Paginate GET /markets?active=true&closed=false up to max_pages pages.
    Returns deduplicated raw market dicts.
    """
    items: list[dict] = []
    seen_ids: set[str] = set()
    page_size = 100
    offset = 0

    for _ in range(max_pages):
        params: dict = {
            "active": "true",
            "closed": "false",
            "limit": page_size,
            "offset": offset,
        }

        data = _get(f"{GAMMA_BASE}/markets", params=params)
        if not data:
            break

        page = data if isinstance(data, list) else (data.get("data") or data.get("markets") or [])
        if not page:
            break

        for item in page:
            mid = str(item.get("id") or "")
            if mid and mid not in seen_ids:
                seen_ids.add(mid)
                items.append(item)

        if len(page) < page_size:
            break
        offset += page_size

    return items


def discover_daily_temperature_only(
    max_events: int = 100,
    max_pages: int = 20,
    window_days: int = 2,
    include_future: bool = False,
) -> TemperatureDiscoveryResult:
    """
    Strictly discover daily city temperature events using three sources:
      A) Paginated /events (no tag filter)
      B) /events with search queries for 'highest temperature' / 'lowest temperature'
      C) Paginated /markets grouped by event

    Only events passing is_daily_temperature_event() are kept.
    Events within window_days of today go to summaries; beyond → future_watchlist.
    Used by --discover-temperature-only.
    """
    result = TemperatureDiscoveryResult()
    seen_event_ids: set[str] = set()
    seen_market_ids: set[str] = set()
    passing_events: list[dict] = []

    # ── Source A: Paginated /events ───────────────────────────────────────────
    raw_events = _paginate_raw_events(max_pages=max_pages)
    result.raw_events_seen += len(raw_events)
    result.first30_raw_event_slugs = [
        e.get("slug") or e.get("title") or str(e.get("id", ""))
        for e in raw_events[:30]
    ]

    for event in raw_events:
        slug = event.get("slug") or ""
        title = event.get("title") or event.get("name") or ""
        eid = str(event.get("id") or "")

        if is_daily_temperature_event(title, slug):
            if eid not in seen_event_ids:
                seen_event_ids.add(eid)
                passing_events.append(event)
        elif _is_weather_market(event):
            result.filtered_other_weather += 1
        else:
            result.filtered_non_temperature += 1

    # ── Source B: Search queries (q= param, two terms only) ──────────────────
    for query in ("highest temperature", "lowest temperature"):
        search_events = _paginate_raw_events(max_pages=3, extra_params={"q": query})
        for event in search_events:
            result.raw_events_seen += 1
            slug = event.get("slug") or ""
            title = event.get("title") or event.get("name") or ""
            eid = str(event.get("id") or "")
            if is_daily_temperature_event(title, slug):
                if eid not in seen_event_ids:
                    seen_event_ids.add(eid)
                    passing_events.append(event)

    # ── Source C: Paginated /markets ──────────────────────────────────────────
    raw_markets = _paginate_raw_markets(max_pages=max_pages)
    result.raw_markets_seen = len(raw_markets)
    result.first30_raw_market_slugs = [
        m.get("slug") or (m.get("question") or "")[:60] or str(m.get("id", ""))
        for m in raw_markets[:30]
    ]

    # Group temperature markets by event, building synthetic events for new ones
    market_by_event: dict[str, list[dict]] = {}
    for m in raw_markets:
        slug = m.get("slug") or ""
        question = m.get("question") or ""
        title = m.get("title") or m.get("groupItemTitle") or ""
        eid = str(m.get("eventId") or m.get("event_id") or "")

        if is_daily_temperature_event(title, slug, question):
            if eid and eid not in seen_event_ids:
                market_by_event.setdefault(eid, []).append(m)

    for eid, mlist in market_by_event.items():
        rep = mlist[0]
        synthetic = {
            "id": eid,
            "slug": rep.get("slug"),
            "title": rep.get("title") or rep.get("groupItemTitle") or "",
            "markets": mlist,
            "endDate": rep.get("endDate") or rep.get("closeTime"),
        }
        seen_event_ids.add(eid)
        passing_events.append(synthetic)

    result.daily_temperature_events_found = len(passing_events)

    # ── Split into window vs future ───────────────────────────────────────────
    today = date.today()
    cutoff = today + timedelta(days=window_days)

    for event in passing_events:
        slug = event.get("slug") or ""
        title = event.get("title") or event.get("name") or ""
        close_time = event.get("endDate") or event.get("closeTime") or event.get("end_date_iso")

        markets = _extract_markets_from_event(event)
        summary = _make_event_summary(event, markets)
        event_date = _extract_event_date(slug, title, close_time)

        if event_date is not None and event_date > cutoff:
            result.future_watchlist.append(summary)
            if not include_future:
                continue

        result.summaries.append(summary)
        for rm in markets:
            if rm.market_id not in seen_market_ids:
                seen_market_ids.add(rm.market_id)
                result.markets.append(rm)

    logger.info(
        "discover_daily_temperature_only: raw_events=%d raw_markets=%d "
        "found=%d active=%d future=%d filtered_weather=%d filtered_other=%d",
        result.raw_events_seen, result.raw_markets_seen,
        result.daily_temperature_events_found,
        len(result.summaries), len(result.future_watchlist),
        result.filtered_other_weather, result.filtered_non_temperature,
    )
    return result


def discover_daily_temperature_events(
    max_events: int = 100,
    max_pages: int = 20,
    window_days: int = 2,
    include_future: bool = False,
) -> list[RawMarket]:
    """
    Discover daily temperature markets for the full processing pipeline.
    Returns only RawMarket objects for events within the date window.
    Used by --discover-temperature --max-events N --max-pages N.
    """
    disc = discover_daily_temperature_only(
        max_events=max_events,
        max_pages=max_pages,
        window_days=window_days,
        include_future=include_future,
    )
    logger.info(
        "discover_daily_temperature_events: returning %d markets from %d events",
        len(disc.markets), len(disc.summaries),
    )
    return disc.markets
