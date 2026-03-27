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
    now_ts = int(time.time())
    secs_into_window = now_ts - current_ts
    secs_left_pre = _secs_to_resolution(interval)

    # === DEBUG: discovery context ===
    print(f"[DISC_DEBUG] === discover_market START ===", flush=True)
    print(f"[DISC_DEBUG] now={now_ts} current_window_ts={current_ts} "
          f"secs_into_window={secs_into_window} secs_left={secs_left_pre} "
          f"interval={interval} slug={slug}", flush=True)
    await log_module.log("disc_debug_start", {
        "now_ts": now_ts, "current_window_ts": current_ts,
        "secs_into_window": secs_into_window, "secs_left": secs_left_pre,
        "interval": interval, "computed_slug": slug,
    })

    async with aiohttp.ClientSession() as session:
        # --- Fallback 1: Direkt slug lookup ---
        markets = await _fetch_gamma(session, f"{gamma_base}/markets", {"slug": slug})
        print(f"[DISC_DEBUG] FB1 slug={slug} → type={type(markets).__name__} "
              f"len={len(markets) if isinstance(markets, list) else 'N/A'} "
              f"truthy={bool(markets)}", flush=True)
        if markets and isinstance(markets, list) and len(markets) > 0:
            print(f"[DISC_DEBUG] FB1 hit: slug_returned={markets[0].get('slug','')} "
                  f"keys={list(markets[0].keys())[:15]}", flush=True)
        if not markets:
            # Bir önceki pencereyi de dene (yeni pencere henüz oluşmamış olabilir)
            prev_ts = current_ts - MARKET_INTERVALS[interval]
            prev_slug = _slug(interval, prev_ts)
            print(f"[DISC_DEBUG] FB1 miss → trying prev_slug={prev_slug}", flush=True)
            markets = await _fetch_gamma(session, f"{gamma_base}/markets", {"slug": prev_slug})
            print(f"[DISC_DEBUG] FB1-prev → type={type(markets).__name__} "
                  f"len={len(markets) if isinstance(markets, list) else 'N/A'} "
                  f"truthy={bool(markets)}", flush=True)
            if markets:
                slug = prev_slug

        # --- Fallback 2: Tag/keyword filtresi ---
        if not markets:
            tag_query = "btc" if interval == "5m" else "btc-15m"
            print(f"[DISC_DEBUG] FB2 tag_query={tag_query}", flush=True)
            raw_tag_markets = await _fetch_gamma(
                session,
                f"{gamma_base}/markets",
                {"tag": tag_query, "active": "true", "closed": "false", "_limit": 20},
            )
            print(f"[DISC_DEBUG] FB2 raw_count={len(raw_tag_markets) if isinstance(raw_tag_markets, list) else 'NOT_LIST'}", flush=True)
            # Log all slugs returned by tag search for diagnosis
            if isinstance(raw_tag_markets, list):
                tag_slugs = [m.get("slug", "NO_SLUG") for m in raw_tag_markets[:10]]
                print(f"[DISC_DEBUG] FB2 candidate_slugs={tag_slugs}", flush=True)
                await log_module.log("disc_debug_fb2_candidates", {
                    "tag_query": tag_query,
                    "raw_count": len(raw_tag_markets),
                    "candidate_slugs": tag_slugs,
                })
            # Slug ile filtrele
            prefix = "btc-updown-5m" if interval == "5m" else "btc-updown-15m"
            markets = [m for m in raw_tag_markets if m.get("slug", "").startswith(prefix)] if isinstance(raw_tag_markets, list) else []
            print(f"[DISC_DEBUG] FB2 after prefix filter (prefix={prefix}): "
                  f"matched={len(markets)}", flush=True)

        # --- Fallback 3: CLOB /markets ---
        if not markets:
            print(f"[DISC_DEBUG] FB3 CLOB fallback starting...", flush=True)
            try:
                async with session.get(
                    f"{clob_base}/markets",
                    params={"next_cursor": ""},
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as r:
                    print(f"[DISC_DEBUG] FB3 CLOB status={r.status}", flush=True)
                    if r.status == 200:
                        body = await r.json()
                        top_keys = list(body.keys()) if isinstance(body, dict) else f"NOT_DICT:{type(body).__name__}"
                        prefix = "btc-updown-5m" if interval == "5m" else "btc-updown-15m"
                        clob_markets = body.get("data", []) if isinstance(body, dict) else []
                        print(f"[DISC_DEBUG] FB3 body_keys={top_keys} "
                              f"data_count={len(clob_markets)}", flush=True)
                        # Log sample slugs from CLOB for diagnosis
                        clob_slugs = [m.get("market_slug", m.get("slug", "NO_SLUG")) for m in clob_markets[:10]]
                        print(f"[DISC_DEBUG] FB3 sample_slugs={clob_slugs}", flush=True)
                        await log_module.log("disc_debug_fb3_candidates", {
                            "status": r.status,
                            "body_keys": top_keys,
                            "data_count": len(clob_markets),
                            "sample_slugs": clob_slugs,
                        })
                        markets = [m for m in clob_markets if m.get("market_slug", "").startswith(prefix)]
                        print(f"[DISC_DEBUG] FB3 after prefix filter: matched={len(markets)}", flush=True)
                    else:
                        body_preview = await r.text()
                        print(f"[DISC_DEBUG] FB3 CLOB non-200: status={r.status} "
                              f"body={body_preview[:200]}", flush=True)
            except Exception as e:
                print(f"[DISC_DEBUG] FB3 CLOB exception: {e}", flush=True)
                await log_module.log("clob_fallback_error", {"error": str(e)})

        if not markets:
            print(f"[DISC_DEBUG] ALL FALLBACKS FAILED — no market found", flush=True)
            await log_module.log("market_not_found", {
                "slug": slug, "interval": interval,
                "now_ts": now_ts, "current_window_ts": current_ts,
                "secs_into_window": secs_into_window,
            })
            return None

        market = markets[0]
        print(f"[DISC_DEBUG] market found: slug={market.get('slug','')} "
              f"keys={list(market.keys())[:15]}", flush=True)

        # --- clobTokenIds BUG FIX ---
        raw_token_ids = market.get("clobTokenIds") or market.get("clob_token_ids", "[]")
        print(f"[DISC_DEBUG] raw_token_ids type={type(raw_token_ids).__name__} "
              f"value={str(raw_token_ids)[:200]}", flush=True)
        try:
            token_ids = _parse_token_ids(raw_token_ids)
        except (json.JSONDecodeError, TypeError) as e:
            print(f"[DISC_DEBUG] token_parse FAILED: {e}", flush=True)
            await log_module.log("token_parse_error", {"raw": str(raw_token_ids)[:200], "error": str(e)})
            return None

        print(f"[DISC_DEBUG] token_ids count={len(token_ids)} "
              f"ids={[t[:16]+'...' for t in token_ids[:4]]}", flush=True)
        if len(token_ids) < 2:
            print(f"[DISC_DEBUG] token_count < 2 — FAIL", flush=True)
            await log_module.log("token_count_error", {"token_ids": token_ids})
            return None

        secs_left = _secs_to_resolution(interval)
        print(f"[DISC_DEBUG] secs_to_resolution={secs_left}", flush=True)

        # Pencere neredeyse kapanmış — işlemek için çok geç
        if secs_left < 60:
            print(f"[DISC_DEBUG] TOO LATE: secs_left={secs_left} < 60", flush=True)
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
        print(f"[DISC_DEBUG] === SUCCESS === slug={result['slug']} "
              f"secs_left={secs_left} up={token_ids[0][:16]}... down={token_ids[1][:16]}...", flush=True)
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


async def get_market_fee_rate(token_id: str, clob_base: str = CLOB_BASE) -> dict:
    """
    Market-specific fee rate'i CLOB'dan çek.
    Structured sonuç döndürür — caller gerçek cevap ile fallback'i ayırabilir.

    Döndürür:
        {
            "fee_rate": float,           # fiilen kullanılacak değer
            "verified_remote": bool,     # True = CLOB 200 döndü ve fee_rate alanı vardı
            "status": str,               # "verified" | "endpoint_no_fee_field" | "http_error" | "fetch_failed"
            "note": str,                 # insan-okunur açıklama
        }
    """
    fallback = {"fee_rate": 0.072, "verified_remote": False, "status": "fetch_failed", "note": ""}
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(
                f"{clob_base}/fee-rate",
                params={"token_id": token_id},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as r:
                if r.status == 200:
                    data = await r.json()
                    if "fee_rate" in data:
                        return {
                            "fee_rate": float(data["fee_rate"]),
                            "verified_remote": True,
                            "status": "verified",
                            "note": f"CLOB returned fee_rate={data['fee_rate']} for token {token_id[:16]}",
                        }
                    else:
                        return {
                            "fee_rate": 0.072,
                            "verified_remote": False,
                            "status": "endpoint_no_fee_field",
                            "note": f"CLOB 200 but response has no fee_rate field. keys={list(data.keys())}",
                        }
                else:
                    fallback["status"] = "http_error"
                    fallback["note"] = f"CLOB returned HTTP {r.status}"
                    await log_module.log("fee_rate_http_error", {
                        "token_id": token_id, "status": r.status,
                    })
        except Exception as e:
            fallback["status"] = "fetch_failed"
            fallback["note"] = f"CLOB unreachable: {e}"
    return fallback
