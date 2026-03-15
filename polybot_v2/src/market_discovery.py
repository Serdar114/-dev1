"""
Discovers the current active Polymarket BTC 5-minute market.

Strategy:
  1. Search Gamma API for BTC 5m markets that are not yet closed.
  2. Match a market whose end_date falls within the current 5-minute window.
  3. Cache result until window rolls over.
  4. Return (condition_id, token_id_yes, token_id_no, window_end_ts).
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Optional

from polymarket_client import PolymarketClient

log = logging.getLogger(__name__)

# How many seconds before window end we accept a match
_WINDOW_LOOKAHEAD_SEC = 30
# How many past seconds we allow (market just opened)
_WINDOW_LOOKBEHIND_SEC = 290


@dataclass
class ActiveMarket:
    condition_id: str
    token_id_yes: str
    token_id_no: str
    window_end_ts: float
    slug: str


class MarketDiscovery:
    def __init__(self, client: PolymarketClient, window_sec: int = 300) -> None:
        self._client = client
        self._window_sec = window_sec
        self._cache: Optional[ActiveMarket] = None
        self._cache_expires: float = 0.0

    def get_current_market(self) -> Optional[ActiveMarket]:
        """
        Return the active 5m BTC market for the current window.
        Returns None if no suitable market is found.
        Result is cached until the window rolls over.
        """
        now = time.time()
        if self._cache and now < self._cache_expires:
            return self._cache

        market = self._discover(now)
        if market:
            # cache until just after window end
            self._cache = market
            self._cache_expires = market.window_end_ts + 10
            log.info(
                "Discovered market: %s | ends in %.0fs",
                market.slug,
                market.window_end_ts - now,
            )
        else:
            # short negative-cache to avoid hammering API
            self._cache = None
            self._cache_expires = now + 10
            log.warning("No active BTC 5m market found for current window")

        return self._cache

    def invalidate(self) -> None:
        self._cache = None
        self._cache_expires = 0.0

    # ------------------------------------------------------------------ #
    # Internal
    # ------------------------------------------------------------------ #

    def _discover(self, now: float) -> Optional[ActiveMarket]:
        # Search terms that Polymarket uses for BTC 5m markets
        search_terms = [
            "Will Bitcoin be higher in 5 minutes",
            "Bitcoin 5-minute",
            "BTC 5 min",
        ]
        candidates: list[dict] = []
        for term in search_terms:
            results = self._client.search_markets(term, limit=20)
            candidates.extend(results)
            if len(candidates) >= 5:
                break

        for raw in candidates:
            market = self._parse_candidate(raw, now)
            if market:
                return market

        # fallback: try tag-based search
        tag_results = self._client.get_markets(tag="bitcoin", limit=30)
        for raw in tag_results:
            if self._is_btc_5m(raw):
                market = self._parse_candidate(raw, now)
                if market:
                    return market

        return None

    def _is_btc_5m(self, raw: dict) -> bool:
        question = (raw.get("question") or raw.get("title") or "").lower()
        return ("bitcoin" in question or "btc" in question) and (
            "5 min" in question or "5-min" in question or "5minute" in question
        )

    def _parse_candidate(self, raw: dict, now: float) -> Optional[ActiveMarket]:
        try:
            # end_date can be ISO string or unix ts
            end_date = raw.get("endDate") or raw.get("end_date_iso") or raw.get("end_time")
            if not end_date:
                return None

            window_end_ts = self._parse_timestamp(end_date)
            if window_end_ts is None:
                return None

            # Must be in current window
            if window_end_ts < now - _WINDOW_LOOKBEHIND_SEC:
                return None
            if window_end_ts > now + _WINDOW_LOOKAHEAD_SEC + self._window_sec:
                return None

            # Extract token ids
            outcomes = raw.get("outcomes", [])
            token_ids = raw.get("clobTokenIds") or raw.get("clob_token_ids") or []

            if isinstance(token_ids, str):
                import json
                token_ids = json.loads(token_ids)

            # outcomes order: [YES, NO] or we infer from names
            yes_idx, no_idx = 0, 1
            if isinstance(outcomes, list) and len(outcomes) >= 2:
                for i, o in enumerate(outcomes):
                    name = (o if isinstance(o, str) else o.get("value", "")).upper()
                    if name == "YES":
                        yes_idx = i
                    elif name == "NO":
                        no_idx = i

            if not token_ids or len(token_ids) < 2:
                return None

            condition_id = raw.get("conditionId") or raw.get("condition_id") or ""
            if not condition_id:
                return None

            return ActiveMarket(
                condition_id=condition_id,
                token_id_yes=str(token_ids[yes_idx]),
                token_id_no=str(token_ids[no_idx]),
                window_end_ts=window_end_ts,
                slug=raw.get("slug", raw.get("question", "")[:40]),
            )
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            log.debug("_parse_candidate error: %s | raw keys=%s", exc, list(raw.keys()))
            return None

    @staticmethod
    def _parse_timestamp(value: object) -> Optional[float]:
        if isinstance(value, (int, float)):
            ts = float(value)
            # if ms epoch
            if ts > 1e12:
                ts /= 1000.0
            return ts
        if isinstance(value, str):
            import datetime
            for fmt in (
                "%Y-%m-%dT%H:%M:%SZ",
                "%Y-%m-%dT%H:%M:%S.%fZ",
                "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%d %H:%M:%S",
            ):
                try:
                    dt = datetime.datetime.strptime(value.rstrip("Z") + "Z" if "Z" not in value and "+" not in value else value, fmt)
                    if dt.tzinfo is None:
                        import datetime as dt2
                        dt = dt.replace(tzinfo=dt2.timezone.utc)
                    return dt.timestamp()
                except ValueError:
                    continue
            # try numeric string
            try:
                return MarketDiscovery._parse_timestamp(float(value))
            except ValueError:
                pass
        return None
