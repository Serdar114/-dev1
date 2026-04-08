"""
market_discovery.py — Discovers active BTC 5-minute markets on Polymarket.

Design:
  Queries Polymarket CLOB REST API for active, non-closed markets.
  Filters by:
    - question/title contains BTC/Bitcoin keyword
    - window duration is approximately 5 minutes (240–360 seconds)
    - market is active and not closed
  Returns only MarketIdentity objects with all fields populated.
  Partial markets are logged with full diagnostic detail and dropped.
  This module NEVER invents token IDs or prices.

  Identity resolution (priority order):
    1. condition_id / conditionId
    2. id
    3. market_slug / slug
  The first non-empty value becomes internal_market_id.
  A market is dropped only if ALL three are absent.

  Window time resolution (priority order):
    1. game_start_time / gameStartTime
    2. start_date_iso / startDateIso
    3. Slug suffix: btc-updown-5m-<unix_timestamp>
       → window_start = slug_ts, window_end = slug_ts + 300
  time_source field records which fallback was used.

  Token role assignment (priority order):
    1. outcome == "Up" → up_token_id, "Down" → down_token_id
    2. outcome == "Yes" → up_token_id, "No" → down_token_id
    3. Unrecognised → log all raw outcome strings and reject

  Rejection logging:
    Every rejected market logs:
      - top-level keys present in the raw object
      - raw slug, raw conditionId, raw gameStartTime, raw startDateIso,
        raw endDateIso, raw outcome strings
"""

from __future__ import annotations
import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any, Tuple

try:
    import aiohttp
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False

try:
    from dateutil import parser as dateutil_parser
    DATEUTIL_AVAILABLE = True
except ImportError:
    DATEUTIL_AVAILABLE = False

from loggingx.schemas import MarketIdentity

logger = logging.getLogger("polybot.market_discovery")

PAGE_LIMIT = 100

# Slug pattern for BTC 5m markets: btc-updown-5m-<unix_timestamp>
_BTC_5M_SLUG_RE = re.compile(r"btc-updown-5m-(\d+)")


def normalize_market_fields(raw: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalize a raw CLOB market dict into a canonical field set.

    Returns a dict with keys:
      internal_market_id  — best available identifier (conditionId → id → slug)
      condition_id        — conditionId / condition_id if present, else None
      id_source           — "condition_id" | "id" | "slug" | None
      slug                — market_slug / slug if present
      question            — question / title
      window_start_ts     — float unix, or None
      window_end_ts       — float unix, or None
      time_source         — "game_start_time" | "start_date_iso" | "end_date_iso" |
                            "slug_fallback" | None
      up_token_id         — str or None
      down_token_id       — str or None
      raw_outcomes        — list of raw outcome strings from tokens[]

    Never raises. Missing fields are None.
    """
    out: Dict[str, Any] = {
        "internal_market_id": None,
        "condition_id": None,
        "id_source": None,
        "slug": None,
        "question": None,
        "window_start_ts": None,
        "window_end_ts": None,
        "time_source": None,
        "up_token_id": None,
        "down_token_id": None,
        "raw_outcomes": [],
    }

    # ── Identity ─────────────────────────────────────────────────────────────
    condition_id = raw.get("condition_id") or raw.get("conditionId")
    id_field     = raw.get("id")
    slug         = raw.get("market_slug") or raw.get("slug")

    out["condition_id"] = condition_id or None
    out["slug"] = slug or None

    if condition_id:
        out["internal_market_id"] = condition_id
        out["id_source"] = "condition_id"
    elif id_field:
        out["internal_market_id"] = str(id_field)
        out["id_source"] = "id"
    elif slug:
        out["internal_market_id"] = slug
        out["id_source"] = "slug"
    # else: internal_market_id stays None → market will be rejected

    # ── Question ─────────────────────────────────────────────────────────────
    out["question"] = raw.get("question") or raw.get("title") or ""

    # ── Window times ─────────────────────────────────────────────────────────
    start_ts: Optional[float] = None
    end_ts:   Optional[float] = None
    time_source: Optional[str] = None

    # Priority 1: game_start_time / gameStartTime
    raw_start = raw.get("game_start_time") or raw.get("gameStartTime")
    if raw_start is not None:
        start_ts = _parse_timestamp(raw_start)
        if start_ts is not None:
            time_source = "game_start_time"

    # Priority 2: start_date_iso / startDateIso
    if start_ts is None:
        raw_start = raw.get("start_date_iso") or raw.get("startDateIso")
        if raw_start is not None:
            start_ts = _parse_timestamp(raw_start)
            if start_ts is not None:
                time_source = "start_date_iso"

    # End time
    raw_end = raw.get("end_date_iso") or raw.get("endDateIso") or raw.get("end_date")
    if raw_end is not None:
        end_ts = _parse_timestamp(raw_end)

    # Priority 3: slug fallback for BTC 5m pattern
    if start_ts is None and slug:
        m = _BTC_5M_SLUG_RE.search(slug)
        if m:
            slug_ts = float(m.group(1))
            start_ts = slug_ts
            # If no end_ts from API either, derive from slug + 300s
            if end_ts is None:
                end_ts = slug_ts + 300.0
            time_source = "slug_fallback"
            logger.info(
                "TIME_SOURCE=slug_fallback slug=%s start=%.0f end=%.0f",
                slug, start_ts, end_ts,
            )

    out["window_start_ts"] = start_ts
    out["window_end_ts"]   = end_ts
    out["time_source"]     = time_source

    # ── Token roles ──────────────────────────────────────────────────────────
    tokens = raw.get("tokens", [])
    up_token:   Optional[str] = None
    down_token: Optional[str] = None
    raw_outcomes: List[str] = []

    for tok in tokens:
        token_id = tok.get("token_id") or tok.get("tokenId")
        outcome  = (tok.get("outcome") or "").strip()
        raw_outcomes.append(outcome)

        if not token_id:
            continue

        outcome_lc = outcome.lower()
        if outcome_lc == "up":
            up_token = token_id
        elif outcome_lc == "down":
            down_token = token_id
        elif outcome_lc == "yes":
            up_token = token_id
        elif outcome_lc == "no":
            down_token = token_id

    out["up_token_id"]   = up_token
    out["down_token_id"] = down_token
    out["raw_outcomes"]  = raw_outcomes

    return out


def _parse_timestamp(raw: Any) -> Optional[float]:
    """Parse a timestamp string or epoch number to UTC unix float."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        if DATEUTIL_AVAILABLE:
            try:
                dt = dateutil_parser.parse(raw)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.timestamp()
            except Exception:
                pass
        for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
                return dt.timestamp()
            except ValueError:
                continue
    return None


def _log_rejection(mid: str, reason: str, raw: Dict[str, Any], raw_outcomes: List[str]) -> None:
    """Log a full diagnostic dump for every rejected market."""
    logger.warning(
        "REJECTED [%s] reason=%s | "
        "keys=%s | "
        "slug=%r | conditionId=%r | condition_id=%r | "
        "gameStartTime=%r | game_start_time=%r | "
        "startDateIso=%r | start_date_iso=%r | "
        "endDateIso=%r | end_date_iso=%r | "
        "outcomes=%s",
        mid, reason,
        sorted(raw.keys()),
        raw.get("market_slug") or raw.get("slug"),
        raw.get("conditionId"), raw.get("condition_id"),
        raw.get("gameStartTime"), raw.get("game_start_time"),
        raw.get("startDateIso"), raw.get("start_date_iso"),
        raw.get("endDateIso"), raw.get("end_date_iso"),
        raw_outcomes,
    )


class MarketDiscovery:
    """
    Discovers BTC 5m markets on Polymarket CLOB.

    Job: Return list of MarketIdentity for active BTC 5m markets only.
    Input: config (keywords, min/max window secs)
    Output: List[MarketIdentity] — only complete, validated entries
    Failure:
      - API failure → empty list + log (no crash, no guessing)
      - Partial market → skipped + logged with full diagnostic
      - Ambiguous token roles → skipped + logged with raw outcomes
    """

    def __init__(
        self,
        clob_api_url: str = "https://clob.polymarket.com",
        title_keywords: Optional[List[str]] = None,
        min_window_secs: float = 240.0,
        max_window_secs: float = 360.0,
    ):
        self._base = clob_api_url.rstrip("/")
        self._keywords = [k.lower() for k in (title_keywords or ["btc", "bitcoin"])]
        self._min_window = min_window_secs
        self._max_window = max_window_secs

    async def discover(self) -> List[MarketIdentity]:
        """
        Fetch and filter active BTC 5m markets.
        Returns empty list on any API failure — never crashes caller.
        """
        if not AIOHTTP_AVAILABLE:
            logger.error("aiohttp not installed — cannot discover markets")
            return []

        raw_markets = await self._fetch_all_active_markets()
        logger.info("Fetched %d raw active markets from CLOB", len(raw_markets))

        results: List[MarketIdentity] = []
        for raw in raw_markets:
            identity = self._parse_market(raw)
            if identity is not None:
                results.append(identity)

        logger.info(
            "Discovery: %d raw → %d BTC 5m markets accepted",
            len(raw_markets), len(results),
        )
        return results

    async def _fetch_all_active_markets(self) -> List[Dict[str, Any]]:
        """Paginate through CLOB markets endpoint, collecting all active markets."""
        all_markets: List[Dict[str, Any]] = []
        next_cursor: Optional[str] = None
        pages_fetched = 0

        try:
            async with aiohttp.ClientSession() as session:
                while True:
                    params: Dict[str, Any] = {
                        "active": "true",
                        "closed": "false",
                        "limit": PAGE_LIMIT,
                    }
                    if next_cursor:
                        params["next_cursor"] = next_cursor

                    try:
                        async with session.get(
                            f"{self._base}/markets",
                            params=params,
                            timeout=aiohttp.ClientTimeout(total=15),
                        ) as resp:
                            if resp.status != 200:
                                logger.error("CLOB markets API returned HTTP %d", resp.status)
                                break

                            data = await resp.json()
                            page_markets = data.get("data", [])
                            all_markets.extend(page_markets)
                            pages_fetched += 1

                            next_cursor = data.get("next_cursor")
                            if not next_cursor or next_cursor in ("", "LTE="):
                                break
                            if pages_fetched >= 20:
                                logger.warning("Hit 20-page limit during market discovery")
                                break

                    except asyncio.TimeoutError:
                        logger.error("Timeout fetching CLOB markets page %d", pages_fetched + 1)
                        break
                    except Exception as exc:
                        logger.error("Error fetching CLOB markets: %s", exc)
                        break

        except Exception as exc:
            logger.error("Session error in market discovery: %s", exc)

        return all_markets

    def _parse_market(self, raw: Dict[str, Any]) -> Optional[MarketIdentity]:
        """
        Normalize and validate one raw market dict.
        Returns MarketIdentity on success, None on any validation failure.
        Every rejection logs full diagnostic via _log_rejection().
        """
        norm = normalize_market_fields(raw)
        mid  = norm["internal_market_id"] or "<no-id>"

        # ── Must have some identity ──────────────────────────────────────────
        if norm["internal_market_id"] is None:
            _log_rejection("<no-id>", "no_identity", raw, norm["raw_outcomes"])
            return None

        # ── Keyword filter (silent — not a rejection) ────────────────────────
        question = norm["question"] or ""
        if not any(kw in question.lower() for kw in self._keywords):
            return None

        # ── Window start required ────────────────────────────────────────────
        if norm["window_start_ts"] is None:
            _log_rejection(mid, "no_window_start", raw, norm["raw_outcomes"])
            return None

        # ── Window end: derive from start + 300 if still missing ─────────────
        if norm["window_end_ts"] is None:
            # Last resort: if start came from slug we already set end above.
            # If start came from API fields but end is missing, log and drop.
            _log_rejection(mid, "no_window_end", raw, norm["raw_outcomes"])
            return None

        # ── Duration filter ──────────────────────────────────────────────────
        duration = norm["window_end_ts"] - norm["window_start_ts"]
        if not (self._min_window <= duration <= self._max_window):
            logger.debug(
                "[%s] Skipping: duration=%.0fs outside [%.0f, %.0f] time_source=%s",
                mid, duration, self._min_window, self._max_window, norm["time_source"],
            )
            return None

        # ── Tokens ──────────────────────────────────────────────────────────
        tokens = raw.get("tokens", [])
        if len(tokens) < 2:
            _log_rejection(mid, "fewer_than_2_tokens", raw, norm["raw_outcomes"])
            return None

        if norm["up_token_id"] is None or norm["down_token_id"] is None:
            _log_rejection(mid, "token_roles_unresolved", raw, norm["raw_outcomes"])
            return None

        # ── Build identity ───────────────────────────────────────────────────
        # condition_id must be a str for MarketIdentity; use internal_market_id
        # when the actual condition_id field was absent (id_source != condition_id).
        condition_id = norm["condition_id"] or norm["internal_market_id"]

        logger.info(
            "ACCEPTED [%s] slug=%s time_source=%s id_source=%s duration=%.0fs",
            condition_id, norm["slug"], norm["time_source"],
            norm["id_source"], duration,
        )

        return MarketIdentity(
            condition_id=condition_id,
            question=question,
            up_token_id=norm["up_token_id"],
            down_token_id=norm["down_token_id"],
            window_start_ts=norm["window_start_ts"],
            window_end_ts=norm["window_end_ts"],
            slug=norm["slug"],
            raw_end_date=str(
                raw.get("end_date_iso") or raw.get("endDateIso") or raw.get("end_date")
            ),
        )
