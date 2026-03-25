"""
Signal Engine — BTC delta → probability → edge → sinyal üret.

Mantık:
  1. open_price: pencerenin ilk 5 saniyesinde yakalanan Binance mid fiyatı
  2. current_price: son Binance mid
  3. delta_pct = (current - open) / open * 100
  4. delta > 0 → UP sinyali (BTC yükseldi), delta < 0 → DOWN sinyali
  5. delta büyüklüğüne göre quote_buckets'tan olasılık seç
  6. fee_engine ile edge hesapla
  7. Entry window: secs_to_resolution ∈ [entry_window_start_ste, entry_window_end_ste]

Karar:
  - delta_pct < min_delta_pct → skip (delta çok küçük)
  - edge < min_edge_pct → skip (fiyat + fee'yi geçemiyor)
  - Değilse → trade sinyali

Quote buckets (örn. config):
  B1: [0.83, 0.86]  → küçük delta
  B2: [0.87, 0.90]  → orta delta
  B3: [0.91, 0.93]  → büyük delta
  Aralık dışı → en yüksek bucket'ın üst sınırı
"""

from dataclasses import dataclass
from fee_engine import is_edge_positive
import logger as log_module


@dataclass
class Signal:
    action: str          # "buy_up" | "buy_down" | "skip"
    direction: str       # "up" | "down" | "none"
    delta_pct: float
    p_entry: float       # market fiyatı (midpoint)
    p_signal: float      # tahmini olasılık
    edge_pct: float
    secs_to_res: int
    reason: str


def _delta_to_p_signal(delta_pct_abs: float, buckets: dict) -> float:
    """
    |delta_pct| → tahmini true probability.
    Buckets sıralı (küçük → büyük delta).
    """
    # Bucket eşik değerleri: B1 < B2 < B3 delta büyüklüğü varsayımı
    # Delta aralığı: B1=0-0.10%, B2=0.10-0.20%, B3=0.20%+
    bucket_deltas = [0.10, 0.20]  # hardcoded geçiş noktaları

    sorted_buckets = sorted(buckets.keys())  # B1, B2, B3

    for i, key in enumerate(sorted_buckets):
        threshold = bucket_deltas[i] if i < len(bucket_deltas) else float("inf")
        if delta_pct_abs <= threshold:
            lo, hi = buckets[key]
            # Delta içindeki konuma göre interpolasyon
            if i == 0:
                t = delta_pct_abs / threshold
            else:
                prev_t = bucket_deltas[i - 1]
                t = (delta_pct_abs - prev_t) / (threshold - prev_t)
            t = max(0.0, min(1.0, t))
            return lo + t * (hi - lo)

    # En büyük bucket'ın üst sınırı
    last_key = sorted_buckets[-1]
    return buckets[last_key][1]


class SignalEngine:
    def __init__(self, config: dict):
        self.min_delta_pct: float = config.get("min_delta_pct", 0.05)
        self.min_edge_pct: float = config.get("min_edge_pct", 1.0)
        self.entry_window_start: int = config.get("entry_window_start_ste", 45)
        self.entry_window_end: int = config.get("entry_window_end_ste", 10)
        self.fee_rate: float = config.get("fee_rate", 0.072)
        self.fee_exponent: float = config.get("fee_exponent", 1.0)
        self.quote_buckets: dict = config.get("quote_buckets", {
            "B1": [0.83, 0.86],
            "B2": [0.87, 0.90],
            "B3": [0.91, 0.93],
        })

    def evaluate(
        self,
        open_price: float,
        current_price: float,
        p_entry_up: float,      # CLOB midpoint for UP token
        p_entry_down: float,    # CLOB midpoint for DOWN token
        secs_to_res: int,
    ) -> Signal:
        """
        Sinyal üret.

        Returns Signal dataclass.
        """
        # Entry window kontrolü
        if secs_to_res > self.entry_window_start or secs_to_res < self.entry_window_end:
            return Signal(
                action="skip", direction="none",
                delta_pct=0.0, p_entry=0.0, p_signal=0.0, edge_pct=0.0,
                secs_to_res=secs_to_res, reason="outside_entry_window",
            )

        # Delta hesapla
        if open_price <= 0:
            return Signal(
                action="skip", direction="none",
                delta_pct=0.0, p_entry=0.0, p_signal=0.0, edge_pct=0.0,
                secs_to_res=secs_to_res, reason="no_open_price",
            )

        delta_pct = (current_price - open_price) / open_price * 100.0
        delta_abs = abs(delta_pct)

        # Min delta filtresi
        if delta_abs < self.min_delta_pct:
            return Signal(
                action="skip", direction="none",
                delta_pct=delta_pct, p_entry=0.0, p_signal=0.0, edge_pct=0.0,
                secs_to_res=secs_to_res, reason=f"delta_too_small({delta_abs:.3f}%)",
            )

        # Yön belirle
        if delta_pct > 0:
            direction = "up"
            p_entry = p_entry_up
        else:
            direction = "down"
            p_entry = p_entry_down

        if p_entry <= 0 or p_entry >= 1:
            return Signal(
                action="skip", direction=direction,
                delta_pct=delta_pct, p_entry=p_entry, p_signal=0.0, edge_pct=0.0,
                secs_to_res=secs_to_res, reason="invalid_p_entry",
            )

        # Signal probability
        p_signal = _delta_to_p_signal(delta_abs, self.quote_buckets)

        # Edge kontrolü
        has_edge, edge_pct = is_edge_positive(
            p_entry=p_entry,
            p_true=p_signal,
            fee_rate=self.fee_rate,
            fee_exponent=self.fee_exponent,
            min_edge_pct=self.min_edge_pct,
        )

        if not has_edge:
            return Signal(
                action="skip", direction=direction,
                delta_pct=delta_pct, p_entry=p_entry, p_signal=p_signal,
                edge_pct=edge_pct, secs_to_res=secs_to_res,
                reason=f"no_edge({edge_pct:.2f}%<{self.min_edge_pct}%)",
            )

        action = f"buy_{direction}"
        return Signal(
            action=action, direction=direction,
            delta_pct=delta_pct, p_entry=p_entry, p_signal=p_signal,
            edge_pct=edge_pct, secs_to_res=secs_to_res, reason="edge_ok",
        )

    async def log_signal(self, signal: Signal) -> None:
        await log_module.log("signal", {
            "action": signal.action,
            "direction": signal.direction,
            "delta_pct": round(signal.delta_pct, 4),
            "p_entry": signal.p_entry,
            "p_signal": signal.p_signal,
            "edge_pct": round(signal.edge_pct, 2),
            "secs_to_res": signal.secs_to_res,
            "reason": signal.reason,
        })
