"""
Resolution Truth — Resolve anında winner belirlemek için truth layer.

Katmanlar:
  1. Binance REST: btc_close çeker, btc_open ile karşılaştırır → winner_binance
  2. Chainlink:    Polygon RPC eth_call → historical getRoundData() on BTC/USD feed
  3. Karşılaştırma: iki kaynak uyuşuyor mu? → resolution_match

Polymarket BTC 5m/15m marketleri Chainlink ile resolve oluyor.
Binance close yalnızca proxy/sinyal — canonical truth değil.

Resolution logic:
  1. latestRoundData() → get current roundId
  2. Walk backward via getRoundData(roundId - N)
  3. Find first round where updatedAt <= round_end_ts
  4. Use that round's answer as btc_close_chainlink

UNPROVEN:
  - Polymarket's exact round selection logic may differ from
    "first round with updatedAt <= round_end".
  - Token UP/DOWN index sırası (market_discovery) doğrulanmamış.
"""

import time
import asyncio
import socket
import aiohttp
import aiohttp.resolver
import logger as log_module
from dataclasses import dataclass


BINANCE_REST_URL = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"

# Chainlink BTC/USD AggregatorV3 on Polygon mainnet
CHAINLINK_BTC_USD_POLYGON = "0xc907E116054Ad103354f2D350FD2514433D57F6f"
POLYGON_RPC_URLS = [
    "https://polygon.drpc.org",
    "https://polygon.publicnode.com",
    "https://1rpc.io/matic",
]
CHAINLINK_DECIMALS = 8
# Max seconds between chainlink updatedAt and round_end to be "fresh"
CHAINLINK_STALENESS_LIMIT = 600
# Max rounds to walk backward from latest
MAX_ROUND_WALKBACK = 50


def _encode_get_round_data(round_id: int) -> str:
    """Encode getRoundData(uint80) call data for eth_call."""
    # selector 0x9a6fc8f5 + uint80 padded to 32 bytes
    return "0x9a6fc8f5" + format(round_id, "064x")


def _parse_round_response(result_hex: str) -> tuple[int, float, int] | None:
    """Parse ABI response from latestRoundData() or getRoundData().
    Returns: (roundId, price, updatedAt) or None if response too short.
    ABI: (uint80 roundId, int256 answer, uint256 startedAt,
          uint256 updatedAt, uint80 answeredInRound)
    """
    if len(result_hex) < 322:
        return None
    hex_data = result_hex[2:]
    round_id = int(hex_data[0:64], 16)
    answer_raw = int(hex_data[64:128], 16)
    if answer_raw >= 2**255:
        answer_raw -= 2**256
    price = round(answer_raw / (10 ** CHAINLINK_DECIMALS), 2)
    updated_at = int(hex_data[192:256], 16)
    return round_id, price, updated_at


def _make_rpc_connector() -> aiohttp.TCPConnector:
    """IPv4 + threaded DNS for Polygon RPC calls."""
    return aiohttp.TCPConnector(
        family=socket.AF_INET,
        resolver=aiohttp.resolver.ThreadedResolver(),
    )


@dataclass
class ResolutionResult:
    """Tek bir pencere resolve sonucu."""
    window_ts: int
    interval: str                # "5m" | "15m"
    round_start_ts: int          # pencere açılış unix ts
    round_end_ts: int            # pencere kapanış unix ts

    # Binance layer
    btc_open: float
    btc_close_binance: float     # 0.0 = fetch başarısız
    binance_fetch_ok: bool       # True = gerçek fiyat alındı
    winner_binance: str          # "up" | "down" | "unknown"

    # Chainlink layer — historical round
    btc_close_chainlink: float   # 0.0 = fetch failed
    winner_chainlink: str        # "up" | "down" | "unknown"
    chainlink_status: str        # "fetched" | "fetched_stale" | error variants

    # Karşılaştırma
    resolution_match: str        # "match" | "mismatch" | "unknown"
    winner_source: str           # "binance" | "none"
    resolution_truth_status: str # "binance_only" | "dual_verified" | "dual_mismatch" | "unresolved_fetch_error"

    resolve_ts: float            # bu sonucun üretildiği unix ts


def _compute_round_timestamps(window_ts: int, interval: str) -> tuple[int, int]:
    """Pencere başlangıç ve bitiş unix timestamp'lerini hesapla."""
    divisor = 300 if interval == "5m" else 900
    round_start = window_ts
    round_end = window_ts + divisor
    return round_start, round_end


def _determine_winner(btc_open: float, btc_close: float) -> str:
    """BTC close >= open → UP wins, aksi halde DOWN wins. Tie → UP."""
    if btc_close >= btc_open:
        return "up"
    return "down"


async def _fetch_btc_close_binance() -> tuple[float, bool]:
    """
    Binance REST anlık BTC fiyatı.
    Returns: (price, success)
    Fetch başarısızsa (0.0, False) döner — sahte fallback KULLANILMAZ.
    """
    try:
        connector = aiohttp.TCPConnector(
            family=socket.AF_INET,
            resolver=aiohttp.resolver.ThreadedResolver(),
        )
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(
                BINANCE_REST_URL,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as r:
                if r.status == 200:
                    data = await r.json()
                    price = float(data["price"])
                    if price > 0:
                        return price, True
                    await log_module.log("resolution_binance_invalid_price", {
                        "price": price,
                    })
                else:
                    await log_module.log("resolution_binance_http_error", {
                        "status": r.status,
                    })
    except Exception as e:
        await log_module.log("resolution_binance_fetch_error", {"error": str(e)})
    return 0.0, False


async def _fetch_btc_close_chainlink(
    window_ts: int,
    interval: str,
    btc_open: float,
) -> tuple[float, str, str]:
    """
    Chainlink BTC/USD Price Feed on Polygon — historical round lookup.

    1. latestRoundData() → get current roundId
    2. Walk backward via getRoundData(roundId - N)
    3. Find first round where updatedAt <= round_end_ts
    4. Use that round's answer as settlement price

    Returns: (price, winner, status)
    """
    round_end = window_ts + (300 if interval == "5m" else 900)
    timeout = aiohttp.ClientTimeout(total=8)

    latest_payload = {
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [
            {"to": CHAINLINK_BTC_USD_POLYGON, "data": "0xfeaf968c"},
            "latest",
        ],
        "id": 1,
    }

    last_error = ""
    last_rpc = ""

    for rpc_url in POLYGON_RPC_URLS:
        last_rpc = rpc_url
        try:
            connector = _make_rpc_connector()
            async with aiohttp.ClientSession(connector=connector) as session:
                # --- Step 1: latestRoundData() ---
                async with session.post(
                    rpc_url, json=latest_payload, timeout=timeout,
                ) as r:
                    if r.status != 200:
                        last_error = f"http_{r.status}"
                        await log_module.log("resolution_chainlink_http_error", {
                            "rpc_url": rpc_url, "status": r.status,
                            "body": (await r.text())[:200],
                        })
                        continue

                    data = await r.json()
                    if "error" in data:
                        last_error = "rpc_error"
                        await log_module.log("resolution_chainlink_rpc_error", {
                            "rpc_url": rpc_url, "error": data["error"],
                        })
                        continue

                    parsed = _parse_round_response(data.get("result", "0x"))
                    if not parsed:
                        last_error = "short_response"
                        await log_module.log("resolution_chainlink_short_response", {
                            "rpc_url": rpc_url, "phase": "latest",
                        })
                        continue

                latest_round_id, latest_price, latest_updated_at = parsed

                # --- Step 2: Walk backward to find round at round_end_ts ---
                target_round_id = latest_round_id
                target_price = latest_price
                target_updated_at = latest_updated_at
                walkback_steps = 0

                if latest_updated_at <= round_end:
                    # Latest round is already at or before window close — use it
                    pass
                else:
                    # Walk backward
                    found = False
                    walk_id = latest_round_id - 1

                    for step in range(1, MAX_ROUND_WALKBACK + 1):
                        get_payload = {
                            "jsonrpc": "2.0",
                            "method": "eth_call",
                            "params": [
                                {
                                    "to": CHAINLINK_BTC_USD_POLYGON,
                                    "data": _encode_get_round_data(walk_id),
                                },
                                "latest",
                            ],
                            "id": step + 1,
                        }

                        # Fetch with 429 retry (max 3 attempts: 1s, 2s, 3s)
                        step_ok = False
                        resp_json = None
                        for attempt in range(3):
                            try:
                                async with session.post(
                                    rpc_url, json=get_payload, timeout=timeout,
                                ) as r2:
                                    if r2.status == 429:
                                        wait_s = (attempt + 1) * 1.0
                                        await log_module.log(
                                            "resolution_chainlink_walkback_429", {
                                                "rpc_url": rpc_url,
                                                "walk_id": walk_id,
                                                "walk_step": step,
                                                "attempt": attempt + 1,
                                                "backoff_secs": wait_s,
                                            },
                                        )
                                        await asyncio.sleep(wait_s)
                                        continue
                                    if r2.status != 200:
                                        last_error = f"walkback_http_{r2.status}"
                                        await log_module.log(
                                            "resolution_chainlink_walkback_http", {
                                                "rpc_url": rpc_url,
                                                "walk_id": walk_id,
                                                "walk_step": step,
                                                "status": r2.status,
                                            },
                                        )
                                        break
                                    resp_json = await r2.json()
                                    step_ok = True
                                    break
                            except Exception as step_exc:
                                last_error = f"walkback_exc: {str(step_exc)[:80]}"
                                break

                        if not step_ok or resp_json is None:
                            # This round's fetch failed — skip, keep walking
                            walk_id -= 1
                            continue

                        if "error" in resp_json:
                            last_error = "walkback_rpc_error"
                            await log_module.log(
                                "resolution_chainlink_walkback_rpc_error", {
                                    "rpc_url": rpc_url,
                                    "walk_id": walk_id,
                                    "walk_step": step,
                                    "error": resp_json["error"],
                                },
                            )
                            walk_id -= 1
                            continue

                        parsed2 = _parse_round_response(
                            resp_json.get("result", "0x")
                        )
                        if not parsed2:
                            last_error = "walkback_parse_fail"
                            await log_module.log(
                                "resolution_chainlink_walkback_parse_fail", {
                                    "rpc_url": rpc_url,
                                    "walk_id": walk_id,
                                    "walk_step": step,
                                    "result_len": len(
                                        resp_json.get("result", "0x")
                                    ),
                                },
                            )
                            walk_id -= 1
                            continue

                        rid, rprice, rupdated = parsed2
                        if rupdated <= round_end:
                            target_round_id = rid
                            target_price = rprice
                            target_updated_at = rupdated
                            walkback_steps = step
                            found = True
                            break

                        walk_id -= 1

                    if not found:
                        last_error = (
                            last_error
                            or f"walkback_exhausted(steps={MAX_ROUND_WALKBACK})"
                        )
                        await log_module.log("resolution_chainlink_walkback_failed", {
                            "rpc_url": rpc_url,
                            "latest_round_id": latest_round_id,
                            "latest_updated_at": latest_updated_at,
                            "round_end_ts": round_end,
                            "steps_attempted": step,
                            "last_walk_id": walk_id,
                            "last_error": last_error,
                            "window_ts": window_ts,
                        })
                        continue

                # --- Step 3: Validate and return ---
                if target_price <= 0:
                    last_error = "invalid_price"
                    await log_module.log("resolution_chainlink_invalid_price", {
                        "rpc_url": rpc_url,
                        "chainlink_round_id": target_round_id,
                        "price": target_price,
                    })
                    continue

                round_age = round_end - target_updated_at
                winner = _determine_winner(btc_open, target_price)
                status = (
                    "fetched" if round_age <= CHAINLINK_STALENESS_LIMIT
                    else "fetched_stale"
                )

                await log_module.log("resolution_chainlink_fetched", {
                    "rpc_url": rpc_url,
                    "price": target_price,
                    "chainlink_round_id": target_round_id,
                    "chainlink_updated_at": target_updated_at,
                    "chainlink_round_age_secs": round_age,
                    "round_end_ts": round_end,
                    "latest_round_id": latest_round_id,
                    "walkback_steps": walkback_steps,
                    "status": status,
                    "winner": winner,
                })

                return target_price, winner, status

        except Exception as e:
            last_error = str(e)[:120]
            await log_module.log("resolution_chainlink_rpc_fail", {
                "rpc_url": rpc_url, "error": last_error, "window_ts": window_ts,
            })
            continue

    # All RPCs failed
    from urllib.parse import urlparse
    last_host = urlparse(last_rpc).hostname or last_rpc
    status = f"all_rpc_failed({last_host}:{last_error})"
    await log_module.log("resolution_chainlink_all_failed", {
        "rpc_count": len(POLYGON_RPC_URLS),
        "last_rpc": last_rpc,
        "last_error": last_error,
        "status": status,
        "window_ts": window_ts,
    })
    return 0.0, "unknown", status


async def resolve_truth(
    window_ts: int,
    interval: str,
    btc_open: float,
) -> ResolutionResult:
    """
    Tek bir pencere için resolution truth hesapla.

    Şu an sadece Binance aktif.
    Chainlink placeholder olarak loglanır, winner_chainlink="unknown".
    İleride Chainlink fetch eklendiğinde resolution_match ve
    resolution_truth_status otomatik olarak dolacak.
    """
    round_start, round_end = _compute_round_timestamps(window_ts, interval)

    # --- Layer 1: Binance ---
    btc_close_binance, binance_ok = await _fetch_btc_close_binance()

    if binance_ok:
        winner_binance = _determine_winner(btc_open, btc_close_binance)
    else:
        # Fetch başarısız — sahte winner üretme
        winner_binance = "unknown"
        await log_module.log("resolution_binance_fetch_failed", {
            "window_ts": window_ts,
            "btc_open": btc_open,
            "note": "Binance close fetch failed. No fallback used. Winner = unknown.",
        })

    # --- Layer 2: Chainlink (Polygon RPC) ---
    btc_close_chainlink, winner_chainlink, chainlink_status = (
        await _fetch_btc_close_chainlink(window_ts, interval, btc_open)
    )

    # --- Karşılaştırma ---
    # Binance başarısız → hiçbir kaynak yok → unresolved
    if not binance_ok:
        resolution_match = "unknown"
        winner_source = "none"
        resolution_truth_status = "unresolved_fetch_error"
    elif chainlink_status in ("fetched", "fetched_stale") and winner_chainlink != "unknown":
        # İki kaynak da var → karşılaştır
        winner_source = "binance"
        if winner_binance == winner_chainlink:
            resolution_match = "match"
            resolution_truth_status = "dual_verified"
        else:
            resolution_match = "mismatch"
            resolution_truth_status = "dual_mismatch"
    else:
        # Binance var, Chainlink yok/placeholder
        resolution_match = "unknown"
        winner_source = "binance"
        resolution_truth_status = "binance_only"

    result = ResolutionResult(
        window_ts=window_ts,
        interval=interval,
        round_start_ts=round_start,
        round_end_ts=round_end,
        btc_open=btc_open,
        btc_close_binance=round(btc_close_binance, 2),
        binance_fetch_ok=binance_ok,
        winner_binance=winner_binance,
        btc_close_chainlink=round(btc_close_chainlink, 2),
        winner_chainlink=winner_chainlink,
        chainlink_status=chainlink_status,
        resolution_match=resolution_match,
        winner_source=winner_source,
        resolution_truth_status=resolution_truth_status,
        resolve_ts=time.time(),
    )

    await log_module.log("resolution_truth", {
        "window_ts": result.window_ts,
        "interval": result.interval,
        "round_start_ts": result.round_start_ts,
        "round_end_ts": result.round_end_ts,
        "btc_open": result.btc_open,
        "btc_close_binance": result.btc_close_binance,
        "binance_fetch_ok": result.binance_fetch_ok,
        "winner_binance": result.winner_binance,
        "btc_close_chainlink": result.btc_close_chainlink,
        "winner_chainlink": result.winner_chainlink,
        "chainlink_status": result.chainlink_status,
        "resolution_match": result.resolution_match,
        "winner_source": result.winner_source,
        "resolution_truth_status": result.resolution_truth_status,
    })

    return result
