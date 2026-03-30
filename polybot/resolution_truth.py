"""
Resolution Truth — Resolve anında winner belirlemek için truth layer.

Katmanlar:
  1. Binance REST: btc_close çeker, btc_open ile karşılaştırır → winner_binance
  2. Chainlink:    Polygon RPC eth_call → latestRoundData() on BTC/USD feed
  3. Karşılaştırma: iki kaynak uyuşuyor mu? → resolution_match

Polymarket BTC 5m/15m marketleri Chainlink ile resolve oluyor.
Binance close yalnızca proxy/sinyal — canonical truth değil.

UNPROVEN:
  - latestRoundData() returns current round, not historical — acceptable
    when called ~130s after window close, but not for replaying old windows.
  - Chainlink'in tam resolution timestamp'i ile Binance REST çağrı anı
    arasındaki sapma ölçülmemiş.
  - Token UP/DOWN index sırası (market_discovery) doğrulanmamış.
"""

import time
import socket
import aiohttp
import aiohttp.resolver
import logger as log_module
from dataclasses import dataclass


BINANCE_REST_URL = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"

# Chainlink BTC/USD AggregatorV3 on Polygon mainnet
CHAINLINK_BTC_USD_POLYGON = "0xc907E116054Ad103354f2D350FD2514433D57F6f"
POLYGON_RPC_URL = "https://polygon-rpc.com"
CHAINLINK_DECIMALS = 8
# Max seconds between chainlink updatedAt and round_end to be "fresh"
CHAINLINK_STALENESS_LIMIT = 600


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

    # Chainlink layer — PLACEHOLDER
    btc_close_chainlink: float   # 0.0 = henüz fetch edilmedi
    winner_chainlink: str        # "up" | "down" | "unknown"
    chainlink_status: str        # "fetched" | "placeholder" | "error"

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
    Chainlink BTC/USD Price Feed on Polygon via raw JSON-RPC eth_call.

    Calls latestRoundData() on AggregatorV3Interface at CHAINLINK_BTC_USD_POLYGON.
    Response ABI: (uint80 roundId, int256 answer, uint256 startedAt,
                   uint256 updatedAt, uint80 answeredInRound)

    Returns: (price, winner, status)
      status: "fetched" | "fetched_stale" | "http_error" | "rpc_error" |
              "short_response" | "invalid_price" | "fetch_error"
    """
    round_end = window_ts + (300 if interval == "5m" else 900)

    payload = {
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [
            {
                "to": CHAINLINK_BTC_USD_POLYGON,
                "data": "0xfeaf968c",  # latestRoundData()
            },
            "latest",
        ],
        "id": 1,
    }

    try:
        connector = aiohttp.TCPConnector(
            family=socket.AF_INET,
            resolver=aiohttp.resolver.ThreadedResolver(),
        )
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.post(
                POLYGON_RPC_URL,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=8),
            ) as r:
                if r.status != 200:
                    body = await r.text()
                    await log_module.log("resolution_chainlink_http_error", {
                        "status": r.status, "body": body[:200],
                    })
                    return 0.0, "unknown", f"http_error_{r.status}"

                data = await r.json()

                if "error" in data:
                    await log_module.log("resolution_chainlink_rpc_error", {
                        "error": data["error"],
                    })
                    return 0.0, "unknown", "rpc_error"

                result_hex = data.get("result", "0x")
                # 5 ABI slots × 64 hex chars + "0x" prefix = 322 chars minimum
                if len(result_hex) < 322:
                    await log_module.log("resolution_chainlink_short_response", {
                        "result_len": len(result_hex),
                    })
                    return 0.0, "unknown", "short_response"

                hex_data = result_hex[2:]
                # Slot 1 (offset 64-128): answer (int256), 8 decimals
                answer_raw = int(hex_data[64:128], 16)
                if answer_raw >= 2**255:
                    answer_raw -= 2**256
                price = round(answer_raw / (10 ** CHAINLINK_DECIMALS), 2)

                # Slot 3 (offset 192-256): updatedAt (uint256)
                updated_at = int(hex_data[192:256], 16)

                staleness = abs(updated_at - round_end)

                if price <= 0:
                    await log_module.log("resolution_chainlink_invalid_price", {
                        "raw_answer": answer_raw, "updated_at": updated_at,
                    })
                    return 0.0, "unknown", "invalid_price"

                winner = _determine_winner(btc_open, price)
                status = "fetched" if staleness <= CHAINLINK_STALENESS_LIMIT else "fetched_stale"

                await log_module.log("resolution_chainlink_fetched", {
                    "price": price,
                    "updated_at": updated_at,
                    "round_end_ts": round_end,
                    "staleness_secs": staleness,
                    "status": status,
                    "winner": winner,
                })

                return price, winner, status

    except Exception as e:
        await log_module.log("resolution_chainlink_fetch_error", {
            "error": str(e), "window_ts": window_ts,
        })
        return 0.0, "unknown", "fetch_error"


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
