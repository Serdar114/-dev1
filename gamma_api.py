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
from typing import Any, Dict, List, Optional, Set, Tuple

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
# Rejection reason codes — used in every discovery log record
# ---------------------------------------------------------------------------

REASON_NO_SLUG_MATCH       = "no_slug_match"        # slug/event_slug doesn't match allowed prefixes
REASON_TIMING_REJECTED     = "timing_rejected"       # end_time already past
REASON_TOKEN_PARSE_FAILED  = "token_parse_failed"    # token extraction returned empty list
REASON_FEWER_THAN_2_TOKENS = "fewer_than_2_tokens"   # selection layer: need Up+Down pair
REASON_STATE_REJECTED      = "state_rejected"        # accepting_orders=False AND active=False
REASON_UNKNOWN_SHAPE       = "unknown_shape"         # raw dict missing required id field
REASON_SELECTION_EXCEPTION = "selection_exception"   # exception during selection logic

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
# API fetch helpers
# ---------------------------------------------------------------------------

def _unwrap_response(data: Any, context: str) -> List[Dict[str, Any]]:
    """Unwrap Gamma API response — handles bare list or {"data": [...]} wrapper."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        inner = data.get("data") or data.get("markets") or data.get("results")
        if isinstance(inner, list):
            return inner
        log.log_parse_anomaly(context, "response_root", list(data.keys()), "dict without known list key")
        return []
    log.log_parse_anomaly(context, "response_root", type(data).__name__, "unexpected root type")
    return []


def _fetch_markets_page(params: Dict[str, Any]) -> List[Dict[str, Any]]:
    try:
        resp = requests.get(MARKETS_ENDPOINT, params=params, timeout=HTTP_TIMEOUT_S)
        resp.raise_for_status()
        return _unwrap_response(resp.json(), "gamma_api.markets")
    except requests.RequestException as exc:
        log.log_exception("gamma_api._fetch_markets_page", exc, {"params": params})
        return []
    except ValueError as exc:
        log.log_exception("gamma_api._fetch_markets_page.json_decode", exc)
        return []


def _fetch_events_page(params: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Fetch one page from the /events endpoint."""
    try:
        resp = requests.get(EVENTS_ENDPOINT, params=params, timeout=HTTP_TIMEOUT_S)
        resp.raise_for_status()
        return _unwrap_response(resp.json(), "gamma_api.events")
    except requests.RequestException as exc:
        log.log_exception("gamma_api._fetch_events_page", exc, {"params": params})
        return []
    except ValueError as exc:
        log.log_exception("gamma_api._fetch_events_page.json_decode", exc)
        return []


def _markets_from_events(
    events: List[Dict[str, Any]], seen_ids: Set[str]
) -> List[Dict[str, Any]]:
    """
    Extract market dicts from Gamma event objects.

    btc-updown-* slugs are event slugs. Each event object carries a `markets`
    array with the Up and Down market records. We stamp `eventSlug` onto each
    child market so _detect_family can route by event slug when the market's
    own slug doesn't start with the allowed prefix.
    """
    result: List[Dict[str, Any]] = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        ev_slug = ev.get("slug") or ev.get("eventSlug") or ev.get("market_slug") or ""
        raw_markets = ev.get("markets")
        if not isinstance(raw_markets, list):
            # Some shapes embed markets differently; try top-level as a market itself
            raw_markets = []
            if ev.get("id") or ev.get("conditionId"):
                raw_markets = [ev]

        for m in raw_markets:
            if not isinstance(m, dict):
                continue
            mid = str(m.get("id") or m.get("conditionId") or "")
            if not mid or mid in seen_ids:
                continue
            seen_ids.add(mid)
            # Stamp event slug if not already present — critical for _detect_family
            m_copy = dict(m)
            if not m_copy.get("eventSlug") and not m_copy.get("event_slug"):
                m_copy["eventSlug"] = ev_slug
            result.append(m_copy)
    return result


def _collect_from_markets_endpoint(
    params: Dict[str, Any], seen_ids: Set[str]
) -> List[Dict[str, Any]]:
    """Page through /markets with given params; dedup by seen_ids."""
    result: List[Dict[str, Any]] = []
    offset = params.pop("_offset_start", 0)
    while True:
        p = {**params, "limit": PAGE_LIMIT, "offset": offset}
        page = _fetch_markets_page(p)
        if not page:
            break
        for m in page:
            mid = str(m.get("id") or m.get("conditionId") or "")
            if mid and mid not in seen_ids:
                seen_ids.add(mid)
                result.append(m)
        if len(page) < PAGE_LIMIT:
            break
        offset += PAGE_LIMIT
    return result


def _collect_from_events_endpoint(
    params: Dict[str, Any], seen_ids: Set[str]
) -> List[Dict[str, Any]]:
    """Page through /events with given params; extract markets; dedup by seen_ids."""
    result: List[Dict[str, Any]] = []
    offset = params.pop("_offset_start", 0)
    while True:
        p = {**params, "limit": PAGE_LIMIT, "offset": offset}
        events = _fetch_events_page(p)
        if not events:
            break
        result.extend(_markets_from_events(events, seen_ids))
        if len(events) < PAGE_LIMIT:
            break
        offset += PAGE_LIMIT
    return result


def _fetch_all_active_btc_candidates() -> List[Dict[str, Any]]:
    """
    Multi-strategy BTC candidate fetch.

    Strategy 1 (PRIMARY) — /events slug-prefix search:
      btc-updown-15m-* and btc-updown-5m-* are event slugs on Polymarket.
      Each event embeds Up + Down markets. We query /events by the known
      family prefixes. Server-side active/closed filters are intentionally
      omitted here — short-horizon windows cycle states quickly and the
      normaliser will record state flags; we must not pre-filter at the API layer.

    Strategy 2 — /events tag search:
      Query /events with tag_slug=btc/bitcoin to catch events that the slug
      search misses (e.g. if the API doesn't support slug_contains).

    Strategy 3 — /markets tag search (existing approach, no active/closed filter):
      Kept as fallback. active/closed removed so we don't miss markets whose
      state the API reports differently from what we expect.

    Strategy 4 — /markets question_contains:
      Broad fallback. Also no active/closed filter.
    """
    all_raw: List[Dict[str, Any]] = []
    seen_ids: Set[str] = set()

    # ---- Strategy 1: /events by slug prefix ----
    for prefix in ("btc-updown-15m", "btc-updown-5m", "btc-updown"):
        found = _collect_from_events_endpoint({"slug": prefix}, seen_ids)
        if not found:
            # Also try slug_contains if the API supports it
            found = _collect_from_events_endpoint({"slug_contains": prefix}, seen_ids)
        all_raw.extend(found)
        log.log_system_event(
            "gamma_events_fetch",
            detail=f"events slug={prefix!r}: {len(found)} markets extracted",
            extra={"strategy": "events_slug", "prefix": prefix, "count": len(found)},
        )

    # ---- Strategy 2: /events tag search ----
    for tag in ("btc", "bitcoin", "crypto"):
        found = _collect_from_events_endpoint({"tag_slug": tag}, seen_ids)
        all_raw.extend(found)
        log.log_system_event(
            "gamma_events_fetch",
            detail=f"events tag_slug={tag!r}: {len(found)} new markets",
            extra={"strategy": "events_tag", "tag": tag, "count": len(found)},
        )

    # ---- Strategy 3: /markets tag search (no active/closed filter) ----
    for tag in ("btc", "bitcoin", "crypto"):
        found = _collect_from_markets_endpoint({"tag_slug": tag}, seen_ids)
        all_raw.extend(found)

    # ---- Strategy 4: /markets question_contains ----
    for kw in ("BTC", "Bitcoin", "btc updown", "btc-updown"):
        found = _collect_from_markets_endpoint({"question_contains": kw}, seen_ids)
        all_raw.extend(found)

    log.log_system_event(
        "gamma_fetch_complete",
        detail=f"fetched {len(all_raw)} unique raw market records across all strategies",
        extra={"total_candidates": len(all_raw)},
    )
    return all_raw


# ---------------------------------------------------------------------------
# Per-candidate discovery log (written to markets.jsonl)
# ---------------------------------------------------------------------------

def _log_discovery_candidate(
    m: MarketRecord,
    status: str,
    reason: str,
) -> None:
    """
    Write one structured record to markets.jsonl for every candidate seen.

    status : "accepted" | "rejected"
    reason : one of the REASON_* constants, or a free-form extension string

    This is the audit trail that makes 'why is market_discovery empty?' answerable.
    """
    rec: Dict[str, Any] = {
        "ts_local": _now_ms(),
        "event": "discovery_candidate",
        "status": status,
        "reason": reason,
        "market_slug": m.market_slug,
        "event_slug": m.event_slug,
        "market_id": m.market_id,
        "family_label": m.family_label,
        "active": m.active,
        "closed": m.closed,
        "accepting_orders": m.accepting_orders,
        "end_time": m.end_time,
        "token_count": len(m.tokens),
        "parse_warnings": m.parse_warnings,
    }
    log.streams.markets.write(rec)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def list_candidate_btc_markets() -> List[MarketRecord]:
    """
    Fetch, parse, and return markets that belong to the two allowed families.

    Acceptance:
      btc-updown-15m-<unix_ts>  (family_label="15m") — primary execution family
      btc-updown-5m-<unix_ts>   (family_label="5m")  — observer / gate family

    The match is performed on market_slug first, then event_slug (fallback).
    The unix timestamp suffix is not validated — any suffix is accepted.

    Every candidate is logged to markets.jsonl with status + reason code so
    the discovery audit trail is never empty when candidates exist.

    Returns all records (accepted + excluded) with .excluded / .exclusion_reason set.
    """
    now_ms = _now_ms()
    raw_list = _fetch_all_active_btc_candidates()

    if not raw_list:
        log.log_system_event(
            "gamma_no_raw_candidates",
            detail="all fetch strategies returned 0 raw market records",
            level="warning",
            extra={"reason": "no_raw_candidates_from_api"},
        )

    candidates: List[MarketRecord] = []
    excluded: List[MarketRecord] = []
    parse_failures = 0

    for raw in raw_list:
        m = _normalise_market(raw)
        if m is None:
            parse_failures += 1
            # Write a minimal record even for completely unparseable inputs
            log.streams.markets.write({
                "ts_local": _now_ms(),
                "event": "discovery_candidate",
                "status": "rejected",
                "reason": REASON_UNKNOWN_SHAPE,
                "raw_id": str(raw.get("id") or raw.get("conditionId") or "?")[:80],
                "raw_slug": str(raw.get("slug") or raw.get("marketSlug") or "?")[:80],
            })
            continue

        # ----------------------------------------------------------------
        # Filter 1: strict family slug-prefix match
        # Accepts: btc-updown-15m-<any>  or  btc-updown-5m-<any>
        # Checks market_slug first; falls back to event_slug
        # ----------------------------------------------------------------
        family = _detect_family(m)
        if family is None:
            m.excluded = True
            m.exclusion_reason = (
                f"{REASON_NO_SLUG_MATCH}: slug={m.market_slug!r} "
                f"event_slug={m.event_slug!r}; "
                "only btc-updown-15m-* and btc-updown-5m-* accepted"
            )
            excluded.append(m)
            _log_discovery_candidate(m, "rejected", REASON_NO_SLUG_MATCH)
            continue

        # Stamp family + resolution reference on qualifying records
        m.family_label = family
        m.resolution_reference = RESOLUTION_REFERENCE

        # ----------------------------------------------------------------
        # Filter 2: market window must not have already closed
        # ----------------------------------------------------------------
        if not _is_not_expired(m, now_ms):
            m.excluded = True
            m.exclusion_reason = (
                f"{REASON_TIMING_REJECTED}: end_time={m.end_time} now={now_ms}"
            )
            excluded.append(m)
            _log_discovery_candidate(m, "rejected", REASON_TIMING_REJECTED)
            continue

        # ----------------------------------------------------------------
        # Filter 3: must have at least one token (Up or Down)
        # ----------------------------------------------------------------
        if not m.tokens:
            m.excluded = True
            m.exclusion_reason = REASON_TOKEN_PARSE_FAILED
            excluded.append(m)
            _log_discovery_candidate(m, "rejected", REASON_TOKEN_PARSE_FAILED)
            continue

        # Accepted — log before appending
        _log_discovery_candidate(m, "accepted", f"family={family}")
        candidates.append(m)

    if parse_failures:
        log.log_system_event(
            "gamma_parse_failures",
            detail=f"{parse_failures} market records dropped ({REASON_UNKNOWN_SHAPE})",
            level="warning",
            extra={"count": parse_failures, "reason": REASON_UNKNOWN_SHAPE},
        )

    by_family = {
        "15m": sum(1 for m in candidates if m.family_label == "15m"),
        "5m":  sum(1 for m in candidates if m.family_label == "5m"),
    }
    log.log_system_event(
        "gamma_candidates",
        detail=(
            f"candidates={len(candidates)} "
            f"(15m={by_family['15m']} 5m={by_family['5m']}) "
            f"excluded={len(excluded)} parse_failures={parse_failures} "
            f"total_raw={len(raw_list)}"
        ),
        extra={
            "candidates_15m": by_family["15m"],
            "candidates_5m": by_family["5m"],
            "excluded": len(excluded),
            "parse_failures": parse_failures,
            "total_raw": len(raw_list),
        },
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
            m.exclusion_reason = REASON_FEWER_THAN_2_TOKENS
            excluded.append(m)
            _log_discovery_candidate(m, "rejected", REASON_FEWER_THAN_2_TOKENS)
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
        try:
            sel = _select_front_from_family(candidates, excluded, family)
        except Exception as exc:
            log.log_exception(
                f"gamma_api.select_front.{family}", exc,
                {"reason": REASON_SELECTION_EXCEPTION, "family": family},
            )
            result[family] = None
            continue

        result[family] = sel

        family_candidates = [m for m in candidates if m.family_label == family and not m.excluded]
        log.log_market_selection(
            selected_market=sel,
            candidates=family_candidates,
            excluded=excluded,
        )
        if sel is None:
            # Distinguish: were there candidates that all failed token check, or none at all?
            reason = "no_active_candidates" if not family_candidates else "no_valid_token_pair"
            log.log_system_event(
                "no_market_selected",
                detail=f"family={family} reason={reason}",
                level="warning",
                extra={"family": family, "reason": reason,
                       "candidate_count": len(family_candidates)},
            )
            # Write explicit discovery_audit record so markets.jsonl is never silent
            log.streams.markets.write({
                "ts_local": _now_ms(),
                "event": "discovery_audit",
                "family": family,
                "selected": None,
                "reason": reason,
                "candidate_count": len(family_candidates),
                "total_raw": len(candidates) + len(excluded),
            })
        else:
            log.log_system_event(
                "market_selected",
                detail=f"family={family} slug={sel.market_slug} end={sel.end_time}",
                extra={
                    "family": family,
                    "market_slug": sel.market_slug,
                    "end_time": sel.end_time,
                    "token_ids": [t.token_id for t in sel.tokens],
                    "accepting_orders": sel.accepting_orders,
                    "active": sel.active,
                },
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
