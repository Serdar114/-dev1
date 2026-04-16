"""
Polymarket Gamma REST API client.

Discovers active BTC up/down 5m and 15m markets.
For each family, selects the soonest-resolving active market.

Slug patterns (verified):
  5m:  btc-updown-5m-{unix_timestamp_seconds}
  15m: btc-updown-15m-{unix_timestamp_seconds}

Token extraction: matches "Up"/"Down" in outcome names; falls back to
positional order (index 0 = Up, index 1 = Down).

Fields that cannot be found are left as None — no dummy values.
"""
import time
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

import requests

from schemas import MarketPair

GAMMA_BASE = "https://gamma-api.polymarket.com"
ALLOWED_PREFIXES = ("btc-updown-5m-", "btc-updown-15m-")


# ------------------------------------------------------------------ #
# Slug / family detection                                             #
# ------------------------------------------------------------------ #

def _detect_family(slug: str) -> Optional[str]:
    if slug.startswith("btc-updown-5m-"):
        return "5m"
    if slug.startswith("btc-updown-15m-"):
        return "15m"
    return None


# ------------------------------------------------------------------ #
# Field parsing helpers                                               #
# ------------------------------------------------------------------ #

def _parse_float(val) -> Optional[float]:
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _parse_end_time(end_date) -> Optional[int]:
    """
    Parse an end date value into Unix milliseconds.
    Accepts: ISO-8601 string, numeric string, int/float (seconds or ms).
    Returns None if parsing fails.
    """
    if end_date is None:
        return None

    if isinstance(end_date, (int, float)):
        val = int(end_date)
        # Heuristic: < 1e12 → seconds, else → milliseconds
        return val * 1000 if val < 1_000_000_000_000 else val

    if isinstance(end_date, str):
        # Attempt ISO 8601 via fromisoformat (Python 3.7+).
        # Replace trailing Z with +00:00 for compatibility.
        try:
            s = end_date.strip().replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        except ValueError:
            pass

        # Fallback: numeric string
        try:
            val = int(end_date)
            return val * 1000 if val < 1_000_000_000_000 else val
        except ValueError:
            pass

    return None


# ------------------------------------------------------------------ #
# Token extraction                                                    #
# ------------------------------------------------------------------ #

def _extract_tokens(market: dict) -> Tuple[Optional[str], Optional[str]]:
    """
    Return (up_token_id, down_token_id).

    Strategy:
      1. Try market["tokens"] list, matching "up"/"down" in the outcome field.
      2. Try market["clobTokenIds"] with market["outcomes"] for matching.
      3. Positional fallback: index 0 = Up, index 1 = Down.
    """
    up_token: Optional[str] = None
    down_token: Optional[str] = None

    tokens = market.get("tokens") or []
    outcomes = market.get("outcomes") or []

    if tokens:
        for i, token in enumerate(tokens):
            if isinstance(token, dict):
                token_id = token.get("token_id") or token.get("id")
                outcome_name = str(token.get("outcome", "")).lower()
            else:
                token_id = str(token)
                outcome_name = str(outcomes[i]).lower() if i < len(outcomes) else ""

            if "up" in outcome_name:
                up_token = token_id
            elif "down" in outcome_name:
                down_token = token_id

        # Positional fallback when outcome matching failed
        if up_token is None and len(tokens) >= 1:
            t = tokens[0]
            up_token = (t.get("token_id") or t.get("id")) if isinstance(t, dict) else str(t)
        if down_token is None and len(tokens) >= 2:
            t = tokens[1]
            down_token = (t.get("token_id") or t.get("id")) if isinstance(t, dict) else str(t)

        return up_token, down_token

    # Try clobTokenIds + outcomes
    clob_ids = market.get("clobTokenIds") or []
    if clob_ids:
        for i, token_id in enumerate(clob_ids):
            outcome_name = str(outcomes[i]).lower() if i < len(outcomes) else ""
            if "up" in outcome_name:
                up_token = str(token_id)
            elif "down" in outcome_name:
                down_token = str(token_id)

        if up_token is None and len(clob_ids) >= 1:
            up_token = str(clob_ids[0])
        if down_token is None and len(clob_ids) >= 2:
            down_token = str(clob_ids[1])

    return up_token, down_token


# ------------------------------------------------------------------ #
# Public API                                                          #
# ------------------------------------------------------------------ #

def select_markets_by_family(session: requests.Session) -> Dict[str, MarketPair]:
    """
    Fetch active BTC up/down markets from Gamma API.

    Returns a dict mapping family string ("5m" or "15m") to the
    soonest-resolving active MarketPair for that family.
    If no market is found for a family the key is absent.
    All errors are silently swallowed so callers get a partial or
    empty dict rather than an exception.
    """
    now_ms = int(time.time() * 1000)
    result: Dict[str, MarketPair] = {}

    try:
        params = {
            "active": "true",
            "limit": 100,
        }
        resp = session.get(f"{GAMMA_BASE}/markets", params=params, timeout=15)
        resp.raise_for_status()
        raw = resp.json()
    except Exception:
        return result

    # Normalise response shape — some endpoints wrap in {"markets": [...]}
    if isinstance(raw, dict):
        markets = raw.get("markets") or raw.get("data") or []
    elif isinstance(raw, list):
        markets = raw
    else:
        return result

    # Group candidates by family; keep all that are still in the future.
    family_candidates: Dict[str, list] = {"5m": [], "15m": []}

    for market in markets:
        if not isinstance(market, dict):
            continue

        slug = market.get("slug", "")
        family = _detect_family(slug)
        if family is None:
            continue

        end_date = (
            market.get("endDate")
            or market.get("end_date_iso")
            or market.get("endDateIso")
            or market.get("end_time")
            or market.get("endTime")
        )
        end_time_ms = _parse_end_time(end_date)
        if end_time_ms is None or end_time_ms <= now_ms:
            continue

        family_candidates[family].append((end_time_ms, market))

    for family, candidates in family_candidates.items():
        if not candidates:
            continue

        # Soonest-resolving first
        candidates.sort(key=lambda x: x[0])
        end_time_ms, market = candidates[0]

        condition_id = (
            market.get("conditionId")
            or market.get("condition_id")
            or ""
        )
        slug = market.get("slug", "")

        up_token, down_token = _extract_tokens(market)
        if not up_token or not down_token:
            continue

        start_price = _parse_float(
            market.get("startPrice")
            or market.get("start_price")
            or market.get("barrier")
            or market.get("initialPrice")
            or market.get("initial_price")
        )
        fee_rate = _parse_float(
            market.get("feeRate") or market.get("fee_rate")
        )
        fee_c = _parse_float(
            market.get("feeC") or market.get("fee_c") or market.get("c")
        )

        result[family] = MarketPair(
            family=family,
            condition_id=condition_id,
            slug=slug,
            end_time_ms=end_time_ms,
            up_token_id=up_token,
            down_token_id=down_token,
            start_price=start_price,
            fee_rate=fee_rate,
            fee_c=fee_c,
        )

    return result
