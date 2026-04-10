"""
market_resolver.py — Post-close settlement via Polymarket Gamma winner field.

Design:
  After a BTC 5m market window closes, Polymarket resolves it within ~1-3 min.
  This module polls the Gamma API until the winning token is published, then
  maps it to ResolutionOutcome.UP or DOWN using the known token roles.

  Replaces: local Chainlink open/close reconstruction (dropped because RTDS
  Chainlink cadence is ~minutes between oracle rounds, not per-second).

  Chainlink stream is kept as optional diagnostic / reference only.
  Settlement is canonical Polymarket resolution, not locally reconstructed.

  API: GET {gamma_base}/markets?slug={slug}
  Winner detection field priority:
    1. item["winner"]               — token_id or outcome label string
    2. item["tokens"][i]["winner"]  — per-token winner bool in tokens array

  Polling:
    - starts immediately after window close fires (may be slightly early)
    - polls every poll_interval_secs until resolved or max_poll_secs elapsed
    - returns UNRESOLVED on timeout (no winner found within deadline)
"""

from __future__ import annotations
import asyncio
import json
import logging
import time
from typing import Optional

try:
    import aiohttp
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False

from loggingx.schemas import ResolutionOutcome

logger = logging.getLogger("polybot.market_resolver")

GAMMA_BASE = "https://gamma-api.polymarket.com"


class MarketResolver:
    """
    Polls Gamma API after market close until resolved winner is available.

    Job: map a closed BTC 5m market → ResolutionOutcome.UP / DOWN / UNRESOLVED.
    Input: condition_id, up_token_id, down_token_id, slug (optional but faster)
    Output: ResolutionOutcome string

    Failure:
      - Gamma returns no winner within max_poll_secs → UNRESOLVED (logged)
      - aiohttp not installed → UNRESOLVED (logged)
      - Network error on individual poll → skipped, retried on next interval
    """

    def __init__(
        self,
        gamma_api_url: str = GAMMA_BASE,
        poll_interval_secs: float = 10.0,
        max_poll_secs: float = 300.0,     # 5 min hard cap
    ):
        self._gamma_base    = gamma_api_url.rstrip("/")
        self._poll_interval = poll_interval_secs
        self._max_poll_secs = max_poll_secs

    async def resolve(
        self,
        condition_id: str,
        up_token_id: str,
        down_token_id: str,
        slug: Optional[str] = None,
    ) -> str:
        """
        Poll until winner is found or timeout.
        Returns ResolutionOutcome.UP / DOWN / UNRESOLVED.
        """
        if not AIOHTTP_AVAILABLE:
            logger.error("[%s] aiohttp unavailable — cannot poll resolution", condition_id)
            return ResolutionOutcome.UNRESOLVED

        deadline = time.time() + self._max_poll_secs
        attempt  = 0

        async with aiohttp.ClientSession() as session:
            while time.time() < deadline:
                attempt += 1
                try:
                    outcome = await self._try_once(
                        session, condition_id, up_token_id, down_token_id, slug
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.debug("[%s] Poll attempt %d error: %s", condition_id, attempt, exc)
                    outcome = None

                if outcome is not None:
                    logger.info(
                        "[%s] POLYMARKET_RESOLVED outcome=%s attempts=%d",
                        condition_id, outcome, attempt,
                    )
                    return outcome

                await asyncio.sleep(self._poll_interval)

        logger.warning(
            "[%s] Resolution poll timed out after %.0fs (%d attempts) — UNRESOLVED",
            condition_id, self._max_poll_secs, attempt,
        )
        return ResolutionOutcome.UNRESOLVED

    async def _try_once(
        self,
        session,
        condition_id: str,
        up_token_id: str,
        down_token_id: str,
        slug: Optional[str],
    ) -> Optional[str]:
        item = await self._fetch_market(session, condition_id, slug)
        if item is None:
            return None

        if not (item.get("closed") or item.get("resolved")):
            logger.debug("[%s] Market not yet closed/resolved (attempt)", condition_id)
            return None

        return self._extract_outcome(item, condition_id, up_token_id, down_token_id)

    async def _fetch_market(
        self,
        session,
        condition_id: str,
        slug: Optional[str],
    ) -> Optional[dict]:
        url    = f"{self._gamma_base}/markets"
        params = {"slug": slug} if slug else {"conditionId": condition_id}

        async with session.get(
            url, params=params, timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()

        if isinstance(data, list):
            return data[0] if data else None
        if isinstance(data, dict):
            items = data.get("data") or data.get("markets")
            if isinstance(items, list):
                return items[0] if items else None
            return data
        return None

    def _extract_outcome(
        self,
        item: dict,
        condition_id: str,
        up_token_id: str,
        down_token_id: str,
    ) -> Optional[str]:
        """
        Extract UP/DOWN from a resolved Gamma item.

        Field priority:
          1. item["winner"]              — token_id string or label ("Up"/"Down")
          2. item["tokens"][i]["winner"] — per-token winner bool
        Returns None if the market is closed but winner not yet posted.
        """
        # ── Method 1: direct winner field ────────────────────────────────────
        winner_raw = item.get("winner")
        if winner_raw is not None and str(winner_raw).strip():
            result = self._map_winner(
                str(winner_raw), up_token_id, down_token_id, condition_id
            )
            if result is not None:
                return result

        # ── Method 2: tokens array with per-token winner bool ────────────────
        tokens_raw = item.get("tokens") or []
        if isinstance(tokens_raw, str):
            try:
                tokens_raw = json.loads(tokens_raw)
            except Exception:
                tokens_raw = []
        for tok in tokens_raw:
            if not isinstance(tok, dict):
                continue
            if tok.get("winner"):
                tok_id = str(tok.get("token_id") or tok.get("id") or "")
                if tok_id == up_token_id:
                    return ResolutionOutcome.UP
                if tok_id == down_token_id:
                    return ResolutionOutcome.DOWN

        # Market is closed but winner field absent — keep polling
        logger.debug("[%s] Closed but winner field not yet populated — retrying", condition_id)
        return None

    def _map_winner(
        self,
        winner_raw: str,
        up_token_id: str,
        down_token_id: str,
        condition_id: str,
    ) -> Optional[str]:
        # Token-id exact match
        if winner_raw == up_token_id:
            return ResolutionOutcome.UP
        if winner_raw == down_token_id:
            return ResolutionOutcome.DOWN
        # Outcome label match
        w = winner_raw.lower().strip()
        if w in ("up", "yes", "1", "true"):
            return ResolutionOutcome.UP
        if w in ("down", "no", "0", "false"):
            return ResolutionOutcome.DOWN
        logger.debug("[%s] Unrecognised winner value: %r", condition_id, winner_raw)
        return None
