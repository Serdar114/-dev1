"""
discovery/market_discovery.py — Slug-first BTC 5m market discovery.

Algorithm (per spec):
  1. Compute current 5m window timestamp.
  2. Try each configured slug prefix + window_ts.
  3. If not found, try previous window timestamp.
  4. If still not found, try Gamma search by tag.
  5. Log every attempt explicitly.

clobTokenIds handling:
  - May arrive as a JSON list or a JSON string.
  - Always parse via _parse_token_ids().
  - Never treat raw string characters as token ids.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

import requests

from loggingx.schemas import DiscoveryAttemptEvent, MarketFoundEvent
from state import MarketRecord
from truth.window_clock import current_window_start, prev_window_start

log = logging.getLogger(__name__)

_TIMEOUT = 10  # seconds


def _parse_token_ids(raw: Any) -> List[str]:
    """
    Parse clobTokenIds regardless of whether it arrives as list or JSON string.
    Raises ValueError if format is unrecognisable.
    """
    if isinstance(raw, list):
        return [str(x) for x in raw]
    if isinstance(raw, str):
        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            raise ValueError(f"clobTokenIds JSON string did not decode to list: {parsed!r}")
        return [str(x) for x in parsed]
    raise ValueError(f"clobTokenIds has unexpected type {type(raw)}: {raw!r}")


def _parse_outcomes(raw: Any) -> List[str]:
    if isinstance(raw, list):
        return [str(x) for x in raw]
    if isinstance(raw, str):
        parsed = json.loads(raw)
        return [str(x) for x in parsed]
    return ["Up", "Down"]


def _market_from_gamma(record: Dict) -> Optional[MarketRecord]:
    """Convert a Gamma API market dict to a MarketRecord."""
    try:
        raw_ids = record.get("clobTokenIds")
        if raw_ids is None:
            log.warning("clobTokenIds missing from market record")
            return None
        token_ids = _parse_token_ids(raw_ids)
        if len(token_ids) < 2:
            log.warning("clobTokenIds has fewer than 2 ids: %s", token_ids)
            return None

        outcomes = _parse_outcomes(record.get("outcomes", '["Up","Down"]'))
        up_idx = 0
        down_idx = 1
        # Attempt to find correct outcome indices
        for i, o in enumerate(outcomes):
            if o.lower() in ("up", "higher"):
                up_idx = i
            elif o.lower() in ("down", "lower"):
                down_idx = i

        # Determine window start from endDate or slug
        slug = record.get("slug", "")
        window_start = _extract_window_ts_from_slug(slug)

        return MarketRecord(
            slug=slug,
            condition_id=record.get("id", record.get("conditionId", "")),
            up_token_id=token_ids[up_idx],
            down_token_id=token_ids[down_idx],
            up_outcome=outcomes[up_idx] if up_idx < len(outcomes) else "Up",
            down_outcome=outcomes[down_idx] if down_idx < len(outcomes) else "Down",
            window_start=window_start,
            window_end=window_start + 300,
            discovered_at=time.time(),
        )
    except Exception as exc:
        log.error("Failed to parse market record: %s | record=%s", exc, record)
        return None


def _extract_window_ts_from_slug(slug: str) -> int:
    """Extract Unix timestamp from slug like 'btc-up-down-5m-1234567890'."""
    parts = slug.split("-")
    for part in reversed(parts):
        try:
            val = int(part)
            if 1_000_000_000 < val < 9_999_999_999:  # plausible Unix ts
                return val
        except ValueError:
            continue
    return current_window_start()


class MarketDiscovery:
    """
    Discovers the active BTC 5m market via Gamma API.
    Tries slug-first, falls back to search.
    All attempts are explicitly logged via event_logger.
    """

    def __init__(self, config: dict, event_logger=None) -> None:
        self._gamma_base = config["polymarket"]["gamma_base"]
        self._slug_prefixes: List[str] = config["polymarket"]["slug_prefixes"]
        self._search_tag: str = config["polymarket"].get("search_tag", "btc")
        self._search_limit: int = config["polymarket"].get("search_limit", 20)
        self._logger = event_logger

    def _log(self, event) -> None:
        if self._logger:
            self._logger.log(event)

    def _query_by_slug(self, slug: str) -> Optional[Dict]:
        """Query Gamma for an exact slug. Returns first match or None."""
        url = f"{self._gamma_base}/markets"
        params = {"slug": slug}
        try:
            resp = requests.get(url, params=params, timeout=_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list) and len(data) > 0:
                return data[0]
            if isinstance(data, dict) and data:
                return data
            return None
        except Exception as exc:
            log.warning("Gamma slug query failed for %s: %s", slug, exc)
            return None

    def _search_recent(self) -> Optional[Dict]:
        """Fallback: search Gamma for recent BTC 5m markets."""
        url = f"{self._gamma_base}/markets"
        params = {
            "tag": self._search_tag,
            "active": "true",
            "closed": "false",
            "limit": self._search_limit,
        }
        try:
            resp = requests.get(url, params=params, timeout=_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            markets = data if isinstance(data, list) else data.get("markets", [])
            # Filter to 5m markets with btc
            for m in markets:
                slug = m.get("slug", "").lower()
                if "5m" in slug and "btc" in slug and ("up" in slug or "down" in slug):
                    return m
            return None
        except Exception as exc:
            log.warning("Gamma search failed: %s", exc)
            return None

    def discover(self) -> Optional[MarketRecord]:
        """
        Attempt discovery in order:
          1. Current window slug (all prefixes)
          2. Previous window slug (all prefixes)
          3. Gamma search fallback
        Returns MarketRecord or None. Every attempt is logged.
        """
        now = time.time()
        cur_ts = current_window_start(now)
        prev_ts = prev_window_start(now)

        # --- Step 1: current window ---
        for prefix in self._slug_prefixes:
            slug = f"{prefix}-{cur_ts}"
            self._log(DiscoveryAttemptEvent(
                slug_tried=slug,
                source="gamma_slug_current",
                window_id=cur_ts,
            ))
            raw = self._query_by_slug(slug)
            if raw:
                record = _market_from_gamma(raw)
                if record:
                    record.discovery_source = "gamma_slug_current"
                    self._log(MarketFoundEvent(
                        slug=record.slug,
                        condition_id=record.condition_id,
                        up_token_id=record.up_token_id,
                        down_token_id=record.down_token_id,
                        source=record.discovery_source,
                        window_id=cur_ts,
                    ))
                    return record
            self._log(DiscoveryAttemptEvent(
                slug_tried=slug, source="gamma_slug_current",
                found=False, window_id=cur_ts,
            ))

        # --- Step 2: previous window ---
        for prefix in self._slug_prefixes:
            slug = f"{prefix}-{prev_ts}"
            raw = self._query_by_slug(slug)
            if raw:
                record = _market_from_gamma(raw)
                if record:
                    record.discovery_source = "gamma_slug_prev"
                    self._log(MarketFoundEvent(
                        slug=record.slug,
                        condition_id=record.condition_id,
                        up_token_id=record.up_token_id,
                        down_token_id=record.down_token_id,
                        source=record.discovery_source,
                        window_id=cur_ts,
                    ))
                    return record

        # --- Step 3: search fallback ---
        raw = self._search_recent()
        if raw:
            record = _market_from_gamma(raw)
            if record:
                record.discovery_source = "gamma_search"
                self._log(MarketFoundEvent(
                    slug=record.slug,
                    condition_id=record.condition_id,
                    up_token_id=record.up_token_id,
                    down_token_id=record.down_token_id,
                    source="gamma_search",
                    window_id=cur_ts,
                ))
                return record

        log.warning("No BTC 5m market found at window_ts=%d", cur_ts)
        return None
