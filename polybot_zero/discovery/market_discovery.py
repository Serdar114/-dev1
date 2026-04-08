"""
market_discovery.py — BTC 5m market discovery, slug-first via Gamma API.

Discovery order:
  1. Compute current 5m window timestamp (floor(now / 300) * 300)
  2. Build slug: btc-updown-5m-{window_ts}
  3. Query Gamma API by exact slug → parse market
  4. If not found, try previous window slug (window_ts - 300)
  5. Only then fall back to broad CLOB pagination scan

Gamma API is preferred because:
  - Exact-slug lookup is O(1) vs scanning 1000+ markets
  - Gamma returns clobTokenIds + outcomes in one response
  - Token IDs needed for CLOB WS subscription come directly from Gamma

clobTokenIds parsing:
  Gamma may return clobTokenIds as a real list OR as a JSON string.
  e.g. "[\"tok1\",\"tok2\"]"  →  json.loads() before use.
  Never iterate raw string characters as token IDs.

Token role assignment (Gamma path):
  Use outcomes[] array positionally paired with clobTokenIds[]:
    outcomes[i].lower() in ("up","yes") → up_token_id = clobTokenIds[i]
    outcomes[i].lower() in ("down","no") → down_token_id = clobTokenIds[i]
  Positional fallback (no outcomes field):
    clobTokenIds[0] → up_token_id
    clobTokenIds[1] → down_token_id

Window time parsing (Gamma path) — same priority as CLOB path:
  1. gameStartTime / startDate
  2. Slug suffix: btc-updown-5m-<unix_timestamp>
     → window_start = slug_ts, window_end = slug_ts + 300

normalize_market_fields(raw) — retained for CLOB fallback path, unchanged.
"""

from __future__ import annotations
import asyncio
import json
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

GAMMA_API_URL  = "https://gamma-api.polymarket.com"
CLOB_API_URL   = "https://clob.polymarket.com"
PAGE_LIMIT     = 100
WINDOW_SECS    = 300  # 5 minutes

_BTC_5M_SLUG_RE = re.compile(r"btc-updown-5m-(\d+)")


# ─────────────────────────────────────────────────────────────────────────────
# Public normalizer — retained for CLOB fallback path
# ─────────────────────────────────────────────────────────────────────────────

def normalize_market_fields(raw: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalize a raw CLOB market dict into a canonical field set.
    Used by the broad CLOB fallback scan only.
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

    out["question"] = raw.get("question") or raw.get("title") or ""

    start_ts: Optional[float] = None
    end_ts:   Optional[float] = None
    time_source: Optional[str] = None

    raw_start = raw.get("game_start_time") or raw.get("gameStartTime")
    if raw_start is not None:
        start_ts = _parse_timestamp(raw_start)
        if start_ts is not None:
            time_source = "game_start_time"

    if start_ts is None:
        raw_start = raw.get("start_date_iso") or raw.get("startDateIso")
        if raw_start is not None:
            start_ts = _parse_timestamp(raw_start)
            if start_ts is not None:
                time_source = "start_date_iso"

    raw_end = raw.get("end_date_iso") or raw.get("endDateIso") or raw.get("end_date")
    if raw_end is not None:
        end_ts = _parse_timestamp(raw_end)

    if start_ts is None and slug:
        m = _BTC_5M_SLUG_RE.search(slug)
        if m:
            slug_ts = float(m.group(1))
            start_ts = slug_ts
            if end_ts is None:
                end_ts = slug_ts + WINDOW_SECS
            time_source = "slug_fallback"

    out["window_start_ts"] = start_ts
    out["window_end_ts"]   = end_ts
    out["time_source"]     = time_source

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


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _compute_window_ts(offset_windows: int = 0) -> int:
    """Return unix timestamp for the Nth 5m window relative to now (0=current)."""
    now = int(time.time())
    return (now // WINDOW_SECS) * WINDOW_SECS + offset_windows * WINDOW_SECS


def _parse_clob_token_ids(raw_value: Any) -> List[str]:
    """
    Parse clobTokenIds from Gamma API.
    May arrive as a real list or as a JSON-encoded string.
    Returns [] on any parse failure.
    """
    if raw_value is None:
        return []
    if isinstance(raw_value, list):
        return [str(t) for t in raw_value if t]
    if isinstance(raw_value, str):
        try:
            parsed = json.loads(raw_value)
            if isinstance(parsed, list):
                return [str(t) for t in parsed if t]
        except (json.JSONDecodeError, TypeError):
            logger.warning("clobTokenIds JSON parse failed: %r", raw_value[:80])
    return []


def _parse_timestamp(raw: Any) -> Optional[float]:
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


def _log_rejection(mid: str, reason: str, raw: Dict[str, Any],
                   raw_outcomes: Optional[List[str]] = None) -> None:
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
        raw_outcomes or [],
    )


# ─────────────────────────────────────────────────────────────────────────────
# Main class
# ─────────────────────────────────────────────────────────────────────────────

class MarketDiscovery:
    """
    Discovers BTC 5m markets — slug-first via Gamma API, CLOB scan as fallback.

    Job: Return list of MarketIdentity for active BTC 5m markets.
    Failure: API errors → empty list + log, never crash caller.
    """

    def __init__(
        self,
        clob_api_url: str = CLOB_API_URL,
        gamma_api_url: str = GAMMA_API_URL,
        title_keywords: Optional[List[str]] = None,
        min_window_secs: float = 240.0,
        max_window_secs: float = 360.0,
    ):
        self._clob_base  = clob_api_url.rstrip("/")
        self._gamma_base = gamma_api_url.rstrip("/")
        self._keywords   = [k.lower() for k in (title_keywords or ["btc", "bitcoin"])]
        self._min_window = min_window_secs
        self._max_window = max_window_secs

    async def discover(self) -> List[MarketIdentity]:
        """
        Slug-first discovery flow:
          1. Current window slug  (btc-updown-5m-{floor(now/300)*300})
          2. Previous window slug (btc-updown-5m-{floor(now/300)*300 - 300})
          3. Broad CLOB pagination scan (fallback)
        Returns deduplicated list of MarketIdentity.
        """
        if not AIOHTTP_AVAILABLE:
            logger.error("aiohttp not installed — cannot discover markets")
            return []

        found: Dict[str, MarketIdentity] = {}  # condition_id → identity

        async with aiohttp.ClientSession() as session:
            # ── Phase 1 & 2: slug-first Gamma lookups ──────────────────────
            for offset in (0, -1):
                window_ts = _compute_window_ts(offset)
                slug = f"btc-updown-5m-{window_ts}"
                if any(m.slug == slug for m in found.values()):
                    continue

                identity = await self._gamma_lookup_slug(session, slug)
                if identity and identity.condition_id not in found:
                    found[identity.condition_id] = identity
                    logger.info(
                        "SLUG_HIT slug=%s condition_id=%s up=%s down=%s",
                        slug, identity.condition_id,
                        identity.up_token_id[:8], identity.down_token_id[:8],
                    )

            # ── Phase 3: broad CLOB fallback if slug hits found nothing ────
            if not found:
                logger.info("No slug hits — falling back to broad CLOB scan")
                clob_markets = await self._broad_clob_scan(session)
                for identity in clob_markets:
                    if identity.condition_id not in found:
                        found[identity.condition_id] = identity

        results = list(found.values())
        logger.info(
            "Discovery complete: %d BTC 5m market(s) found", len(results)
        )
        return results

    # ── Gamma slug lookup ─────────────────────────────────────────────────────

    async def _gamma_lookup_slug(
        self, session: "aiohttp.ClientSession", slug: str
    ) -> Optional[MarketIdentity]:
        """
        Query Gamma API for one exact slug.
        Returns MarketIdentity on success, None if not found or invalid.
        """
        url = f"{self._gamma_base}/markets"
        try:
            async with session.get(
                url,
                params={"slug": slug},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 404:
                    logger.debug("Gamma slug not found: %s", slug)
                    return None
                if resp.status != 200:
                    logger.warning("Gamma API HTTP %d for slug=%s", resp.status, slug)
                    return None

                data = await resp.json()

                # Gamma returns a list or single object
                if isinstance(data, list):
                    markets = data
                elif isinstance(data, dict):
                    # might be wrapped in {"markets": [...]} or be the object itself
                    markets = data.get("markets") or data.get("data") or [data]
                else:
                    logger.warning("Gamma unexpected response type for slug=%s", slug)
                    return None

                for item in markets:
                    identity = self._parse_gamma_market(item)
                    if identity is not None:
                        return identity

                logger.debug("Gamma slug %s returned %d items, none valid", slug, len(markets))
                return None

        except asyncio.TimeoutError:
            logger.warning("Gamma timeout for slug=%s", slug)
        except Exception as exc:
            logger.warning("Gamma lookup error for slug=%s: %s", slug, exc)
        return None

    def _parse_gamma_market(self, data: Dict[str, Any]) -> Optional[MarketIdentity]:
        """
        Parse one Gamma API market object into MarketIdentity.

        Field mapping (Gamma → internal):
          conditionId / condition_id → condition_id
          slug                       → slug
          question                   → question
          clobTokenIds               → list (may be JSON string)
          outcomes                   → list (may be JSON string)
          gameStartTime / startDate  → window_start_ts
          endDate                    → window_end_ts
        """
        mid = (
            data.get("conditionId") or data.get("condition_id")
            or data.get("id") or data.get("slug") or "<unknown>"
        )

        # ── Identity ──────────────────────────────────────────────────────
        condition_id = data.get("conditionId") or data.get("condition_id")
        slug         = data.get("slug") or data.get("market_slug")
        question     = data.get("question") or data.get("title") or ""

        # Use slug as condition_id if conditionId absent
        internal_id = condition_id or (str(data.get("id")) if data.get("id") else None) or slug
        if not internal_id:
            logger.debug("Gamma market has no identity: keys=%s", sorted(data.keys()))
            return None

        # ── Keyword filter ────────────────────────────────────────────────
        if not any(kw in question.lower() for kw in self._keywords):
            # Also check slug
            if not slug or not any(kw in slug.lower() for kw in self._keywords):
                return None

        # ── clobTokenIds — may be list or JSON string ─────────────────────
        clob_token_ids = _parse_clob_token_ids(data.get("clobTokenIds"))
        if len(clob_token_ids) < 2:
            _log_rejection(mid, "clob_token_ids_insufficient", data)
            return None

        # ── Token role assignment ─────────────────────────────────────────
        raw_outcomes_raw = data.get("outcomes") or []
        if isinstance(raw_outcomes_raw, str):
            try:
                raw_outcomes_raw = json.loads(raw_outcomes_raw)
            except Exception:
                raw_outcomes_raw = []

        up_token_id:   Optional[str] = None
        down_token_id: Optional[str] = None

        for i, token_id in enumerate(clob_token_ids):
            outcome_str = (raw_outcomes_raw[i] if i < len(raw_outcomes_raw) else "").lower()
            if outcome_str in ("up", "yes"):
                up_token_id = token_id
            elif outcome_str in ("down", "no"):
                down_token_id = token_id

        # Positional fallback when outcomes[] absent or unrecognised
        if up_token_id is None and down_token_id is None:
            up_token_id   = clob_token_ids[0]
            down_token_id = clob_token_ids[1]
            logger.debug(
                "[%s] Token roles assigned positionally (no outcomes field)", mid
            )
        elif up_token_id is None or down_token_id is None:
            _log_rejection(mid, "partial_token_role_assignment", data,
                           [str(o) for o in raw_outcomes_raw])
            return None

        # ── Window times ──────────────────────────────────────────────────
        start_ts:   Optional[float] = None
        end_ts:     Optional[float] = None
        time_source: Optional[str]  = None

        # gameStartTime first
        raw_start = data.get("gameStartTime") or data.get("game_start_time")
        if raw_start:
            start_ts = _parse_timestamp(raw_start)
            if start_ts:
                time_source = "gameStartTime"

        # startDate fallback
        if start_ts is None:
            raw_start = data.get("startDate") or data.get("start_date") or data.get("startDateIso")
            if raw_start:
                start_ts = _parse_timestamp(raw_start)
                if start_ts:
                    time_source = "startDate"

        # endDate
        raw_end = data.get("endDate") or data.get("end_date") or data.get("endDateIso")
        if raw_end:
            end_ts = _parse_timestamp(raw_end)

        # Slug timestamp fallback
        if start_ts is None and slug:
            m = _BTC_5M_SLUG_RE.search(slug)
            if m:
                slug_ts   = float(m.group(1))
                start_ts  = slug_ts
                if end_ts is None:
                    end_ts = slug_ts + WINDOW_SECS
                time_source = "slug_fallback"
                logger.info(
                    "TIME_SOURCE=slug_fallback slug=%s start=%.0f end=%.0f",
                    slug, start_ts, end_ts,
                )

        if start_ts is None:
            _log_rejection(mid, "no_window_start", data, [str(o) for o in raw_outcomes_raw])
            return None
        if end_ts is None:
            _log_rejection(mid, "no_window_end", data, [str(o) for o in raw_outcomes_raw])
            return None

        # ── Duration filter ───────────────────────────────────────────────
        duration = end_ts - start_ts
        if not (self._min_window <= duration <= self._max_window):
            logger.debug(
                "[%s] Skipping: duration=%.0fs outside [%.0f, %.0f]",
                mid, duration, self._min_window, self._max_window,
            )
            return None

        return MarketIdentity(
            condition_id=condition_id or internal_id,
            question=question,
            up_token_id=up_token_id,
            down_token_id=down_token_id,
            window_start_ts=start_ts,
            window_end_ts=end_ts,
            slug=slug,
            raw_end_date=str(raw_end or ""),
        )

    # ── Broad CLOB fallback scan ──────────────────────────────────────────────

    async def _broad_clob_scan(
        self, session: "aiohttp.ClientSession"
    ) -> List[MarketIdentity]:
        """
        Paginate CLOB /markets and parse matching BTC 5m markets.
        Used only when Gamma slug lookups return nothing.
        """
        all_raw = await self._fetch_all_active_markets(session)
        logger.info("CLOB fallback scan: %d raw markets", len(all_raw))
        results = []
        for raw in all_raw:
            identity = self._parse_clob_market(raw)
            if identity is not None:
                results.append(identity)
        return results

    async def _fetch_all_active_markets(
        self, session: "aiohttp.ClientSession"
    ) -> List[Dict[str, Any]]:
        all_markets: List[Dict[str, Any]] = []
        next_cursor: Optional[str] = None
        pages_fetched = 0

        while True:
            params: Dict[str, Any] = {"active": "true", "closed": "false", "limit": PAGE_LIMIT}
            if next_cursor:
                params["next_cursor"] = next_cursor
            try:
                async with session.get(
                    f"{self._clob_base}/markets",
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status != 200:
                        logger.error("CLOB markets API returned HTTP %d", resp.status)
                        break
                    data = await resp.json()
                    all_markets.extend(data.get("data", []))
                    pages_fetched += 1
                    next_cursor = data.get("next_cursor")
                    if not next_cursor or next_cursor in ("", "LTE="):
                        break
                    if pages_fetched >= 20:
                        logger.warning("Hit 20-page limit during CLOB scan")
                        break
            except asyncio.TimeoutError:
                logger.error("Timeout fetching CLOB markets page %d", pages_fetched + 1)
                break
            except Exception as exc:
                logger.error("CLOB scan error: %s", exc)
                break

        return all_markets

    def _parse_clob_market(self, raw: Dict[str, Any]) -> Optional[MarketIdentity]:
        """Parse a CLOB market dict — used only in broad scan fallback."""
        norm = normalize_market_fields(raw)
        mid  = norm["internal_market_id"] or "<no-id>"

        if norm["internal_market_id"] is None:
            return None

        question = norm["question"] or ""
        if not any(kw in question.lower() for kw in self._keywords):
            return None

        if norm["window_start_ts"] is None:
            _log_rejection(mid, "no_window_start", raw, norm["raw_outcomes"])
            return None
        if norm["window_end_ts"] is None:
            _log_rejection(mid, "no_window_end", raw, norm["raw_outcomes"])
            return None

        duration = norm["window_end_ts"] - norm["window_start_ts"]
        if not (self._min_window <= duration <= self._max_window):
            return None

        tokens = raw.get("tokens", [])
        if len(tokens) < 2:
            _log_rejection(mid, "fewer_than_2_tokens", raw, norm["raw_outcomes"])
            return None

        if norm["up_token_id"] is None or norm["down_token_id"] is None:
            _log_rejection(mid, "token_roles_unresolved", raw, norm["raw_outcomes"])
            return None

        condition_id = norm["condition_id"] or norm["internal_market_id"]

        logger.info(
            "CLOB_ACCEPTED [%s] slug=%s time_source=%s duration=%.0fs",
            condition_id, norm["slug"], norm["time_source"], duration,
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
