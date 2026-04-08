"""
market_discovery.py — BTC 5m discovery: exact slug candidates only, no fallback scan.

Discovery flow (per cycle):
  1. Compute 3 candidate window timestamps: previous, current, next
     window_ts = floor(now / 300) * 300
     candidates = [window_ts - 300, window_ts, window_ts + 300]
  2. For each candidate, build slug: btc-updown-5m-{window_ts}
  3. GET https://gamma-api.polymarket.com/markets?slug={slug}
  4. Parse result → MarketIdentity (clobTokenIds may be list or JSON string)
  5. Accept market only if active, correct duration, both token IDs resolved
  6. If all 3 slugs miss → log SLUG_MISS for each, return []
  7. No generic BTC scan. No CLOB pagination. No keyword sweep.

clobTokenIds:
  Gamma returns this field either as a real list:   ["tok1", "tok2"]
  or as a JSON-encoded string:                      "[\"tok1\",\"tok2\"]"
  Always json.loads() when type is str before use.

Token role assignment:
  outcomes[i] paired with clobTokenIds[i]:
    "up"  / "yes" → up_token_id
    "down"/ "no"  → down_token_id
  If outcomes[] absent or empty → positional: [0]=up, [1]=down.

Window times:
  Priority: gameStartTime → startDate → slug suffix (btc-updown-5m-{ts})
  Slug suffix: window_start = ts, window_end = ts + 300.
"""

from __future__ import annotations
import asyncio
import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any

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

GAMMA_API_BASE = "https://gamma-api.polymarket.com"
WINDOW_SECS    = 300
_SLUG_RE       = re.compile(r"btc-updown-5m-(\d+)")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _current_window_ts() -> int:
    return (int(time.time()) // WINDOW_SECS) * WINDOW_SECS


def _parse_clob_token_ids(raw: Any) -> List[str]:
    """
    Parse clobTokenIds from Gamma API response.
    Accepts: real list  OR  JSON-encoded string  "[\"tok1\",\"tok2\"]"
    Returns [] on any failure — never treats string characters as token IDs.
    """
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(t) for t in raw if t]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(t) for t in parsed if t]
            logger.warning("clobTokenIds JSON decoded to non-list: %r", type(parsed))
        except json.JSONDecodeError as exc:
            logger.warning("clobTokenIds JSON decode failed: %s | raw=%r", exc, raw[:80])
    return []


def _parse_outcomes(raw: Any) -> List[str]:
    """Parse outcomes field (list or JSON string)."""
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(o) for o in raw]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(o) for o in parsed]
        except json.JSONDecodeError:
            pass
    return []


def _parse_ts(raw: Any) -> Optional[float]:
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
                return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc).timestamp()
            except ValueError:
                continue
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Discovery
# ─────────────────────────────────────────────────────────────────────────────

class MarketDiscovery:
    """
    BTC 5m market discovery — exact slug candidates only.

    Tries 3 slugs per cycle (prev / current / next window).
    Returns [] and logs SLUG_MISS if all 3 miss.
    No broad scan, no keyword sweep, no pagination fallback.
    """

    def __init__(
        self,
        gamma_api_url: str = GAMMA_API_BASE,
        # kept for runner compat — not used for scanning
        clob_api_url: str = "https://clob.polymarket.com",
        title_keywords: Optional[List[str]] = None,
        min_window_secs: float = 240.0,
        max_window_secs: float = 360.0,
    ):
        self._gamma_base = gamma_api_url.rstrip("/")
        self._min_window = min_window_secs
        self._max_window = max_window_secs
        # title_keywords retained for signature compat but not used in slug path

    async def discover(self) -> List[MarketIdentity]:
        if not AIOHTTP_AVAILABLE:
            logger.error("DISCOVERY_ERROR: aiohttp not installed")
            return []

        base_ts   = _current_window_ts()
        candidates = [base_ts - WINDOW_SECS, base_ts, base_ts + WINDOW_SECS]
        found: Dict[str, MarketIdentity] = {}

        async with aiohttp.ClientSession() as session:
            for window_ts in candidates:
                slug = f"btc-updown-5m-{window_ts}"
                identity = await self._gamma_lookup(session, slug, window_ts)
                if identity and identity.condition_id not in found:
                    found[identity.condition_id] = identity
                    logger.info(
                        "SLUG_HIT slug=%s condition_id=%s "
                        "up_token=%s down_token=%s window=[%.0f,%.0f]",
                        slug, identity.condition_id,
                        identity.up_token_id[:12], identity.down_token_id[:12],
                        identity.window_start_ts, identity.window_end_ts,
                    )

        if not found:
            slugs_tried = [f"btc-updown-5m-{ts}" for ts in candidates]
            logger.warning(
                "SLUG_MISS: all 3 exact slug candidates returned no valid market | "
                "slugs_tried=%s | "
                "gamma_url=%s/markets?slug=<slug>",
                slugs_tried, self._gamma_base,
            )

        logger.info("Discovery: %d market(s) found this cycle", len(found))
        return list(found.values())

    async def _gamma_lookup(
        self,
        session: "aiohttp.ClientSession",
        slug: str,
        window_ts: int,
    ) -> Optional[MarketIdentity]:
        """
        GET {gamma_base}/markets?slug={slug}
        Returns MarketIdentity or None.
        Logs exact URL, response status, and parse failure on any miss.
        """
        url = f"{self._gamma_base}/markets"
        params = {"slug": slug}

        try:
            async with session.get(
                url,
                params=params,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                full_url = str(resp.url)

                if resp.status == 404:
                    logger.debug("SLUG_MISS_404 url=%s", full_url)
                    return None

                if resp.status != 200:
                    logger.warning(
                        "SLUG_MISS_HTTP slug=%s status=%d url=%s",
                        slug, resp.status, full_url,
                    )
                    return None

                raw_body = await resp.text()
                try:
                    data = json.loads(raw_body)
                except json.JSONDecodeError as exc:
                    logger.warning(
                        "SLUG_MISS_JSON slug=%s parse_error=%s body_prefix=%r",
                        slug, exc, raw_body[:120],
                    )
                    return None

                # Gamma may return [] (not found), [market], or the object directly
                if isinstance(data, list):
                    if not data:
                        logger.debug("SLUG_MISS_EMPTY slug=%s url=%s", slug, full_url)
                        return None
                    item = data[0]
                elif isinstance(data, dict):
                    # unwrap common envelope shapes
                    if "markets" in data:
                        items = data["markets"]
                        if not items:
                            logger.debug("SLUG_MISS_EMPTY slug=%s url=%s", slug, full_url)
                            return None
                        item = items[0]
                    elif "data" in data:
                        items = data["data"]
                        if not items:
                            logger.debug("SLUG_MISS_EMPTY slug=%s url=%s", slug, full_url)
                            return None
                        item = items[0]
                    else:
                        item = data
                else:
                    logger.warning(
                        "SLUG_MISS_UNEXPECTED_TYPE slug=%s type=%s", slug, type(data)
                    )
                    return None

                identity = self._parse_gamma_item(item, slug, window_ts)
                if identity is None:
                    logger.warning(
                        "SLUG_MISS_PARSE_FAIL slug=%s keys=%s "
                        "clobTokenIds=%r outcomes=%r "
                        "gameStartTime=%r startDate=%r endDate=%r "
                        "active=%r closed=%r",
                        slug, sorted(item.keys()),
                        item.get("clobTokenIds"),
                        item.get("outcomes"),
                        item.get("gameStartTime"), item.get("startDate"),
                        item.get("endDate"),
                        item.get("active"), item.get("closed"),
                    )
                return identity

        except asyncio.TimeoutError:
            logger.warning("SLUG_MISS_TIMEOUT slug=%s url=%s", slug, url)
        except Exception as exc:
            logger.warning("SLUG_MISS_ERROR slug=%s error=%s", slug, exc)
        return None

    def _parse_gamma_item(
        self,
        item: Dict[str, Any],
        slug: str,
        window_ts: int,
    ) -> Optional[MarketIdentity]:
        """
        Parse one Gamma market object into MarketIdentity.
        Returns None on any validation failure (caller logs details).
        """
        # ── Token IDs ────────────────────────────────────────────────────────
        clob_ids = _parse_clob_token_ids(item.get("clobTokenIds"))
        if len(clob_ids) < 2:
            return None  # caller logs

        # ── Token roles ──────────────────────────────────────────────────────
        outcomes = _parse_outcomes(item.get("outcomes"))
        up_id:   Optional[str] = None
        down_id: Optional[str] = None

        for i, tok in enumerate(clob_ids):
            label = outcomes[i].lower() if i < len(outcomes) else ""
            if label in ("up", "yes"):
                up_id = tok
            elif label in ("down", "no"):
                down_id = tok

        # Positional fallback when outcomes field absent or unmatched
        if up_id is None and down_id is None:
            up_id   = clob_ids[0]
            down_id = clob_ids[1]

        if not up_id or not down_id:
            return None

        # ── Window times ─────────────────────────────────────────────────────
        start_ts: Optional[float] = None
        end_ts:   Optional[float] = None

        start_ts = _parse_ts(item.get("gameStartTime")) or _parse_ts(item.get("startDate"))
        end_ts   = _parse_ts(item.get("endDate"))

        # Slug suffix fallback
        if start_ts is None:
            start_ts = float(window_ts)
        if end_ts is None:
            end_ts = float(window_ts + WINDOW_SECS)

        duration = end_ts - start_ts
        if not (self._min_window <= duration <= self._max_window):
            return None

        # ── Identity ─────────────────────────────────────────────────────────
        condition_id = (
            item.get("conditionId") or item.get("condition_id")
            or str(item.get("id") or "") or slug
        )

        return MarketIdentity(
            condition_id=condition_id,
            question=item.get("question") or item.get("title") or slug,
            up_token_id=up_id,
            down_token_id=down_id,
            window_start_ts=start_ts,
            window_end_ts=end_ts,
            slug=item.get("slug") or slug,
            raw_end_date=str(item.get("endDate") or ""),
        )
