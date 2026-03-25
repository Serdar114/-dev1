"""
Resolution Truth — Resolve anında winner belirlemek için truth layer.

Katmanlar:
  1. Binance REST: btc_close çeker, btc_open ile karşılaştırır → winner_binance
  2. Chainlink:    PLACEHOLDER — henüz gerçek fetch yok
  3. Karşılaştırma: iki kaynak uyuşuyor mu? → resolution_match

Polymarket BTC 5m/15m marketleri Chainlink ile resolve oluyor.
Binance close yalnızca proxy/sinyal — canonical truth değil.

UNPROVEN:
  - Chainlink fetch henüz implemente değil (placeholder).
  - Chainlink'in tam resolution timestamp'i ile Binance REST çağrı anı
    arasındaki sapma ölçülmemiş.
  - Token UP/DOWN index sırası (market_discovery) doğrulanmamış.
"""

import time
import aiohttp
import logger as log_module
from dataclasses import dataclass


BINANCE_REST_URL = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"


@dataclass
class ResolutionResult:
    """Tek bir pencere resolve sonucu."""
    window_ts: int
    interval: str                # "5m" | "15m"
    round_start_ts: int          # pencere açılış unix ts
    round_end_ts: int            # pencere kapanış unix ts

    # Binance layer
    btc_open: float
    btc_close_binance: float
    winner_binance: str          # "up" | "down"

    # Chainlink layer — PLACEHOLDER
    btc_close_chainlink: float   # 0.0 = henüz fetch edilmedi
    winner_chainlink: str        # "up" | "down" | "unknown"
    chainlink_status: str        # "fetched" | "placeholder" | "error"

    # Karşılaştırma
    resolution_match: str        # "match" | "mismatch" | "unknown"
    winner_source: str           # "binance" (şimdilik tek aktif kaynak)
    resolution_truth_status: str # "binance_only" | "dual_verified" | "dual_mismatch"

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


async def _fetch_btc_close_binance(btc_open_fallback: float) -> tuple[float, bool]:
    """
    Binance REST anlık BTC fiyatı.
    Returns: (price, success)
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                BINANCE_REST_URL,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as r:
                if r.status == 200:
                    data = await r.json()
                    return float(data["price"]), True
    except Exception as e:
        await log_module.log("resolution_binance_fetch_error", {"error": str(e)})
    return btc_open_fallback, False


async def _fetch_btc_close_chainlink(
    window_ts: int,
    interval: str,
) -> tuple[float, str, str]:
    """
    Chainlink resolution fetch — PLACEHOLDER.

    Gerçek implementasyon için gerekli:
      - Chainlink Price Feed contract adresi (Polygon)
      - Web3 provider (Polygon RPC)
      - getRoundData() veya latestRoundData() çağrısı
      - Polymarket'in kullandığı exact resolution logic (hangi round, hangi timestamp)

    Returns: (price, winner, status)
      price = 0.0  → henüz fetch edilmedi
      winner = "unknown"
      status = "placeholder"
    """
    # PLACEHOLDER — gerçek Chainlink fetch buraya gelecek
    await log_module.log("resolution_chainlink_placeholder", {
        "window_ts": window_ts,
        "interval": interval,
        "note": "UNPROVEN: Chainlink fetch not implemented. "
                "This is a placeholder skeleton. "
                "Real implementation requires Polygon Web3 + Chainlink AggregatorV3 contract.",
    })
    return 0.0, "unknown", "placeholder"


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
    btc_close_binance, binance_ok = await _fetch_btc_close_binance(btc_open)
    winner_binance = _determine_winner(btc_open, btc_close_binance)

    if not binance_ok:
        await log_module.log("resolution_binance_fallback", {
            "window_ts": window_ts,
            "using": "btc_open",
            "price": btc_open,
        })

    # --- Layer 2: Chainlink (PLACEHOLDER) ---
    btc_close_chainlink, winner_chainlink, chainlink_status = (
        await _fetch_btc_close_chainlink(window_ts, interval)
    )

    # --- Karşılaştırma ---
    if chainlink_status == "fetched" and winner_chainlink != "unknown":
        if winner_binance == winner_chainlink:
            resolution_match = "match"
            resolution_truth_status = "dual_verified"
        else:
            resolution_match = "mismatch"
            resolution_truth_status = "dual_mismatch"
    else:
        resolution_match = "unknown"
        resolution_truth_status = "binance_only"

    result = ResolutionResult(
        window_ts=window_ts,
        interval=interval,
        round_start_ts=round_start,
        round_end_ts=round_end,
        btc_open=btc_open,
        btc_close_binance=round(btc_close_binance, 2),
        winner_binance=winner_binance,
        btc_close_chainlink=round(btc_close_chainlink, 2),
        winner_chainlink=winner_chainlink,
        chainlink_status=chainlink_status,
        resolution_match=resolution_match,
        winner_source="binance",
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
        "winner_binance": result.winner_binance,
        "btc_close_chainlink": result.btc_close_chainlink,
        "winner_chainlink": result.winner_chainlink,
        "chainlink_status": result.chainlink_status,
        "resolution_match": result.resolution_match,
        "winner_source": result.winner_source,
        "resolution_truth_status": result.resolution_truth_status,
    })

    return result
