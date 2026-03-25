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
from polymarket_feed import BookSnapshot
import logger as log_module


@dataclass
class Signal:
    action: str          # "buy_up" | "buy_down" | "skip"
    direction: str       # "up" | "down" | "none"
    delta_pct: float
    p_entry: float       # market fiyatı (ask — gerçekçi fill price)
    p_signal: float      # tahmini olasılık
    edge_pct: float
    spread_pct: float    # orderbook spread kalitesi
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
        self.max_spread_pct: float = config.get("max_spread_pct", 2.0)
        self.min_depth: float = config.get("min_orderbook_depth", 50.0)
        self.entry_window_start: int = config.get("entry_window_start_ste", 45)
        self.entry_window_end: int = config.get("entry_window_end_ste", 10)
        self.fee_rate: float = config.get("fee_rate", 0.072)
        self.fee_exponent: float = config.get("fee_exponent", 1.0)
        self.quote_buckets: dict = config.get("quote_buckets", {
            "B1": [0.83, 0.86],
            "B2": [0.87, 0.90],
            "B3": [0.91, 0.93],
        })
        # Bucket geçerli fiyat aralığı — config'den türetilir
        all_vals = [v for bucket in self.quote_buckets.values() for v in bucket]
        self._bucket_lo: float = min(all_vals)
        self._bucket_hi: float = max(all_vals)

    def _skip(self, reason: str, secs_to_res: int, delta_pct: float = 0.0) -> "Signal":
        return Signal(
            action="skip", direction="none",
            delta_pct=delta_pct, p_entry=0.0, p_signal=0.0,
            edge_pct=0.0, spread_pct=0.0,
            secs_to_res=secs_to_res, reason=reason,
        )

    def evaluate(
        self,
        open_price: float,
        current_price: float,
        book_up: BookSnapshot | None,    # UP token orderbook
        book_down: BookSnapshot | None,  # DOWN token orderbook
        secs_to_res: int,
    ) -> Signal:
        """
        Sinyal üret (tick-driven — her Binance tick'inde çağrılır).

        Returns Signal dataclass.
        """
        # Delta hesapla — her şeyden önce (log'da her zaman görünsün)
        if open_price <= 0:
            return self._skip("no_open_price", secs_to_res)

        delta_pct = (current_price - open_price) / open_price * 100.0
        delta_abs = abs(delta_pct)

        # Entry window kontrolü
        if secs_to_res > self.entry_window_start or secs_to_res < self.entry_window_end:
            return self._skip("outside_entry_window", secs_to_res, delta_pct)

        # Orderbook data mevcut mu?
        if book_up is None or book_down is None:
            return self._skip("no_orderbook", secs_to_res, delta_pct)

        # Min delta filtresi
        if delta_abs < self.min_delta_pct:
            return self._skip(f"delta_too_small({delta_abs:.3f}%)", secs_to_res, delta_pct)

        # Yön belirle
        if delta_pct > 0:
            direction = "up"
            book = book_up
        else:
            direction = "down"
            book = book_down

        # Spread kalite filtresi — geniş spread = belirsiz fiyat
        if book.spread_pct > self.max_spread_pct:
            return Signal(
                action="skip", direction=direction,
                delta_pct=delta_pct, p_entry=book.mid, p_signal=0.0,
                edge_pct=0.0, spread_pct=book.spread_pct,
                secs_to_res=secs_to_res,
                reason=f"spread_too_wide({book.spread_pct:.1f}%>{self.max_spread_pct}%)",
            )

        # Orderbook depth filtresi — ince kitap = güvenilmez fiyat
        if book.ask_size < self.min_depth or book.bid_size < self.min_depth:
            return Signal(
                action="skip", direction=direction,
                delta_pct=delta_pct, p_entry=book.mid, p_signal=0.0,
                edge_pct=0.0, spread_pct=book.spread_pct,
                secs_to_res=secs_to_res,
                reason=f"depth_low(bid={book.bid_size:.0f}<{self.min_depth} ask={book.ask_size:.0f})",
            )

        # Entry price: UP sinyali → UP token ask, DOWN → DOWN token ask
        p_entry = book.ask
        if not (0 < p_entry < 1):
            return self._skip("invalid_p_entry", secs_to_res, delta_pct)

        # Bucket range kontrolü — fiyat stratejimizin geçerli aralığında mı?
        if p_entry < self._bucket_lo or p_entry > self._bucket_hi:
            return Signal(
                action="skip", direction=direction,
                delta_pct=delta_pct, p_entry=p_entry, p_signal=0.0,
                edge_pct=0.0, spread_pct=book.spread_pct,
                secs_to_res=secs_to_res,
                reason=f"no_bucket(ask={p_entry:.3f} range=[{self._bucket_lo},{self._bucket_hi}])",
            )

        # Signal probability (delta büyüklüğünden)
        p_signal = _delta_to_p_signal(delta_abs, self.quote_buckets)

        # Edge kontrolü (ask fiyatı kullanarak — daha muhafazakâr)
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
                edge_pct=edge_pct, spread_pct=book.spread_pct,
                secs_to_res=secs_to_res,
                reason=f"no_edge({edge_pct:.2f}%<{self.min_edge_pct}%)",
            )

        return Signal(
            action=f"buy_{direction}", direction=direction,
            delta_pct=delta_pct, p_entry=p_entry, p_signal=p_signal,
            edge_pct=edge_pct, spread_pct=book.spread_pct,
            secs_to_res=secs_to_res, reason="edge_ok",
        )

    async def log_signal(self, signal: Signal) -> None:
        await log_module.log("signal", {
            "action": signal.action,
            "direction": signal.direction,
            "delta_pct": round(signal.delta_pct, 4),
            "p_entry": signal.p_entry,
            "p_signal": signal.p_signal,
            "edge_pct": round(signal.edge_pct, 2),
            "spread_pct": signal.spread_pct,
            "secs_to_res": signal.secs_to_res,
            "reason": signal.reason,
        })
