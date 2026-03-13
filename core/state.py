"""
core/state.py — V21 paylaşılan bot durumu.

Tüm modüller bu tek SharedState nesnesini okur/yazar.
Thread-safe olmak için asyncio.Lock kullanılır.
"""

import time
from collections import deque
from dataclasses import dataclass, field
from typing import List, Optional


# ──────────────────────────────────────────────────────────────────
#  Veri sınıfları
# ──────────────────────────────────────────────────────────────────

@dataclass
class Position:
    id: str
    side: str           # "UP" veya "DOWN"
    token_id: str
    entry_price: float
    stake_usd: float
    shares: float
    open_time: float    # unix timestamp
    market_slug: str
    status: str = "open"   # open | closed_tp1 | closed_tp2 | closed_stop | closed_flip | resolved
    pnl: float = 0.0
    tp1_hit: bool = False


@dataclass
class MarketInfo:
    slug: str = ""
    condition_id: str = ""
    up_token_id: str = ""
    down_token_id: str = ""
    end_time: float = 0.0       # unix timestamp
    market_start: float = 0.0   # ne zaman keşfedildi
    question: str = ""


@dataclass
class BookSnapshot:
    up_bid: float = 0.0
    up_ask: float = 0.0
    down_bid: float = 0.0
    down_ask: float = 0.0
    timestamp: float = 0.0      # unix timestamp


# ──────────────────────────────────────────────────────────────────
#  Ana paylaşılan durum
# ──────────────────────────────────────────────────────────────────

class SharedState:
    def __init__(self, bankroll_usd: float = 30.0):
        # ── BTC fiyat verisi ──────────────────────────────────────
        self.btc_price: float = 0.0
        self.btc_price_ts: float = 0.0

        # ── OFI / TFI ────────────────────────────────────────────
        self.ofi_value: float = 0.0
        self.ofi_z: float = 0.0
        self.ofi_n: int = 0
        self.ofi_ratio: float = 0.0
        self.tfi_value: float = 0.0
        self.tfi_n: int = 0
        self.signal_valid: bool = False
        self.signal_direction: str = ""       # "UP" | "DOWN" | ""
        self.signal_conviction: float = 0.0  # normalized z / z_threshold

        # ── Piyasa bilgisi ────────────────────────────────────────
        self.market: MarketInfo = MarketInfo()
        self.book: BookSnapshot = BookSnapshot()

        # ── Pozisyonlar ──────────────────────────────────────────
        self.open_positions: List[Position] = []
        self.closed_positions: List[Position] = []

        # ── Bankroll ──────────────────────────────────────────────
        self.bankroll: float = bankroll_usd
        self.initial_bankroll: float = bankroll_usd
        self.peak_bankroll: float = bankroll_usd   # peak-based drawdown için
        self.total_pnl: float = 0.0
        self.win_count: int = 0
        self.loss_count: int = 0

        # ── Durum / kontrol ──────────────────────────────────────
        self.bot_status: str = "BAŞLANGIC"
        self.paused: bool = False
        self.pause_reason: str = ""
        self.start_time: float = time.time()

        # ── Event log (dashboard için) ────────────────────────────
        self.events: deque = deque(maxlen=20)

    # ── Yardımcı metodlar ─────────────────────────────────────────

    def log_event(self, msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        self.events.appendleft(f"[{ts}] {msg}")

    def seconds_to_market_end(self) -> float:
        if not self.market.end_time:
            return 0.0
        return max(0.0, self.market.end_time - time.time())

    def is_book_fresh(self, limit_ms: float | None = None) -> bool:
        if not self.book.timestamp:
            return False
        ms = limit_ms if limit_ms is not None else 5000.0
        return (time.time() - self.book.timestamp) * 1000 < ms

    def is_btc_fresh(self, limit_ms: float | None = None) -> bool:
        if not self.btc_price_ts:
            return False
        ms = limit_ms if limit_ms is not None else 5000.0
        return (time.time() - self.btc_price_ts) * 1000 < ms

    def drawdown_pct(self) -> float:
        """Peak-based drawdown. Bankroll.update_peak() ile senkronize tutulur."""
        if self.peak_bankroll <= 0:
            return 0.0
        if self.bankroll > self.peak_bankroll:
            self.peak_bankroll = self.bankroll
        return max(0.0, (self.peak_bankroll - self.bankroll) / self.peak_bankroll * 100.0)

    def win_rate(self) -> float:
        total = self.win_count + self.loss_count
        return self.win_count / total if total > 0 else 0.0

    def uptime_str(self) -> str:
        elapsed = int(time.time() - self.start_time)
        h, rem = divmod(elapsed, 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"
