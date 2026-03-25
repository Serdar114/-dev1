# polymarket.py — Polymarket REST helpers

import logging
import time

import aiohttp

from config import (
    MARKET_URL,
    ORDERBOOK_URL,
    HTTP_TIMEOUT,
    RESOLUTION_POLL_INTERVAL,
    RESOLUTION_TIMEOUT,
    SLUG_PREFIX,
    WINDOW_SECONDS,
)

logger = logging.getLogger(__name__)


def current_window_ts() -> int:
    return (int(time.time()) // WINDOW_SECONDS) * WINDOW_SECONDS


def slug_for(ts: int) -> str:
    return f"{SLUG_PREFIX}{ts}"


async def get_market(session: aiohttp.ClientSession, slug: str) -> dict | None:
    """
    Fetch market metadata by slug.
    Returns dict with keys: condition_id, yes_token_id, no_token_id, min_order_size,
    resolved, outcome (if resolved).
    Returns None on error.
    """
    try:
        async with session.get(
            MARKET_URL,
            params={"slug": slug},
            timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT),
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()

        if not data:
            logger.warning("get_market: empty response for slug=%s", slug)
            return None

        market = data[0] if isinstance(data, list) else data

        tokens = market.get("tokens", [])
        yes_token_id = None
        no_token_id  = None
        for t in tokens:
            outcome = t.get("outcome", "").upper()
            if outcome == "YES":
                yes_token_id = t.get("token_id")
            elif outcome == "NO":
                no_token_id = t.get("token_id")

        # min_order_size lives on the market object; fall back to None
        min_order_size = market.get("min_order_size") or market.get("minimum_order_size")

        resolved = bool(market.get("resolved", False) or market.get("closed", False))
        outcome_raw = market.get("outcome") or market.get("resolution_outcome")
        outcome = outcome_raw.upper() if outcome_raw else None

        # Chainlink price at resolution (logged if present)
        chainlink_price = market.get("chainlink_price") or market.get("resolution_price")

        return {
            "condition_id":    market.get("condition_id"),
            "yes_token_id":    yes_token_id,
            "no_token_id":     no_token_id,
            "min_order_size":  min_order_size,
            "resolved":        resolved,
            "outcome":         outcome,
            "chainlink_price": chainlink_price,
        }

    except aiohttp.ClientError as exc:
        logger.warning("get_market HTTP error (slug=%s): %s", slug, exc)
        return None
    except Exception as exc:
        logger.warning("get_market unexpected error (slug=%s): %s", slug, exc)
        return None


async def get_best_ask(session: aiohttp.ClientSession, token_id: str) -> float | None:
    """
    Fetch the orderbook for token_id and return the best ask (lowest ask price).
    Returns None on error or empty book.
    """
    try:
        async with session.get(
            ORDERBOOK_URL,
            params={"token_id": token_id},
            timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT),
        ) as resp:
            resp.raise_for_status()
            book = await resp.json()

        asks = book.get("asks", [])
        if not asks:
            return None

        # asks is a list of {"price": "0.87", "size": "100"} objects
        best = min(float(a["price"]) for a in asks)
        return best

    except aiohttp.ClientError as exc:
        logger.warning("get_best_ask HTTP error (token=%s): %s", token_id, exc)
        return None
    except Exception as exc:
        logger.warning("get_best_ask unexpected error (token=%s): %s", token_id, exc)
        return None


async def get_min_order_size(session: aiohttp.ClientSession, token_id: str) -> float | None:
    """
    Extract min_order_size from the orderbook endpoint.
    """
    try:
        async with session.get(
            ORDERBOOK_URL,
            params={"token_id": token_id},
            timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT),
        ) as resp:
            resp.raise_for_status()
            book = await resp.json()
        return book.get("min_order_size")
    except Exception as exc:
        logger.warning("get_min_order_size error (token=%s): %s", token_id, exc)
        return None


async def poll_resolution(
    session: aiohttp.ClientSession,
    slug: str,
    window_close_unix: int,
    btc_price_fn,          # callable → float | None  (current Binance price)
) -> dict:
    """
    Poll every RESOLUTION_POLL_INTERVAL seconds until resolved or timeout.
    Returns resolution dict:
      {outcome, chainlink_price, binance_price_at_resolution, price_delta,
       estimated_lag_seconds, resolved_at_unix}
    """
    deadline = window_close_unix + RESOLUTION_TIMEOUT

    while time.time() < deadline:
        market = await get_market(session, slug)
        if market and market["resolved"] and market["outcome"]:
            resolved_at = int(time.time())
            # Capture Binance price at the moment resolution is detected
            binance_at_resolution = btc_price_fn()
            chainlink = market.get("chainlink_price")
            price_delta = None
            if chainlink is not None and binance_at_resolution is not None:
                price_delta = round(float(chainlink) - binance_at_resolution, 4)
            return {
                "outcome":                    market["outcome"],
                "chainlink_price":            float(chainlink) if chainlink else None,
                "binance_price_at_resolution": binance_at_resolution,
                "price_delta":                price_delta,
                "estimated_lag_seconds":      resolved_at - window_close_unix,
                "resolved_at_unix":           resolved_at,
            }

        await _async_sleep(RESOLUTION_POLL_INTERVAL)

    logger.warning("Resolution timeout for slug=%s", slug)
    return {
        "outcome":                    "UNRESOLVED",
        "chainlink_price":            None,
        "binance_price_at_resolution": None,
        "price_delta":                None,
        "estimated_lag_seconds":      None,
        "resolved_at_unix":           None,
    }


async def _async_sleep(seconds: float):
    import asyncio
    await asyncio.sleep(seconds)
