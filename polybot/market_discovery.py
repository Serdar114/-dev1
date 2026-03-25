"""
Market Discovery — Polymarket BTC Up/Down marketlerini bul.

Slug formatları:
  5m  → btc-updown-5m-{unix_ts}   (300'e bölünebilen)
  15m → btc-updown-15m-{unix_ts}  (900'e bölünebilen)

3 kademeli fallback:
  1. Slug ile direkt Gamma API sorgusu
  2. Tag/keyword filtresi ile Gamma API sorgusu
  3. CLOB API /markets endpoint'i

BUG FIX — clobTokenIds:
  Gamma API, clobTokenIds'i JSON string olarak döndürür:
    '["token1","token2"]'
  Yanlış:  data["clobTokenIds"][0]  → '[' karakterini verir → URL encode'da %5B → 404
  Doğru:   json.loads(data["clobTokenIds"])[0]
"""

import json
import time
import asyncio
import aiohttp
import logger as log_module


GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"

MARKET_INTERVALS = {
    "5m": 300,
    "15m": 900,
}


def _current_window_ts(interval: str = "5m") -> int:
    """Şu anki pencere başlangıç Unix timestamp'ini döndür."""
    divisor = MARKET_INTERVALS.get(interval, 300)
    now = int(time.time())
    return (now // divisor) * divisor


def _next_window_ts(interval: str = "5m") -> int:
    divisor = MARKET_INTERVALS.get(interval, 300)
    return _current_window_ts(interval) + divisor


def _secs_to_resolution(interval: str = "5m") -> int:
    """Mevcut pencere kapanmasına kaç saniye kaldı."""
    return _next_window_ts(interval) - int(time.time())


def _slug(interval: str = "5m", ts: int | None = None) -> str:
    if ts is None:
        ts = _current_window_ts(interval)
    prefix = "btc-updown-5m" if interval == "5m" else "btc-updown-15m"
    return f"{prefix}-{ts}"


def _parse_token_ids(raw: str | list) -> list[str]:
    """
    BUG FIX: clobTokenIds JSON string → liste.
    Gamma API bazen string, bazen liste döndürür — ikisini de handle et.
    """
    if isinstance(raw, list):
        return raw
    # raw = '["token1","token2"]' gibi bir string
    parsed = json.loads(raw)
    return parsed


async def _fetch_gamma(session: aiohttp.ClientSession, url: str, params: dict) -> list[dict]:
    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=8)) as r:
            if r.status == 200:
                return await r.json()
            await log_module.log("gamma_http_error", {"url": url, "status": r.status})
    except Exception as e:
        await log_module.log("gamma_fetch_error", {"url": url, "error": str(e)})
    return []


async def discover_market(
    interval: str = "5m",
    gamma_base: str = GAMMA_BASE,
    clob_base: str = CLOB_BASE,
) -> dict | None:
    """
    Aktif BTC Up/Down marketini keşfet.
    Döndürür: {"slug", "condition_id", "token_up", "token_down", "secs_to_resolution"}
    """
    current_ts = _current_window_ts(interval)
    slug = _slug(interval, current_ts)

    async with aiohttp.ClientSession() as session:
        # --- Fallback 1: Direkt slug lookup ---
        markets = await _fetch_gamma(session, f"{gamma_base}/markets", {"slug": slug})
        if not markets:
            # Bir önceki pencereyi de dene (yeni pencere henüz oluşmamış olabilir)
            prev_ts = current_ts - MARKET_INTERVALS[interval]
            prev_slug = _slug(interval, prev_ts)
            markets = await _fetch_gamma(session, f"{gamma_base}/markets", {"slug": prev_slug})
            if markets:
                slug = prev_slug

        # --- Fallback 2: Tag/keyword filtresi ---
        if not markets:
            tag_query = "btc" if interval == "5m" else "btc-15m"
            markets = await _fetch_gamma(
                session,
                f"{gamma_base}/markets",
                {"tag": tag_query, "active": "true", "closed": "false", "_limit": 20},
            )
            # Slug ile filtrele
            prefix = "btc-updown-5m" if interval == "5m" else "btc-updown-15m"
            markets = [m for m in markets if m.get("slug", "").startswith(prefix)]

        # --- Fallback 3: CLOB /markets ---
        if not markets:
            try:
                async with session.get(
                    f"{clob_base}/markets",
                    params={"next_cursor": ""},
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as r:
                    if r.status == 200:
                        body = await r.json()
                        prefix = "btc-updown-5m" if interval == "5m" else "btc-updown-15m"
                        clob_markets = body.get("data", [])
                        markets = [m for m in clob_markets if m.get("market_slug", "").startswith(prefix)]
            except Exception as e:
                await log_module.log("clob_fallback_error", {"error": str(e)})

        if not markets:
            await log_module.log("market_not_found", {"slug": slug, "interval": interval})
            return None

        market = markets[0]

        # --- clobTokenIds BUG FIX ---
        raw_token_ids = market.get("clobTokenIds") or market.get("clob_token_ids", "[]")
        try:
            token_ids = _parse_token_ids(raw_token_ids)
        except (json.JSONDecodeError, TypeError) as e:
            await log_module.log("token_parse_error", {"raw": raw_token_ids, "error": str(e)})
            return None

        if len(token_ids) < 2:
            await log_module.log("token_count_error", {"token_ids": token_ids})
            return None

        secs_left = _secs_to_resolution(interval)

        # Pencere neredeyse kapanmış — işlemek için çok geç
        if secs_left < 60:
            await log_module.log("market_too_late", {
                "slug": market.get("slug", slug),
                "secs_to_resolution": secs_left,
                "reason": "less_than_60s_remaining",
            })
            return None

        result = {
            "slug": market.get("slug", slug),
            "condition_id": market.get("conditionId") or market.get("condition_id", ""),
            "token_up": token_ids[0],    # index 0 = UP token
            "token_down": token_ids[1],  # index 1 = DOWN token
            "secs_to_resolution": secs_left,
            "window_ts": current_ts,
            "interval": interval,
        }
        await log_module.log("market_discovered", result)
        return result


async def get_orderbook_midpoint(token_id: str, clob_base: str = CLOB_BASE) -> float | None:
    """CLOB'dan token midpoint fiyatını çek."""
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(
                f"{clob_base}/midpoint",
                params={"token_id": token_id},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as r:
                if r.status == 200:
                    data = await r.json()
                    mid = float(data.get("mid", 0))
                    return mid if 0 < mid < 1 else None
        except Exception as e:
            await log_module.log("midpoint_error", {"token_id": token_id, "error": str(e)})
    return None


async def get_market_fee_rate(token_id: str, clob_base: str = CLOB_BASE) -> float:
    """Market-specific fee rate'i CLOB'dan çek. Bulunamazsa 0.072 döndür."""
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(
                f"{clob_base}/fee-rate",
                params={"token_id": token_id},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as r:
                if r.status == 200:
                    data = await r.json()
                    return float(data.get("fee_rate", 0.072))
        except Exception:
            pass
    return 0.072
