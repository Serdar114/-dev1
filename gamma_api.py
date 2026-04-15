"""
gamma_api.py — Polymarket Gamma API client for BTC up/down market discovery.

Family lock: discovers the soonest-resolving active market for each allowed
family prefix and returns a MarketPair per family.

DO NOT MODIFY: select_markets_by_family, _detect_family, ALLOWED_PREFIXES
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

import requests

from schemas import MarketPair

log = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"

ALLOWED_PREFIXES = ("btc-updown-5m-", "btc-updown-15m-")

_FAMILY_MAP: Dict[str, str] = {
    "btc-updown-5m-": "5m",
    "btc-updown-15m-": "15m",
}


# ── helpers ───────────────────────────────────────────────────────────────────

def _detect_family(slug: str) -> Optional[str]:
    for prefix, family in _FAMILY_MAP.items():
        if slug.startswith(prefix):
            return family
    return None


def _parse_float(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _parse_end_time_ms(raw: dict) -> Optional[int]:
    """Try multiple possible field names for market end time."""
    for key in ("endDate", "end_date", "closedTime", "endTime", "end_time"):
        v = raw.get(key)
        if v:
            try:
                dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
                return int(dt.timestamp() * 1000)
            except Exception:
                pass
    for key in ("endDateIso",):
        v = raw.get(key)
        if v:
            try:
                dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
                return int(dt.timestamp() * 1000)
            except Exception:
                pass
    # Try raw epoch ms / seconds
    for key in ("end_time_ms", "endTimestamp"):
        v = raw.get(key)
        if v:
            try:
                ts = int(v)
                # Heuristic: if it looks like seconds (< 1e12) convert to ms
                return ts * 1000 if ts < 10_000_000_000 else ts
            except Exception:
                pass
    return None


def _extract_tokens(raw: dict):
    """
    Return (up_token_id, down_token_id) from a market dict.
    Tries 'tokens' list first (with outcome labels), then falls back to
    positional order (first=Up, second=Down).
    """
    tokens = raw.get("tokens") or raw.get("clobTokenIds") or []
    if not tokens or len(tokens) < 2:
        return None, None

    up_tid = down_tid = None
    for t in tokens:
        if isinstance(t, dict):
            outcome = (t.get("outcome") or "").lower()
            tid = str(t.get("token_id") or t.get("tokenId") or t.get("id") or "")
            if not tid:
                continue
            if outcome in ("yes", "up"):
                up_tid = tid
            elif outcome in ("no", "down"):
                down_tid = tid

    if up_tid and down_tid:
        return up_tid, down_tid

    # Fallback: positional
    def _tid(t):
        if isinstance(t, dict):
            return str(t.get("token_id") or t.get("tokenId") or t.get("id") or "")
        return str(t)

    return _tid(tokens[0]), _tid(tokens[1])


# ── public API ────────────────────────────────────────────────────────────────

def select_markets_by_family(
    session: Optional[requests.Session] = None,
    timeout: int = 10,
) -> Dict[str, MarketPair]:
    """
    Discover active BTC up/down markets and return the soonest-resolving
    MarketPair per family.  Returns {} if no markets are found.
    """
    sess = session or requests.Session()
    now_ms = int(time.time() * 1000)
    result: Dict[str, MarketPair] = {}

    for prefix in ALLOWED_PREFIXES:
        family = _FAMILY_MAP[prefix]
        candidates: List[dict] = []

        try:
            resp = sess.get(
                f"{GAMMA_BASE}/markets",
                params={"active": "true", "limit": 50},
                timeout=timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            markets: list = data if isinstance(data, list) else data.get("markets", [])
        except Exception as exc:
            log.warning("gamma_api: fetch failed family=%s: %s", family, exc)
            continue

        for m in markets:
            slug = m.get("slug", "")
            if not slug.startswith(prefix):
                continue
            end_ms = _parse_end_time_ms(m)
            if end_ms is None or end_ms <= now_ms:
                continue
            candidates.append(m)

        if not candidates:
            log.debug("gamma_api: no active candidates for family=%s", family)
            continue

        # Lock onto soonest-resolving
        candidates.sort(key=lambda m: _parse_end_time_ms(m) or float("inf"))
        m = candidates[0]

        up_tid, down_tid = _extract_tokens(m)
        if not up_tid or not down_tid:
            log.warning("gamma_api: cannot extract tokens for slug=%s", m.get("slug"))
            continue

        end_ms = _parse_end_time_ms(m)

        # Start price / barrier ("Price to Beat")
        extra = m.get("extra") or {}
        start_price = _parse_float(
            m.get("startingPrice")
            or m.get("startPrice")
            or m.get("start_price")
            or extra.get("startingPrice")
            or extra.get("startPrice")
        )

        fee_rate = _parse_float(m.get("feeRate") or m.get("makerBaseFee") or m.get("fee_rate"))
        fee_c = _parse_float(m.get("feeC") or m.get("fee_c"))

        pair = MarketPair(
            family=family,
            condition_id=str(m.get("conditionId") or m.get("id") or ""),
            slug=m.get("slug", ""),
            end_time_ms=end_ms,
            up_token_id=up_tid,
            down_token_id=down_tid,
            start_price=start_price,
            fee_rate=fee_rate,
            fee_c=fee_c,
        )
        result[family] = pair
        log.info(
            "gamma_api: locked family=%s slug=%s end_ms=%d start_price=%s",
            family, pair.slug, pair.end_time_ms, pair.start_price,
        )

    return result
