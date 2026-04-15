"""
gamma_api.py — Gamma API client for BTC short-horizon market discovery.

Responsibilities:
  - Query https://gamma-api.polymarket.com for active BTC up/down markets
  - Normalise raw API responses into MarketRecord objects
  - Log all parse anomalies (never silently drop malformed markets)
  - Maintain a refresh-able in-memory cache

No strategy logic. No assumptions about spread/depth/fee values.
All microstructure fields treated as discoverable, not assumed.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

import logger as log
from schemas import MarketRecord, TokenInfo

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GAMMA_BASE = "https://gamma-api.polymarket.com"
MARKETS_ENDPOINT = f"{GAMMA_BASE}/markets"
EVENTS_ENDPOINT = f"{GAMMA_BASE}/events"

# How many results to page through per request
PAGE_LIMIT = 100

# Markets cache TTL in seconds
CACHE_TTL_S = 60

# Minimum fields required for a market record to be usable
REQUIRED_FIELDS = {"id"}

# ---------------------------------------------------------------------------
# Strict family scope — only these two slug-prefix families are allowed.
# Everything else is excluded unconditionally, regardless of BTC content.
# ---------------------------------------------------------------------------

# Primary execution family
SLUG_PREFIX_15M = "btc-updown-15m-"
# Observer / regime / gate family
SLUG_PREFIX_5M  = "btc-updown-5m-"
ALLOWED_PREFIXES = (SLUG_PREFIX_15M, SLUG_PREFIX_5M)

# Slug substrings that are unconditionally excluded (generic price-target markets).
# These should never appear under btc-updown-* but are listed defensively.
EXCLUDED_SLUG_SUBSTRINGS: List[str] = [
    "will-btc-be-above",
    "will-btc-be-below",
    "bitcoin-above",
    "bitcoin-below",
    "btc-above",
    "btc-below",
    "price-target",
    "will-bitcoin",
    "bitcoin-price",
    "btc-price",
]

# Resolution reference for all qualifying markets.
# External spot price is observer-only; Chainlink BTC/USD is the settlement truth.
RESOLUTION_REFERENCE = "chainlink_btc_usd"

# HTTP timeout
HTTP_TIMEOUT_S = 10

# ---------------------------------------------------------------------------
# Module-level cache
# ---------------------------------------------------------------------------

_cache_lock = threading.Lock()
_cache_markets: List[MarketRecord] = []
_cache_ts: float = 0.0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_ms() -> int:
    return int(time.time() * 1000)


def _parse_float(val: Any, field: str, context: str) -> Optional[float]:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError) as exc:
        log.log_parse_anomaly(context, field, val, f"cannot convert to float: {exc}")
        return None


def _parse_bool(val: Any, field: str, context: str) -> Optional[bool]:
    if val is None:
        return None
    if isinstance(val, bool):
        return val
    if isinstance(val, int):
        return bool(val)
    if isinstance(val, str):
        return val.lower() in ("true", "1", "yes")
    log.log_parse_anomaly(context, field, val, "unexpected bool-like value")
    return None


def _parse_ts_ms(val: Any, field: str, context: str) -> Optional[int]:
    """
    Accept ISO-8601 strings or numeric epoch (seconds or ms).
    Returns epoch ms or None.
    """
    if val is None:
        return None
    # Numeric
    if isinstance(val, (int, float)):
        v = float(val)
        # Heuristic: if value < 1e12 treat as epoch seconds, else ms
        return int(v * 1000) if v < 1e12 else int(v)
    if isinstance(val, str):
        if not val.strip():
            return None
        # Try ISO-8601
        try:
            from datetime import datetime, timezone
            from dateutil import parser as dp
            dt = dp.parse(val)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        except Exception as exc:
            pass
        # Try numeric string
        try:
            v = float(val)
            return int(v * 1000) if v < 1e12 else int(v)
        except ValueError:
            pass
        log.log_parse_anomaly(context, field, val, "cannot parse timestamp")
        return None
    log.log_parse_anomaly(context, field, val, f"unexpected timestamp type {type(val)}")
    return None


def _extract_tokens(raw: Dict[str, Any], context: str) -> List[TokenInfo]:
    """
    Parse the tokens/clob_token_ids field from a Gamma market object.
    Handles multiple known shapes:
      Shape A: [{"token_id": "...", "outcome": "Yes"}, ...]
      Shape B: ["token_id_1", "token_id_2"]  (outcomes inferred from position)
      Shape C: {"Yes": "token_id_1", "No": "token_id_2"}
    """
    raw_tokens = raw.get("tokens") or raw.get("clobTokenIds") or raw.get("clob_token_ids")
    if not raw_tokens:
        log.log_parse_anomaly(context, "tokens", raw_tokens, "no token field found")
        return []

    result: List[TokenInfo] = []

    # Shape A: list of dicts
    if isinstance(raw_tokens, list) and all(isinstance(t, dict) for t in raw_tokens):
        for i, t in enumerate(raw_tokens):
            tid = t.get("token_id") or t.get("tokenId") or t.get("id")
            outcome = t.get("outcome") or t.get("side") or f"token_{i}"
            price_raw = t.get("price")
            price = _parse_float(price_raw, f"tokens[{i}].price", context)
            if not tid:
                log.log_parse_anomaly(context, f"tokens[{i}].token_id", t, "missing token_id in dict")
                continue
            result.append(TokenInfo(token_id=str(tid), outcome=str(outcome), price=price))
        return result

    # Shape B: list of strings (token ids only, infer outcomes by position)
    if isinstance(raw_tokens, list) and all(isinstance(t, str) for t in raw_tokens):
        outcomes = ["Yes", "No"] if len(raw_tokens) == 2 else [f"token_{i}" for i in range(len(raw_tokens))]
        for tid, outcome in zip(raw_tokens, outcomes):
            result.append(TokenInfo(token_id=tid, outcome=outcome))
        log.log_parse_anomaly(context, "tokens", raw_tokens, "token list was bare strings; outcomes inferred by position")
        return result

    # Shape C: dict mapping outcome -> token_id
    if isinstance(raw_tokens, dict):
        for outcome, tid in raw_tokens.items():
            if not isinstance(tid, str):
                log.log_parse_anomaly(context, f"tokens[{outcome}]", tid, "token_id value is not a string")
                continue
            result.append(TokenInfo(token_id=tid, outcome=str(outcome)))
        return result

    log.log_parse_anomaly(context, "tokens", type(raw_tokens).__name__, "unrecognised token shape")
    return result


def _normalise_market(raw: Dict[str, Any]) -> Optional[MarketRecord]:
    """
    Convert one raw Gamma API market dict into a MarketRecord.
    Returns None only if the record is so malformed it cannot be identified at all.
    All other anomalies are recorded in parse_warnings.
    """
    market_id = raw.get("id") or raw.get("marketId") or raw.get("conditionId")
    if not market_id:
        log.log_parse_anomaly("normalise_market", "id", raw, "no usable id field; dropping record")
        return None

    context = f"market/{market_id}"
    warnings: List[str] = []

    def warn(msg: str) -> None:
        warnings.append(msg)

    # ---- identity ----
    market_slug = raw.get("slug") or raw.get("marketSlug")
    event_slug = raw.get("eventSlug") or raw.get("event_slug")
    condition_id = raw.get("conditionId") or raw.get("condition_id")
    question = raw.get("question") or raw.get("title")

    # ---- tokens ----
    tokens = _extract_tokens(raw, context)
    if not tokens:
        warn("no tokens extracted")

    # ---- timing ----
    start_time = _parse_ts_ms(raw.get("startTime") or raw.get("start_time"), "startTime", context)
    end_time = _parse_ts_ms(raw.get("endTime") or raw.get("end_time"), "endTime", context)
    resolution_time = _parse_ts_ms(
        raw.get("resolutionTime") or raw.get("resolution_time"), "resolutionTime", context
    )

    # ---- state flags ----
    active = _parse_bool(raw.get("active"), "active", context)
    closed = _parse_bool(raw.get("closed"), "closed", context)
    accepting_orders = _parse_bool(
        raw.get("acceptingOrders") or raw.get("accepting_orders"), "acceptingOrders", context
    )

    # ---- microstructure ----
    minimum_tick_size = _parse_float(
        raw.get("minimumTickSize") or raw.get("minimum_tick_size"), "minimumTickSize", context
    )
    if minimum_tick_size is None:
        warn("minimum_tick_size not found in market object")

    neg_risk = _parse_bool(raw.get("negRisk") or raw.get("neg_risk"), "negRisk", context)

    fees_enabled = _parse_bool(
        raw.get("feesEnabled") or raw.get("fees_enabled"), "feesEnabled", context
    )

    # Fee rate — may appear under several names; treat as Optional
    fee_rate_raw = (
        raw.get("makerBaseFee")
        or raw.get("takerBaseFee")
        or raw.get("feeRate")
        or raw.get("fee_rate")
    )
    fee_rate_bps: Optional[float] = None
    if fee_rate_raw is not None:
        fee_rate_bps = _parse_float(fee_rate_raw, "feeRate", context)
    else:
        warn("no fee rate field found in market object (will attempt CLOB lookup)")

    # Min order size — multiple possible field names
    min_order_size = _parse_float(
        raw.get("minOrderSize") or raw.get("min_order_size") or raw.get("minimumOrderSize"),
        "minOrderSize",
        context,
    )
    if min_order_size is None:
        warn("min_order_size not found in market object")

    min_incentive_size = _parse_float(
        raw.get("minIncentiveSize") or raw.get("min_incentive_size"), "minIncentiveSize", context
    )
    max_incentive_spread = _parse_float(
        raw.get("maxIncentiveSpread") or raw.get("max_incentive_spread"),
        "maxIncentiveSpread",
        context,
    )

    # Preserve raw fields we haven't explicitly mapped (for debugging)
    known_keys = {
        "id", "marketId", "conditionId", "slug", "marketSlug", "eventSlug", "event_slug",
        "question", "title", "tokens", "clobTokenIds", "clob_token_ids",
        "startTime", "start_time", "endTime", "end_time", "resolutionTime", "resolution_time",
        "active", "closed", "acceptingOrders", "accepting_orders",
        "minimumTickSize", "minimum_tick_size", "negRisk", "neg_risk",
        "feesEnabled", "fees_enabled", "makerBaseFee", "takerBaseFee", "feeRate", "fee_rate",
        "minOrderSize", "min_order_size", "minimumOrderSize",
        "minIncentiveSize", "min_incentive_size", "maxIncentiveSpread", "max_incentive_spread",
    }
    raw_fields = {k: v for k, v in raw.items() if k not in known_keys}

    return MarketRecord(
        market_id=str(market_id),
        market_slug=str(market_slug) if market_slug else None,
        event_slug=str(event_slug) if event_slug else None,
        condition_id=str(condition_id) if condition_id else None,
        question=str(question) if question else None,
        tokens=tokens,
        start_time=start_time,
        end_time=end_time,
        resolution_time=resolution_time,
        active=active,
        closed=closed,
        accepting_orders=accepting_orders,
        minimum_tick_size=minimum_tick_size,
        neg_risk=neg_risk,
        fees_enabled=fees_enabled,
        fee_rate_bps=fee_rate_bps,
        min_order_size=min_order_size,
        min_incentive_size=min_incentive_size,
        max_incentive_spread=max_incentive_spread,
        raw_fields=raw_fields,
        parse_warnings=warnings,
    )


def _detect_family(m: MarketRecord) -> Optional[str]:
    """
    Return "15m", "5m", or None.

    Match is slug-prefix ONLY.
      - btc-updown-15m-* → "15m"  (primary execution family)
      - btc-updown-5m-*  → "5m"   (observer / regime / gate family)
      - anything else    → None   (excluded unconditionally)

    Generic BTC markets (will-btc-be-above-*, price-target, etc.) are
    explicitly blocked by EXCLUDED_SLUG_SUBSTRINGS even if they somehow
    matched a prefix check (defensive).
    """
    slug = (m.market_slug or "").lower()
    event_slug = (m.event_slug or "").lower()

    # Hard-exclude generic price-target / above-below markets first
    for pat in EXCLUDED_SLUG_SUBSTRINGS:
        if pat in slug or pat in event_slug:
            return None

    # Primary match: market_slug prefix
    if slug.startswith(SLUG_PREFIX_15M):
        return "15m"
    if slug.startswith(SLUG_PREFIX_5M):
        return "5m"

    # Fallback: some Gamma records expose the family prefix on event_slug only
    if event_slug.startswith(SLUG_PREFIX_15M):
        return "15m"
    if event_slug.startswith(SLUG_PREFIX_5M):
        return "5m"

    return None


def _is_not_expired(m: MarketRecord, now_ms: int) -> bool:
    """
    True if the market window has not already closed.
    For btc-updown-* families the slug guarantees duration; this check only
    gates out windows that have already resolved.
    """
    end = m.end_time or m.resolution_time
    if end is None:
        return True  # unknown end — do not exclude; anomaly logged elsewhere
    return end > now_ms


def _is_accepting_orders(m: MarketRecord) -> bool:
    # explicit flag preferred; fall back to active=True + closed=False
    if m.accepting_orders is not None:
        return m.accepting_orders
    return bool(m.active) and not bool(m.closed)


# ---------------------------------------------------------------------------
# API fetch with pagination
# ---------------------------------------------------------------------------

def _fetch_markets_page(params: Dict[str, Any]) -> List[Dict[str, Any]]:
    try:
        resp = requests.get(MARKETS_ENDPOINT, params=params, timeout=HTTP_TIMEOUT_S)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        log.log_exception("gamma_api._fetch_markets_page", exc, {"params": params})
        return []
    except ValueError as exc:
        log.log_exception("gamma_api._fetch_markets_page.json_decode", exc)
        return []

    # Gamma may return list directly or {"data": [...]}
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "data" in data:
        inner = data["data"]
        if isinstance(inner, list):
            return inner
        log.log_parse_anomaly("gamma_api", "data", type(inner).__name__, "expected list under 'data'")
        return []

    log.log_parse_anomaly("gamma_api", "response_root", type(data).__name__, "unexpected root type")
    return []


def _fetch_all_active_btc_candidates() -> List[Dict[str, Any]]:
    """
    Page through the Gamma /markets endpoint collecting BTC candidates.
    Queries both by tag and by active status to maximise discovery.
    """
    all_raw: List[Dict[str, Any]] = []
    seen_ids: set = set()

    # Strategy 1: tag-based search
    for tag in ("btc", "bitcoin", "crypto"):
        offset = 0
        while True:
            params = {
                "tag_slug": tag,
                "active": "true",
                "closed": "false",
                "limit": PAGE_LIMIT,
                "offset": offset,
            }
            page = _fetch_markets_page(params)
            if not page:
                break
            for m in page:
                mid = m.get("id") or m.get("conditionId")
                if mid and mid not in seen_ids:
                    seen_ids.add(mid)
                    all_raw.append(m)
            if len(page) < PAGE_LIMIT:
                break
            offset += PAGE_LIMIT

    # Strategy 2: question contains "BTC" (catches markets missing btc tag)
    for kw in ("BTC", "Bitcoin"):
        offset = 0
        while True:
            params = {
                "question_contains": kw,
                "active": "true",
                "closed": "false",
                "limit": PAGE_LIMIT,
                "offset": offset,
            }
            page = _fetch_markets_page(params)
            if not page:
                break
            for m in page:
                mid = m.get("id") or m.get("conditionId")
                if mid and mid not in seen_ids:
                    seen_ids.add(mid)
                    all_raw.append(m)
            if len(page) < PAGE_LIMIT:
                break
            offset += PAGE_LIMIT

    log.log_system_event(
        "gamma_fetch_complete",
        detail=f"fetched {len(all_raw)} unique raw markets from Gamma",
    )
    return all_raw


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def list_candidate_btc_markets() -> List[MarketRecord]:
    """
    Fetch, parse, and return markets that belong to the two allowed families.

    Allowed:  btc-updown-15m-*  (family_label="15m")
              btc-updown-5m-*   (family_label="5m")
    Excluded: everything else — including all generic will-btc-be-above-*,
              bitcoin-above, price-target markets, and any BTC market that
              does not match the strict slug-prefix contract.

    Returns all records (candidates + excluded) with .excluded / .exclusion_reason set.
    Never silently drops — every rejection is recorded.
    """
    now_ms = _now_ms()
    raw_list = _fetch_all_active_btc_candidates()

    candidates: List[MarketRecord] = []
    excluded: List[MarketRecord] = []
    parse_failures = 0

    for raw in raw_list:
        m = _normalise_market(raw)
        if m is None:
            parse_failures += 1
            continue

        # Filter 1: strict family slug-prefix match
        family = _detect_family(m)
        if family is None:
            m.excluded = True
            m.exclusion_reason = (
                f"not_in_allowed_family (slug={m.market_slug!r}); "
                "only btc-updown-15m-* and btc-updown-5m-* are accepted"
            )
            excluded.append(m)
            continue

        # Stamp family and resolution reference on qualifying records
        m.family_label = family
        m.resolution_reference = RESOLUTION_REFERENCE

        # Filter 2: market window must not have already closed
        if not _is_not_expired(m, now_ms):
            m.excluded = True
            m.exclusion_reason = f"already_expired (end_time={m.end_time})"
            excluded.append(m)
            continue

        # Filter 3: must have tokens
        if not m.tokens:
            m.excluded = True
            m.exclusion_reason = "no_tokens"
            excluded.append(m)
            continue

        candidates.append(m)

    if parse_failures:
        log.log_system_event(
            "gamma_parse_failures",
            detail=f"{parse_failures} market records could not be identified at all",
            level="warning",
        )

    by_family = {"15m": sum(1 for m in candidates if m.family_label == "15m"),
                 "5m":  sum(1 for m in candidates if m.family_label == "5m")}
    log.log_system_event(
        "gamma_candidates",
        detail=(
            f"candidates={len(candidates)} (15m={by_family['15m']} 5m={by_family['5m']}) "
            f"excluded={len(excluded)} parse_failures={parse_failures} total_raw={len(raw_list)}"
        ),
    )
    return candidates + excluded  # caller filters by .excluded


def _select_front_from_family(
    candidates: List[MarketRecord],
    excluded: List[MarketRecord],
    family: str,
) -> Optional[MarketRecord]:
    """
    Internal: select the front (earliest end_time, accepting_orders first) market
    from a pre-filtered list of candidates for a given family label.
    """
    pool = [m for m in candidates if m.family_label == family]

    def sort_key(m: MarketRecord):
        accepting = 0 if _is_accepting_orders(m) else 1
        end = m.end_time or m.resolution_time or int(9e15)
        return (accepting, end)

    for m in sorted(pool, key=sort_key):
        if len(m.tokens) < 2:
            m.excluded = True
            m.exclusion_reason = "fewer_than_2_tokens"
            excluded.append(m)
            continue
        m.selected = True
        return m

    return None


def select_markets_by_family(now_ts: int) -> Dict[str, Optional[MarketRecord]]:
    """
    Discover and select the front market for each allowed family.

    Returns:
      {
        "15m": MarketRecord | None,   # primary execution lane
        "5m":  MarketRecord | None,   # observer / regime / gate lane
      }

    Selection priority within each family:
      1. accepting_orders == True
      2. earliest end_time (front expiry)
      3. at least 2 tokens

    Logs a market_selection event for each family.
    Updates the module-level cache.
    """
    all_markets = list_candidate_btc_markets()
    candidates = [m for m in all_markets if not m.excluded]
    excluded   = [m for m in all_markets if m.excluded]

    result: Dict[str, Optional[MarketRecord]] = {"15m": None, "5m": None}

    for family in ("15m", "5m"):
        sel = _select_front_from_family(candidates, excluded, family)
        result[family] = sel

        log.log_market_selection(
            selected_market=sel,
            candidates=[m for m in candidates if m.family_label == family and not m.excluded],
            excluded=excluded,
        )
        if sel is None:
            log.log_system_event(
                "no_market_selected",
                detail=f"no accepting market found for family={family}",
                level="warning",
                extra={"family": family},
            )
        else:
            log.log_system_event(
                "market_selected",
                detail=f"family={family} slug={sel.market_slug} end={sel.end_time}",
                extra={"family": family, "market_slug": sel.market_slug,
                       "end_time": sel.end_time, "token_ids": [t.token_id for t in sel.tokens]},
            )

    with _cache_lock:
        global _cache_markets, _cache_ts
        _cache_markets = all_markets
        _cache_ts = time.time()

    return result


def select_front_short_horizon_btc_market(now_ts: int) -> Optional[MarketRecord]:
    """
    Backward-compatible wrapper: returns the front 15m market only.
    Prefer select_markets_by_family() for dual-lane operation.
    """
    return select_markets_by_family(now_ts).get("15m")


def refresh_market_cache() -> None:
    """
    Force-refresh the in-memory market cache.
    Call this periodically (e.g. every 60s) to detect market expiry / new windows.
    """
    global _cache_markets, _cache_ts
    log.log_system_event("market_cache_refresh", detail="forcing cache refresh")
    all_markets = list_candidate_btc_markets()
    with _cache_lock:
        _cache_markets = all_markets
        _cache_ts = time.time()
    log.log_system_event(
        "market_cache_refreshed",
        detail=f"cache holds {len(_cache_markets)} records",
    )


def get_cached_markets() -> List[MarketRecord]:
    """Return cached markets without network call. May be empty if not yet populated."""
    with _cache_lock:
        return list(_cache_markets)
