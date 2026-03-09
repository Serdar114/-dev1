#!/usr/bin/env python3
"""
Polymarket Krajekis Auto-Sniper V17.0  (BTC 5DK/15DK PAPER TEST EDİSYONU)
==========================================================================
V16 → V17 DEĞİŞİKLİKLERİ:

  BTC FİYAT DÜZELTMESİ (V17):
  + 3. WS URL: wss://stream.binance.com:443  (port 9443 bloke ağlar için)
  + REST fallback task: WS fiyatı 30s güncellenmediyse Binance ticker REST
  + self._btc_price_ts ile fiyat tazeliği izleme
  + UI header'da kaynak etiketi: [WS] / [REST] / [STALE]

  KELLY OVERBETTİNG FİXİ (V17):
  + _calc_kelly_stake(): max(stake, min_stake) KALDIRILDI
  + _analyze(): hesaplanan stake < min_stake → "KELLY<MIN" — işlem yapılmaz
  + Bankroll < 3×min_stake → "KASA YETERSIZ" — tüm işlemler duraklatılır

  CIRCUIT BREAKER (V17 YENİ):
  + N art arda zarar → pause (varsayılan: 3 zarar → 10dk bekleme)
  + _consec_losses sayacı; WIN → sıfırla, LOSS → arttır
  + config: circuit_breaker_losses=3, circuit_breaker_pause_min=10

  API RATE LİMİTER (V17 YENİ):
  + RateLimiter sınıfı: 10dk penceresinde CLOB istek sayısı
  + Polymarket limit: 36.000 req/10min
  + Uyarı eşiği: >30000 sarı, >34000 kırmızı, >35500 işlem bloke
  + _update_markets / _fetch_book / place_buy / _check_resolved → tick()

  LATENCY GATE DÜZELTMESİ:
  + max_fok_latency_p90_ms varsayılanı: 500ms  (önceki 2500ms işlevsizdi)
  + scan_interval_secs: 10s  (5dk piyasa keşfi için daha sık tarama)

  DEĞİŞMEYEN PARÇALAR (V16'dan korundu):
  + 5dk/15dk BTC slug tarayıcı (_update_markets)
  + OFIBuffer 15dk kümülatif (Binance depth10@100ms)
  + Monte Carlo başlangıç simülasyonu
  + OrderManager (CLOB auth, FOK/GTC, cancel, approvals)
  + Quadratic fee modeli
  + Market timing penceresi (%20-%60 elapsed)
  + Emergency SL exit (piyasa fiyatı < eşik)
  + Rich terminal UI
"""
import sys
import asyncio
import socket
import aiohttp
import json
import time
import os
import random
import statistics
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, List, Tuple
from rich.console import Console
from rich.table import Table
from rich.live import Live
from rich.panel import Panel
from rich.layout import Layout
from rich.text import Text
from rich import box

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType
    BUY, SELL = "BUY", "SELL"
    CLOB_OK = True
except ImportError:
    CLOB_OK = False


# ============================================================ config / utils

def load_config(path: str = "config_v17.json") -> dict:
    if not os.path.exists(path):
        raise FileNotFoundError(f"{path} bulunamadi!")
    with open(path) as f:
        return json.load(f)


def _cfg_has_creds(cfg: dict) -> bool:
    c = cfg.get("credentials", {})
    return bool(c.get("api_key") and c.get("private_key") and
                c.get("private_key") not in ("0x", ""))


def _parse_clob_token_ids(raw) -> list:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            res = json.loads(raw)
            return res if isinstance(res, list) else []
        except Exception:
            return []
    return []


def _safe_price(p: float) -> float:
    return round(max(0.01, min(0.99, float(p))), 4)


def _safe_amounts(price: float, stake: float) -> Tuple[float, float]:
    price_r    = round(max(0.01, min(0.99, float(price))), 2)
    shares_int = float(int(round(stake / price_r, 4)))
    return price_r, shares_int


# ============================================================ Fee formülü

def _calc_fee_shares(price: float, raw_shares: float,
                     fee_rate: float = 0.25, exponent: int = 2,
                     fees_enabled: bool = True) -> float:
    """Polymarket kuadratik taker ücreti: fee = shares × rate × (p×(1-p))^exp"""
    if not fees_enabled:
        return 0.0
    p = max(0.01, min(0.99, price))
    return raw_shares * fee_rate * (p * (1.0 - p)) ** exponent


def _calc_fee_usd(price: float, raw_shares: float,
                  fee_rate: float = 0.25, exponent: int = 2,
                  fees_enabled: bool = True) -> float:
    return _calc_fee_shares(price, raw_shares, fee_rate, exponent, fees_enabled)


def _calc_ev_settle(entry: float, raw_shares: float, p_win: float,
                    fee_rate: float = 0.25, exponent: int = 2,
                    fees_enabled: bool = True) -> float:
    """Binary settlement EV = P_win × net_shares - cost"""
    net_shares = raw_shares - _calc_fee_shares(
        entry, raw_shares, fee_rate, exponent, fees_enabled)
    return p_win * net_shares - raw_shares * entry


# ============================================================ Kelly Kriteri (V17: overbetting fix)

def _calc_kelly_stake(bankroll: float, win_rate: float, entry_price: float,
                      kelly_fraction: float = 0.5,
                      min_stake: float = 2.5,
                      max_stake_pct: float = 0.4) -> float:
    """
    Kelly Kriteri ile optimal stake hesabı.

    V17 DEĞİŞİKLİĞİ: max(stake, min_stake) KALDIRILDI.
    Hesaplanan stake < min_stake ise 0.0 döner → çağıran "KELLY<MIN" gösterir.
    Bu sayede küçük kasada overbetting önlenir.

    f* = (b×p - q) / b
      b = (1 - entry) / entry  (net kazanç oranı)
      p = win_rate
      q = 1 - win_rate
    """
    entry = max(0.01, min(0.99, entry_price))
    b = (1.0 - entry) / entry
    p = max(0.01, min(0.99, win_rate))
    q = 1.0 - p

    f_full = (b * p - q) / b
    if f_full <= 0:
        return 0.0  # negatif EV

    f_applied = f_full * kelly_fraction
    stake     = bankroll * f_applied
    max_stake = bankroll * max_stake_pct
    stake     = min(stake, max_stake)

    # V17: min_stake zorlaması YOK — çağıran kontrol eder
    if stake > bankroll:
        return 0.0
    return round(stake, 2)


# ============================================================ LatencyTracker

class LatencyTracker:
    def __init__(self, maxlen: int = 50):
        self._rtts: deque = deque(maxlen=maxlen)

    def record(self, rtt_ms: float) -> None:
        self._rtts.append(rtt_ms)

    @property
    def count(self) -> int:
        return len(self._rtts)

    @property
    def p50(self) -> float:
        if not self._rtts:
            return 0.0
        s = sorted(self._rtts)
        return s[len(s) // 2]

    @property
    def p90(self) -> float:
        if not self._rtts:
            return 0.0
        s = sorted(self._rtts)
        return s[int(len(s) * 0.9)]

    @property
    def mean(self) -> float:
        if not self._rtts:
            return 0.0
        return sum(self._rtts) / len(self._rtts)

    def summary(self) -> str:
        if not self._rtts:
            return "—"
        return f"p50={self.p50:.0f}ms p90={self.p90:.0f}ms avg={self.mean:.0f}ms n={self.count}"


# ============================================================ RateLimiter (V17 YENİ)

class RateLimiter:
    """
    Polymarket CLOB API rate limit izleyici.
    Limit: 36.000 istek / 10 dakika
    Uyarı: >30.000, Kritik: >34.000, Bloke: >35.500
    """
    POLYMARKET_LIMIT = 36_000
    WINDOW_SECS      = 600   # 10 dakika

    def __init__(self):
        self._timestamps: deque = deque()

    def tick(self) -> bool:
        """Yeni istek kaydet. False döndürürse limit aşıldı."""
        now    = time.time()
        cutoff = now - self.WINDOW_SECS
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()
        self._timestamps.append(now)
        return len(self._timestamps) < self.POLYMARKET_LIMIT

    @property
    def count_10min(self) -> int:
        now    = time.time()
        cutoff = now - self.WINDOW_SECS
        return sum(1 for t in self._timestamps if t >= cutoff)

    @property
    def usage_pct(self) -> float:
        return self.count_10min / self.POLYMARKET_LIMIT * 100

    @property
    def status_rich(self) -> str:
        n   = self.count_10min
        pct = self.usage_pct
        if pct < 83:
            return f"[green]{n}/{self.POLYMARKET_LIMIT}[/green]"
        elif pct < 94:
            return f"[yellow]{n}/{self.POLYMARKET_LIMIT} uyarı[/yellow]"
        else:
            return f"[bold red]{n}/{self.POLYMARKET_LIMIT} KRİTİK![/bold red]"

    @property
    def is_ok(self) -> bool:
        return self.count_10min < 35_500


# ============================================================ OFIBuffer (V16'dan korundu)

class OFIBuffer:
    """
    15 dakikalık kümülatif Order Flow Imbalance takipçisi.
    Binance BTC/USDT depth10@100ms WebSocket ile beslenir.
    Her 1 saniyede bir delta kaydedilir (snapshot_interval_s).
    """
    def __init__(self, lookback_minutes: int = 15,
                 snapshot_interval_s: float = 1.0):
        self.lookback_secs       = lookback_minutes * 60
        self.snapshot_interval_s = snapshot_interval_s
        self._deltas:        deque = deque()
        self._prev_bid:      float = 0.0
        self._prev_ask:      float = 0.0
        self._last_snapshot: float = 0.0
        self._last_data_ts:  float = 0.0

    def update(self, bid_depth: float, ask_depth: float) -> None:
        now = time.time()
        if now - self._last_snapshot < self.snapshot_interval_s:
            return
        self._last_snapshot = now
        self._last_data_ts  = now

        bid_delta = bid_depth - self._prev_bid if self._prev_bid > 0 else 0.0
        ask_delta = ask_depth - self._prev_ask if self._prev_ask > 0 else 0.0
        self._prev_bid = bid_depth
        self._prev_ask = ask_depth

        self._deltas.append((now, bid_delta, ask_delta))
        cutoff = now - self.lookback_secs
        while self._deltas and self._deltas[0][0] < cutoff:
            self._deltas.popleft()

    @property
    def cumulative_bid(self) -> float:
        return sum(max(0.0, d[1]) for d in self._deltas)

    @property
    def cumulative_ask(self) -> float:
        return sum(max(0.0, d[2]) for d in self._deltas)

    @property
    def ratio(self) -> float:
        """Bid/Ask kümülatif oranı. >1 = alıcı baskısı."""
        ask = self.cumulative_ask
        if ask <= 0:
            return 1.0
        return self.cumulative_bid / ask

    @property
    def z_score(self) -> float:
        """Net delta serisi Z-skoru."""
        net_deltas = [d[1] - d[2] for d in self._deltas]
        if len(net_deltas) < 3:
            return 0.0
        try:
            mu  = statistics.mean(net_deltas)
            std = statistics.stdev(net_deltas)
            if std < 1e-8:
                return 0.0
            return mu / std
        except Exception:
            return 0.0

    @property
    def sample_count(self) -> int:
        return len(self._deltas)

    @property
    def data_age_ms(self) -> float:
        if self._last_data_ts == 0.0:
            return 9999.0
        return (time.time() - self._last_data_ts) * 1000.0

    def signal(self, ratio_threshold: float = 3.0,
               z_threshold: float = 3.0) -> Optional[str]:
        """
        YES: bid/ask > threshold VE z > z_threshold
        NO:  ask/bid > threshold VE z < -z_threshold
        None: yetersiz sinyal
        """
        if self.sample_count < 5:
            return None
        r   = self.ratio
        z   = self.z_score
        inv = (1.0 / r) if r > 0 else 0.0
        if r > ratio_threshold and z > z_threshold:
            return "YES"
        if inv > ratio_threshold and z < -z_threshold:
            return "NO"
        return None


# ============================================================ Monte Carlo (V16'dan korundu)

def run_monte_carlo(
    win_rate: float, kelly_fraction: float, start_bankroll: float,
    target: float, min_stake: float, trades_per_day: int, days: int,
    n_simulations: int = 1000, avg_entry: float = 0.38,
) -> dict:
    """
    Monte Carlo bankroll simülasyonu.
    V17: Kelly < min_stake durumunda işlem yapılmaz (overbetting fix yansıtıldı).
    """
    total_trades = trades_per_day * days
    b = (1.0 - avg_entry) / avg_entry

    success = ruin = 0
    finals:  List[float] = []

    for _ in range(n_simulations):
        bankroll       = start_bankroll
        reached_target = False
        hit_ruin       = False

        for _ in range(total_trades):
            if bankroll < min_stake:
                hit_ruin = True
                break

            f_full    = (b * win_rate - (1 - win_rate)) / b
            f_applied = max(0.0, f_full * kelly_fraction)
            stake     = bankroll * f_applied

            # V17: Kelly < min_stake → bu simülasyonda daha fazla işlem yok
            if stake < min_stake:
                break

            stake = min(stake, bankroll * 0.4)

            if stake > bankroll:
                hit_ruin = True
                break

            if random.random() <= win_rate:
                bankroll += stake * b
            else:
                bankroll -= stake

            if bankroll >= target:
                reached_target = True
                break

        finals.append(bankroll)
        if reached_target:
            success += 1
        elif hit_ruin:
            ruin += 1

    finals.sort()
    median_final = finals[n_simulations // 2]        if finals else 0.0
    p10_final    = finals[int(n_simulations * 0.10)] if finals else 0.0
    p90_final    = finals[int(n_simulations * 0.90)] if finals else 0.0

    return {
        "success_pct":  round(success / n_simulations * 100, 1),
        "ruin_pct":     round(ruin    / n_simulations * 100, 1),
        "median_final": round(median_final, 2),
        "p10_final":    round(p10_final, 2),
        "p90_final":    round(p90_final, 2),
    }


# ============================================================ dataclasses

@dataclass
class LiveTrade:
    market_id:      str
    token_id:       str
    side:           str
    entry_price:    float
    raw_shares:     float
    net_shares:     float
    stake:          float
    kelly_fraction: float
    win_rate_used:  float
    fees_enabled:   bool
    fee_usd:        float
    ev_usd:         float
    ofi_ratio:      float
    ofi_z:          float
    order_id:       str      = ""
    entry_time:     datetime = field(
        default_factory=lambda: datetime.now(timezone.utc))


class MarketState:
    def __init__(self, mid: str, question: str, end_time: datetime,
                 yes_id: str = "", no_id: str = "",
                 duration_hours: float = 0.083, fees_enabled: bool = True):
        self.mid            = mid
        self.question       = question
        self.end_time       = end_time
        self.yes_id         = yes_id
        self.no_id          = no_id
        self.duration_hours = duration_hours
        self.fees_enabled   = fees_enabled

        self.best_ask:  float = 0.5
        self.best_bid:  float = 0.5
        self.signal:    str   = "BEKLE"
        self.active_trade:    Optional[LiveTrade] = None
        self.has_traded:      bool  = False
        self.last_buy_attempt: float = 0.0
        self.liquidity:       float = 0.0
        self.last_book_ts:    float = 0.0
        self.resolved:        Optional[bool] = None
        self.settled:         bool = False

    @property
    def secs_left(self) -> float:
        return max(0.0, (self.end_time - datetime.now(timezone.utc)).total_seconds())

    @property
    def mins_left(self) -> float:
        return self.secs_left / 60.0

    @property
    def hours_left(self) -> float:
        return self.secs_left / 3600.0

    @property
    def short_name(self) -> str:
        return (self.question[:36] + "…") if len(self.question) > 37 else self.question

    @property
    def fee_label(self) -> str:
        return "[green]FREE[/green]" if not self.fees_enabled else "[yellow]FEE[/yellow]"


# ============================================================ OrderManager (V15'ten korundu)

class OrderManager:
    def __init__(self, cfg: dict, live_mode: bool):
        self.cfg        = cfg
        self.live_mode  = live_mode
        self._client    = None
        self._executor  = ThreadPoolExecutor(max_workers=3)
        self._paper_seq = 0

    def _client_or_raise(self) -> "ClobClient":
        if self._client is None:
            creds       = self.cfg["credentials"]
            funder_addr = creds.get("wallet_address", "")
            self._client = ClobClient(
                host=self.cfg["network"]["clob_url"],
                chain_id=self.cfg["network"]["chain_id"],
                key=creds["private_key"],
                creds=ApiCreds(
                    api_key=creds["api_key"],
                    api_secret=creds["api_secret"],
                    api_passphrase=creds["api_passphrase"],
                ),
                funder=funder_addr if funder_addr else None,
                signature_type=1 if funder_addr else 0,
            )
        return self._client

    async def place_buy(self, token_id: str, price: float, shares: float) -> str:
        if not self.live_mode:
            self._paper_seq += 1
            return f"PAPER-{self._paper_seq:04d}"
        def _do():
            try:
                client = self._client_or_raise()
                price_r, shares_r = _safe_amounts(price, shares * price)
                args   = OrderArgs(price=price_r, size=shares_r,
                                   side=BUY, token_id=token_id)
                signed = client.create_order(args)
                resp   = client.post_order(signed, OrderType.FOK)
                return (resp.get("orderID") or resp.get("order_id")
                        or resp.get("id", ""))
            except Exception as e:
                return f"ERR:{e}"
        return await asyncio.get_running_loop().run_in_executor(self._executor, _do)

    async def place_sell(self, token_id: str, price: float, shares: float) -> str:
        if not self.live_mode:
            return "PAPER_SELL_OK"
        if not CLOB_OK:
            return "ERR:CLOB_IMPORT"
        def _do():
            try:
                client  = self._client_or_raise()
                price_r = _safe_price(price)
                args    = OrderArgs(price=price_r, size=round(shares, 2),
                                    side=SELL, token_id=token_id)
                signed  = client.create_order(args)
                resp    = client.post_order(signed, OrderType.GTC)
                return (resp.get("orderID") or resp.get("order_id")
                        or resp.get("id", ""))
            except Exception as e:
                return f"ERR:{e}"
        return await asyncio.get_running_loop().run_in_executor(self._executor, _do)

    async def ensure_approvals(self) -> str:
        if not self.live_mode:
            return "PAPER"
        def _do():
            import time as _t
            client  = self._client_or_raise()
            results = []
            try:
                from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
                for label, at in [("COLLATERAL",  AssetType.COLLATERAL),
                                   ("CONDITIONAL", AssetType.CONDITIONAL)]:
                    try:
                        resp      = client.get_balance_allowance(
                            params=BalanceAllowanceParams(asset_type=at))
                        allowance = int(resp.get("allowance", "0") or "0") \
                            if isinstance(resp, dict) else 0
                        if allowance == 0:
                            client.update_balance_allowance(
                                params=BalanceAllowanceParams(asset_type=at))
                            _t.sleep(5.0)
                            results.append(f"{label}:SET")
                        else:
                            results.append(f"{label}:OK")
                    except Exception as e:
                        results.append(f"{label}:ERR:{e}")
            except ImportError:
                return "IMPORT_ERR"
            return ",".join(results)
        return await asyncio.get_running_loop().run_in_executor(self._executor, _do)

    async def cancel_all(self) -> None:
        if not self.live_mode:
            return
        def _do():
            try:
                self._client_or_raise().cancel_all_orders()
            except Exception:
                pass
        await asyncio.get_running_loop().run_in_executor(self._executor, _do)


# ============================================================ Ana Bot V17

class KrajekisSniperV17:
    def __init__(self, config: dict):
        self.cfg   = config
        self.risk  = config["risk"]
        self.strat = config["strategy"]
        self.net   = config["network"]

        self.live_mode = (
            False if self.strat.get("paper_only", True)
            else (CLOB_OK and _cfg_has_creds(config))
        )

        self.console   = Console()
        self.order_mgr = OrderManager(config, self.live_mode)
        self.markets:  Dict[str, MarketState] = {}
        self.logs:     deque = deque(maxlen=14)

        self.bankroll: float = float(self.risk.get("bankroll_usd", 30.0))

        self._ofi_buf = OFIBuffer(
            lookback_minutes=int(self.strat.get("ofi_lookback_minutes", 15)),
            snapshot_interval_s=float(self.strat.get("ofi_snapshot_interval_s", 1.0)),
        )

        # BTC fiyat (V17: tazelik izleme)
        self.prices:         Dict[str, float] = {"BTC_BINANCE": 0.0}
        self._btc_price_ts:  float = 0.0   # son güncelleme zamanı
        self._btc_price_src: str   = "—"   # "WS" | "REST"

        self.trades:      int   = 0
        self.wins:        int   = 0
        self.losses:      int   = 0
        self.session_pnl: float = 0.0
        self.daily_pnl:   float = 0.0

        # V17: Circuit breaker
        self._consec_losses:      int   = 0
        self._circuit_open_until: float = 0.0

        # V17: Rate limiter
        self._rate_limiter = RateLimiter()

        self._daily_reset: datetime = (
            datetime.now(timezone.utc)
            .replace(hour=0, minute=0, second=0, microsecond=0)
            + timedelta(days=1)
        )
        self._running:    bool = True
        self._rtds_ok:    bool = False
        self._rtds_tasks: List[asyncio.Task] = []
        self._mc_results: dict = {}

        self._book_lat  = LatencyTracker(maxlen=50)
        self._order_lat = LatencyTracker(maxlen=30)

    # ------------------------------------------------------------------ log

    def _log(self, msg: str, level: str = "INFO") -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        colors = {
            "INFO":    "cyan",
            "TRADE":   "bold green",
            "WARNING": "yellow",
            "ERROR":   "bold red",
            "LIVE":    "bold green",
            "PAPER":   "dim cyan",
            "KELLY":   "magenta",
        }
        c = colors.get(level, "white")
        self.logs.append(f"[{c}][{ts}] {level}[/] {msg}")
        try:
            with open("debug.log", "a", encoding="utf-8") as f:
                f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] [{level}] {msg}\n")
        except Exception:
            pass

    def _check_daily_reset(self) -> None:
        if datetime.now(timezone.utc) >= self._daily_reset:
            self.daily_pnl    = 0.0
            self._daily_reset += timedelta(days=1)
            self._log("Gunluk PnL sifirlandi", "INFO")

    @property
    def _daily_loss_hit(self) -> bool:
        max_loss_pct = float(self.risk.get("max_daily_loss_pct", 0.30))
        max_loss_usd = float(self.risk.get("max_daily_loss_usd",
                                           self.bankroll * max_loss_pct))
        return self.daily_pnl <= -abs(max_loss_usd)

    @property
    def _open_positions(self) -> int:
        return sum(1 for m in self.markets.values() if m.active_trade)

    # ---- Circuit breaker ----

    @property
    def _circuit_open(self) -> bool:
        return time.time() < self._circuit_open_until

    def _circuit_trip(self) -> None:
        pause_min = float(self.risk.get("circuit_breaker_pause_min", 10))
        self._circuit_open_until = time.time() + pause_min * 60
        n = int(self.risk.get("circuit_breaker_losses", 3))
        self._log(
            f"CIRCUIT BREAKER: {n} ard arda zarar — {pause_min:.0f}dk bekleme",
            "WARNING",
        )

    # ---- BTC fiyat tazeliği ----

    @property
    def _btc_age_s(self) -> float:
        if self._btc_price_ts == 0.0:
            return 9999.0
        return time.time() - self._btc_price_ts

    @property
    def _btc_tag_rich(self) -> str:
        age = self._btc_age_s
        if age > 60:
            return "[bold red][STALE][/bold red]"
        if self._btc_price_src == "WS":
            return "[bold green][WS][/bold green]"
        return "[yellow][REST][/yellow]"

    # ------------------------------------------------------------------ Monte Carlo

    def _run_startup_monte_carlo(self) -> None:
        wr       = float(self.strat.get("target_win_rate", 0.60))
        kf       = float(self.risk.get("kelly_fraction",   0.5))
        bankroll = self.bankroll
        target   = float(self.risk.get("target_bankroll",  500.0))
        days     = int(self.risk.get("target_days",        60))
        tpd      = int(self.strat.get("trades_per_day",    4))

        self._mc_results = run_monte_carlo(
            win_rate=wr, kelly_fraction=kf,
            start_bankroll=bankroll, target=target,
            min_stake=float(self.risk.get("min_stake_usd", 2.5)),
            trades_per_day=tpd, days=days, n_simulations=1000,
        )
        self._log(
            f"Monte Carlo ({days}g, {tpd}x/g, WR={wr:.0%}, Kelly×{kf}): "
            f"Hedef={self._mc_results['success_pct']}% | "
            f"Ruin={self._mc_results['ruin_pct']}% | "
            f"Medyan=${self._mc_results['median_final']}",
            "INFO",
        )

    # ------------------------------------------------------------------ BTC REST fallback (V17 YENİ)

    async def _btc_price_rest_task(self) -> None:
        """
        V17: BTC fiyat REST fallback.
        WS fiyatı 30s'den eskiyse veya hiç gelmemişse Binance ticker REST kullanır.
        Her 5 saniyede bir kontrol; taze WS verisi varsa atlar.
        """
        REST_URL       = "https://api.binance.com/api/v3/ticker/price"
        STALE_LIMIT_S  = 30.0

        while self._running:
            await asyncio.sleep(5)

            # WS fiyatı tazeyse REST gerekmez
            if self._btc_age_s < STALE_LIMIT_S and self.prices["BTC_BINANCE"] > 0:
                continue

            try:
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=4)
                ) as sess:
                    async with sess.get(
                        REST_URL, params={"symbol": "BTCUSDT"}
                    ) as r:
                        if r.status == 200:
                            d     = await r.json(content_type=None)
                            price = float(d.get("price", 0) or 0)
                            if price > 0:
                                self.prices["BTC_BINANCE"] = price
                                self._btc_price_ts  = time.time()
                                self._btc_price_src = "REST"
            except Exception:
                pass  # sessizce devam

    # ------------------------------------------------------------------ RTDS: Binance WS

    async def _rtds_binance_ws(self) -> None:
        """
        V17: Binance BTC/USDT WebSocket combined stream.
        + btcusdt@miniTicker    → anlık BTC/USDT fiyatı
        + btcusdt@depth10@100ms → top-10 derinlik (OFI buffer için)

        V17 yeni: port 443 URL eklendi (9443 bloke ağlar için).
        Fallback sırası: 9443 → 443 → data-stream.binance.vision
        """
        _STREAMS = "btcusdt@miniTicker/btcusdt@depth10@100ms"
        _WS_URLS = [
            f"wss://stream.binance.com:9443/stream?streams={_STREAMS}",
            f"wss://stream.binance.com:443/stream?streams={_STREAMS}",    # V17: port 443
            f"wss://data-stream.binance.vision/stream?streams={_STREAMS}",
        ]
        backoff  = 1.0
        _url_idx = 0

        while self._running:
            _WS_URL = _WS_URLS[_url_idx % len(_WS_URLS)]
            try:
                async with aiohttp.ClientSession() as sess:
                    async with sess.ws_connect(
                        _WS_URL, heartbeat=20.0, receive_timeout=30.0,
                    ) as ws:
                        if not self._rtds_ok:
                            self._rtds_ok = True
                            self._log(
                                f"RTDS Binance WS baglandi: "
                                f"{_WS_URL.split('/')[2]}",
                                "INFO",
                            )
                        backoff = 1.0

                        async for msg in ws:
                            if not self._running:
                                break
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                payload = json.loads(msg.data)
                                stream  = payload.get("stream", "")
                                data    = payload.get("data",   {})

                                if "miniTicker" in stream:
                                    price = float(data.get("c", 0) or 0)
                                    if price > 0:
                                        self.prices["BTC_BINANCE"] = price
                                        self._btc_price_ts  = time.time()
                                        self._btc_price_src = "WS"

                                elif "depth" in stream:
                                    bids      = data.get("bids", [])
                                    asks      = data.get("asks", [])
                                    bid_depth = sum(float(b[1]) for b in bids) if bids else 0.0
                                    ask_depth = sum(float(a[1]) for a in asks) if asks else 0.0
                                    self._ofi_buf.update(bid_depth, ask_depth)

                            elif msg.type in (
                                aiohttp.WSMsgType.ERROR,
                                aiohttp.WSMsgType.CLOSE,
                            ):
                                self._log(
                                    f"RTDS WS kapandi (type={msg.type}), yeniden bag.",
                                    "WARNING",
                                )
                                break

            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._rtds_ok = False
                _url_idx += 1
                if self._running:
                    self._log(
                        f"RTDS WS hata: {str(e)[:60]} — {backoff:.0f}s bag.",
                        "WARNING",
                    )
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)

        self._rtds_ok = False

    # ------------------------------------------------------------------ Piyasa Tarayıcı (5dk/15dk BTC)

    async def _update_markets(self, session: aiohttp.ClientSession) -> None:
        """
        Slug tabanlı BTC 5m/15m piyasa keşfi.
        btc-updown-5m-{ts} ve btc-updown-15m-{ts} slugları üretilir;
        geçmiş (-1) ve gelecek (+3) slotlar dahil taranır.
        """
        now_ts  = int(datetime.now(timezone.utc).timestamp())
        now_dt  = datetime.now(timezone.utc)

        base_5m  = (now_ts // 300)  * 300
        base_15m = (now_ts // 900)  * 900

        sluglar: list = []
        for i in range(-1, 4):
            sluglar.append(f"btc-updown-5m-{base_5m  + i * 300}")
            sluglar.append(f"btc-updown-15m-{base_15m + i * 900}")

        new_count = 0
        for slug in sluglar:
            if not self._rate_limiter.is_ok:
                self._log("RATE LIMIT: piyasa taraması duraklatıldı!", "WARNING")
                break
            try:
                self._rate_limiter.tick()
                async with session.get(
                    f"{self.net['gamma_url']}/events",
                    params={"slug": slug},
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as r:
                    if r.status != 200:
                        continue
                    data = await r.json(content_type=None)
            except Exception:
                continue

            for ev in (data if isinstance(data, list) else [data]):
                if not ev or not isinstance(ev, dict):
                    continue
                for m in ev.get("markets", []):
                    if not isinstance(m, dict):
                        continue
                    if m.get("closed") or m.get("active") is False:
                        continue

                    mid = m.get("id")
                    if not mid or mid in self.markets:
                        continue

                    end_str = m.get("endDate", "")
                    try:
                        end_t = datetime.fromisoformat(
                            end_str.replace("Z", "+00:00")
                        ).replace(tzinfo=timezone.utc)
                    except Exception:
                        continue

                    secs_left = (end_t - now_dt).total_seconds()
                    if secs_left < -60:
                        continue

                    try:
                        prices_raw = m.get("outcomePrices", "[]")
                        if isinstance(prices_raw, str):
                            prices_raw = json.loads(prices_raw)
                        yes_price = float(prices_raw[0]) if prices_raw else 0.5
                    except Exception:
                        yes_price = 0.5

                    liquidity    = float(m.get("liquidity", 0) or 0)
                    fees_enabled = bool(m.get("feesEnabled", True))

                    cids   = _parse_clob_token_ids(m.get("clobTokenIds", []))
                    yes_id = cids[0] if len(cids) > 0 else ""
                    no_id  = cids[1] if len(cids) > 1 else ""
                    if not yes_id:
                        continue

                    question    = m.get("question") or ev.get("title") or "BTC Up/Down"
                    tag         = slug + question.lower()
                    horizon_min = 15 if "15m" in tag or "15 min" in tag else 5
                    dur_hours   = max(secs_left, 0) / 3600.0

                    ms_new = MarketState(
                        mid, question, end_t, yes_id, no_id,
                        duration_hours=dur_hours,
                        fees_enabled=fees_enabled,
                    )
                    ms_new.liquidity = liquidity
                    ms_new.best_ask  = yes_price + 0.01
                    ms_new.best_bid  = yes_price - 0.01
                    self.markets[mid] = ms_new
                    new_count += 1
                    fee_tag = "FREE" if not fees_enabled else "FEE"
                    self._log(
                        f"Radar [{horizon_min}m/{fee_tag}] "
                        f"p={yes_price:.2f} liq=${liquidity:.0f}: "
                        f"{question[:40]}",
                        "INFO",
                    )

        if new_count > 0:
            self._log(f"Tarama: {new_count} yeni piyasa eklendi", "INFO")

    # ------------------------------------------------------------------ Orderbook

    async def _fetch_book(self, session: aiohttp.ClientSession,
                           ms: MarketState) -> None:
        """Polymarket CLOB orderbook: best bid/ask güncelleme + RTT ölçümü."""
        if not self._rate_limiter.is_ok:
            return
        try:
            self._rate_limiter.tick()
            _t0 = time.perf_counter()
            async with session.get(
                f"{self.net['clob_url']}/book",
                params={"token_id": ms.yes_id},
                timeout=aiohttp.ClientTimeout(total=3),
            ) as r:
                if r.status == 200:
                    d    = await r.json(content_type=None)
                    _rtt = (time.perf_counter() - _t0) * 1000.0
                    self._book_lat.record(_rtt)
                    bids = sorted(d.get("bids", []),
                                  key=lambda x: float(x.get("price", 0)),
                                  reverse=True)
                    asks = sorted(d.get("asks", []),
                                  key=lambda x: float(x.get("price", 0)))
                    if bids and asks:
                        ms.best_bid     = float(bids[0]["price"])
                        ms.best_ask     = float(asks[0]["price"])
                        ms.last_book_ts = time.time()
        except Exception:
            pass

    # ------------------------------------------------------------------ Settle kontrol

    async def _check_resolved(self, session: aiohttp.ClientSession,
                               ms: MarketState) -> None:
        try:
            self._rate_limiter.tick()
            async with session.get(
                f"{self.net['gamma_url']}/markets/{ms.mid}",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as r:
                if r.status == 200:
                    d = await r.json(content_type=None)
                    if isinstance(d, list):
                        d = d[0] if d else {}
                    if d.get("resolved") or d.get("closed"):
                        resolution = (d.get("resolution") or "").lower()
                        if resolution in ("yes", "1", "true"):
                            ms.resolved = True
                        elif resolution in ("no", "0", "false"):
                            ms.resolved = False
        except Exception:
            pass

    # ------------------------------------------------------------------ Ana Analiz

    async def _analyze(self, ms: MarketState,
                        session: aiohttp.ClientSession) -> None:
        # Günlük limit
        if self._daily_loss_hit:
            ms.signal = "GUNLUK LIMIT"
            return

        # Circuit breaker
        if self._circuit_open:
            remaining = int(self._circuit_open_until - time.time())
            ms.signal = f"CIRCUIT {remaining}s bekle"
            return

        # Tamamlanmış piyasa
        if ms.has_traded and not ms.active_trade:
            ms.signal = "TAMAMLANDI"
            return

        # Aktif pozisyon: fiyat SL kontrolü → let-settle
        if ms.active_trade:
            await self._fetch_book(session, ms)
            t       = ms.active_trade
            yes_mid = (ms.best_bid + ms.best_ask) / 2.0
            sl_thr  = float(self.risk.get("sl_market_price_threshold", 0.04))

            price_crashed = (
                (t.side == "YES" and yes_mid < sl_thr) or
                (t.side == "NO"  and (1.0 - yes_mid) < sl_thr)
            )
            if price_crashed:
                await self._emergency_sl_exit(ms, session, yes_mid)
                return

            ms.signal = (
                f"LET SETTLE {ms.mins_left:.1f}m "
                f"({t.side}@{t.entry_price:.2f} | mkt={yes_mid:.3f})"
            )
            return

        # Max pozisyon
        max_pos = int(self.risk.get("max_open_positions", 2))
        if self._open_positions >= max_pos:
            ms.signal = "POS DOLU"
            return

        # V17: Bankroll < 3×min_stake → işlem yapma (overbetting önleme)
        min_stake = float(self.risk.get("min_stake_usd", 2.5))
        if self.bankroll < min_stake * 3:
            ms.signal = f"KASA KUCUK ${self.bankroll:.2f}<${min_stake*3:.2f}"
            return

        # Orderbook güncelle
        await self._fetch_book(session, ms)
        entry = ms.best_ask
        if entry <= 0.01 or entry >= 0.99:
            ms.signal = "FIYAT HATALI"
            return

        # Zamanlama penceresi: 5dk/15dk piyasalar için %20–%60
        total_secs = ms.duration_hours * 3600.0
        secs_left  = ms.secs_left
        if total_secs < 1800:  # 30 dakikadan kısa piyasa
            elapsed_pct = (total_secs - secs_left) / total_secs if total_secs > 0 else 0
            if elapsed_pct < 0.20:
                ms.signal = f"TOO EARLY {elapsed_pct:.0%}<20%"
                return
            if elapsed_pct > 0.60:
                ms.signal = f"TOO LATE {elapsed_pct:.0%}>60%"
                return

        # Stale veri kontrolü
        stale_limit_ms = float(self.strat.get("stale_data_limit_ms", 5000))
        ofi_age_ms     = self._ofi_buf.data_age_ms
        book_age_ms    = (time.time() - ms.last_book_ts) * 1000.0

        if ofi_age_ms > stale_limit_ms:
            ms.signal = f"OFI BAYAT {ofi_age_ms:.0f}ms"
            return
        if ms.last_book_ts > 0 and book_age_ms > stale_limit_ms:
            ms.signal = f"BOOK BAYAT {book_age_ms:.0f}ms"
            return

        # OFI sinyali
        ofi_threshold = float(self.strat.get("ofi_ratio_threshold", 3.0))
        ofi_z_thresh  = float(self.strat.get("ofi_z_threshold",     3.0))
        ofi_signal    = self._ofi_buf.signal(ofi_threshold, ofi_z_thresh)
        ofi_ratio     = self._ofi_buf.ratio
        ofi_z         = self._ofi_buf.z_score

        if not ofi_signal:
            ms.signal = (
                f"OFI ZAYIF r={ofi_ratio:.1f}x z={ofi_z:.1f} "
                f"n={self._ofi_buf.sample_count}"
            )
            return

        # Fiyat bandı uyumu
        pb_lo_min = float(self.strat.get("price_band_low_min",  0.25))
        pb_lo_max = float(self.strat.get("price_band_low_max",  0.42))
        pb_hi_min = float(self.strat.get("price_band_high_min", 0.58))
        pb_hi_max = float(self.strat.get("price_band_high_max", 0.75))

        yes_price = (ms.best_bid + ms.best_ask) / 2.0
        if ofi_signal == "YES" and not (pb_lo_min <= yes_price <= pb_lo_max):
            ms.signal = f"BANT UYUMSUZ YES p={yes_price:.2f}"
            return
        if ofi_signal == "NO" and not (pb_hi_min <= yes_price <= pb_hi_max):
            ms.signal = f"BANT UYUMSUZ NO p={yes_price:.2f}"
            return

        # Kelly stake hesabı
        win_rate    = float(self.strat.get("target_win_rate", 0.60))
        kelly_frac  = float(self.risk.get("kelly_fraction",   0.5))
        max_stk_pct = float(self.risk.get("max_stake_pct",    0.4))

        entry_price = ms.best_ask if ofi_signal == "YES" else (1.0 - ms.best_bid)
        entry_price = _safe_price(entry_price)

        # Slippage guard
        slip_tol    = float(self.strat.get("fok_slippage_tolerance", 0.02))
        limit_price = round(entry_price + slip_tol, 4)
        band_ceil   = pb_lo_max if ofi_signal == "YES" else (1.0 - pb_hi_min)
        if limit_price > band_ceil:
            ms.signal = (
                f"SLIP GUARD {entry_price:.3f}+{slip_tol:.2f}"
                f"={limit_price:.3f}>{band_ceil:.2f}"
            )
            return
        entry_price = limit_price

        stake = _calc_kelly_stake(
            bankroll=self.bankroll,
            win_rate=win_rate,
            entry_price=entry_price,
            kelly_fraction=kelly_frac,
            min_stake=min_stake,
            max_stake_pct=max_stk_pct,
        )
        if stake <= 0:
            ms.signal = "KELLY NEG EV"
            return

        # V17: Overbetting fix — Kelly hesabı min_stake altındaysa işlem yapma
        if stake < min_stake:
            ms.signal = f"KELLY<MIN ${stake:.2f}<${min_stake:.2f}"
            return

        if stake > self.bankroll:
            ms.signal = "BAKIYE YETERSIZ"
            return

        # EV hesabı
        fee_rate  = float(self.strat.get("fee_rate_bps", 0.25))
        fee_exp   = int(self.strat.get("fee_exponent",   2))
        _, raw_shares = _safe_amounts(entry_price, stake)
        fee_usd    = _calc_fee_usd(entry_price, raw_shares, fee_rate, fee_exp, ms.fees_enabled)
        net_shares = raw_shares - _calc_fee_shares(
            entry_price, raw_shares, fee_rate, fee_exp, ms.fees_enabled)
        ev = _calc_ev_settle(entry_price, raw_shares, win_rate,
                             fee_rate, fee_exp, ms.fees_enabled)

        min_ev = float(self.strat.get("min_ev_threshold", 0.05))
        if ev < min_ev:
            ms.signal = f"EV DUSUK ${ev:.3f}<${min_ev:.2f}"
            return

        # FOK cooldown
        now_ts = datetime.now(timezone.utc).timestamp()
        fok_cd = float(self.strat.get("fok_cooldown", 60))
        if now_ts - ms.last_buy_attempt < fok_cd:
            return

        # V17: Latency gate (500ms varsayılan)
        max_lat = float(self.strat.get("max_fok_latency_p90_ms", 500))
        if self._book_lat.count >= 5 and self._book_lat.p90 > max_lat:
            ms.signal = (
                f"LAT GATE p90={self._book_lat.p90:.0f}ms>{max_lat:.0f}ms"
            )
            return

        # Emir gönder
        trade_side = ofi_signal
        token_id   = ms.yes_id if ofi_signal == "YES" else ms.no_id
        ms.last_buy_attempt = now_ts

        _order_t0 = time.perf_counter()
        self._rate_limiter.tick()
        oid = await self.order_mgr.place_buy(token_id, entry_price, raw_shares)
        _order_rtt = (time.perf_counter() - _order_t0) * 1000.0
        self._order_lat.record(_order_rtt)

        if not oid or oid.startswith("ERR:"):
            self._log(f"FOK red ({int(fok_cd)}s bekleme): {oid}", "WARNING")
            return

        ms.active_trade = LiveTrade(
            market_id=ms.mid, token_id=token_id,
            side=trade_side, entry_price=entry_price,
            raw_shares=raw_shares, net_shares=net_shares,
            stake=stake, kelly_fraction=kelly_frac,
            win_rate_used=win_rate, fees_enabled=ms.fees_enabled,
            fee_usd=fee_usd, ev_usd=ev,
            ofi_ratio=ofi_ratio, ofi_z=ofi_z,
            order_id=oid,
        )
        fee_tag = "FREE" if not ms.fees_enabled else f"fee=${fee_usd:.3f}"
        self._log(
            f"{'LIVE' if self.live_mode else 'PAPER'} SNIPE ({trade_side}) "
            f"entry={entry_price:.3f} stake=${stake:.2f} "
            f"EV=${ev:.3f} OFI={ofi_ratio:.1f}x z={ofi_z:.1f} "
            f"Kelly×{kelly_frac} {fee_tag} RTT={_order_rtt:.0f}ms",
            "LIVE" if self.live_mode else "PAPER",
        )
        ms.signal = (
            f"{trade_side}@{entry_price:.2f} EV=${ev:.3f} OFI={ofi_ratio:.1f}x"
        )

    # ------------------------------------------------------------------ Emergency SL Exit

    async def _emergency_sl_exit(
        self, ms: MarketState, session: aiohttp.ClientSession, yes_mid: float
    ) -> None:
        t = ms.active_trade
        if not t:
            return

        sell_price = _safe_price(ms.best_bid if t.side == "YES"
                                 else (1.0 - ms.best_ask))
        sell_price = max(sell_price, 0.02)

        if self.live_mode:
            self._rate_limiter.tick()
            oid = await self.order_mgr.place_sell(
                token_id=ms.yes_id if t.side == "YES" else ms.no_id,
                price=sell_price, shares=t.net_shares,
            )
            sell_ok = bool(oid and not oid.startswith("ERR:"))
        else:
            sell_ok = True

        recovered = t.net_shares * sell_price if sell_ok else 0.0
        pnl       = round(recovered - t.stake, 4)
        reason    = "SL_EXIT" if sell_ok else "SL_EXIT_FAIL"

        self.losses       += 1
        self.bankroll      = max(0.0, self.bankroll + pnl)
        self.session_pnl  += pnl
        self.daily_pnl    += pnl
        self.trades       += 1

        # Circuit breaker güncelle
        self._consec_losses += 1
        cb_n = int(self.risk.get("circuit_breaker_losses", 3))
        if self._consec_losses >= cb_n:
            self._circuit_trip()

        self._record(ms, pnl, reason)
        self._log(
            f"SL CIKIS ({t.side}) mkt={yes_mid:.3f} < "
            f"esik={self.risk.get('sl_market_price_threshold', 0.04)} | "
            f"kurtarilan=${recovered:.3f} PnL=${pnl:+.3f} "
            f"Bankroll=${self.bankroll:.2f}",
            "TRADE",
        )
        ms.active_trade = None
        ms.has_traded   = True
        ms.settled      = True
        ms.signal       = f"SL {pnl:+.2f}"

    # ------------------------------------------------------------------ Settle

    async def _settle(self, mid: str,
                       session: aiohttp.ClientSession) -> None:
        ms = self.markets.get(mid)
        if not ms or ms.settled:
            return

        if ms.active_trade:
            if ms.resolved is None:
                await self._check_resolved(session, ms)

            if ms.resolved is None:
                ms.signal = "SETTLE BEKLENIYOR"
                return

            t   = ms.active_trade
            won = (ms.resolved and t.side == "YES") or \
                  (not ms.resolved and t.side == "NO")

            if won:
                pnl    = round(t.net_shares * 1.0 - t.raw_shares * t.entry_price, 4)
                reason = "SETTL_WIN"
                self.wins      += 1
                self._consec_losses = 0  # Circuit breaker sıfırla
            else:
                pnl    = round(-(t.raw_shares * t.entry_price), 4)
                reason = "SETTL_LOSS"
                self.losses    += 1
                self._consec_losses += 1
                cb_n = int(self.risk.get("circuit_breaker_losses", 3))
                if self._consec_losses >= cb_n:
                    self._circuit_trip()

            self.bankroll     = max(0.0, self.bankroll + pnl)
            self.session_pnl += pnl
            self.daily_pnl   += pnl
            self.trades      += 1

            self._record(ms, pnl, reason)
            self._log(
                f"SETTLE {'WIN' if won else 'LOSS'} | "
                f"{ms.short_name[:30]} | "
                f"PnL: ${pnl:+.3f} | Bankroll: ${self.bankroll:.2f}",
                "TRADE",
            )
            ms.active_trade = None
            ms.has_traded   = True
            ms.settled      = True
            ms.signal       = f"{'WIN' if won else 'LOSS'} ${pnl:+.2f}"
        else:
            ms.settled = True

    # ------------------------------------------------------------------ Kayıt

    def _record(self, ms: MarketState, pnl: float, rtype: str) -> None:
        t = ms.active_trade
        if not t:
            return

        wr_actual = (self.wins / self.trades * 100) if self.trades else 0.0
        record = {
            "ts":              datetime.now(timezone.utc).isoformat(),
            "mid":             ms.mid,
            "question":        ms.question[:60],
            "duration_h":      round(ms.duration_hours, 4),
            "fees_enabled":    ms.fees_enabled,
            "side":            t.side,
            "entry":           t.entry_price,
            "raw_shares":      t.raw_shares,
            "net_shares":      round(t.net_shares, 4),
            "stake":           t.stake,
            "kelly_fraction":  t.kelly_fraction,
            "win_rate_used":   t.win_rate_used,
            "fee_usd":         round(t.fee_usd, 4),
            "ev_usd":          round(t.ev_usd, 4),
            "ofi_ratio":       round(t.ofi_ratio, 3),
            "ofi_z":           round(t.ofi_z, 3),
            "pnl":             pnl,
            "result":          rtype,
            "bankroll_after":  round(self.bankroll, 4),
            "wr_session_pct":  round(wr_actual, 2),
            "consec_losses":   self._consec_losses,
            "btc_binance":     self.prices.get("BTC_BINANCE", 0.0),
            "btc_src":         self._btc_price_src,
            "live":            self.live_mode,
        }
        try:
            mem = self.cfg.get("memory_file", "trades_v17.jsonl")
            with open(mem, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        except Exception:
            pass

    # ------------------------------------------------------------------ Render

    def _render(self) -> Layout:
        lay = Layout()
        lay.split_column(
            Layout(name="h", size=3),
            Layout(name="b", ratio=1),
            Layout(name="l", size=14),
        )
        lay["b"].split_row(Layout(name="mt", ratio=4), Layout(name="s", size=46))

        bn       = self.prices.get("BTC_BINANCE", 0.0)
        wr       = (self.wins / self.trades * 100) if self.trades else 0.0
        rtds_tag = "[bold green]OK[/]" if self._rtds_ok else "[red]ERR[/]"
        ofi_r    = self._ofi_buf.ratio
        ofi_z    = self._ofi_buf.z_score
        ofi_sig  = self._ofi_buf.signal(
            float(self.strat.get("ofi_ratio_threshold", 3.0)),
            float(self.strat.get("ofi_z_threshold",     3.0)),
        )
        ofi_col  = "green" if ofi_sig == "YES" else ("red" if ofi_sig == "NO" else "yellow")

        mc     = self._mc_results
        mc_str = (
            f"{mc.get('success_pct',0)}% hedef/{mc.get('ruin_pct',0)}% ruin"
            if mc else "—"
        )

        book_p90 = self._book_lat.p90
        max_lat  = float(self.strat.get("max_fok_latency_p90_ms", 500))
        if self._book_lat.count == 0:
            lat_str = "[dim]lat=ölçülüyor[/dim]"
        elif book_p90 < max_lat * 0.5:
            lat_str = f"[bold green]p90={book_p90:.0f}ms[/bold green]"
        elif book_p90 < max_lat:
            lat_str = f"[yellow]p90={book_p90:.0f}ms[/yellow]"
        else:
            lat_str = f"[bold red]p90={book_p90:.0f}ms GATE![/bold red]"

        circuit_str = (
            f"[bold red]CB:{int(self._circuit_open_until - time.time())}s[/bold red]"
            if self._circuit_open else
            f"[green]CB:OK({self._consec_losses}L)[/green]"
        )

        hdr = (
            f"[bold white]KRAJEKIS V17.0 5DK/15DK[/bold white] "
            f"{'[bold red]CANLI[/bold red]' if self.live_mode else '[dim cyan]PAPER[/dim cyan]'} | "
            f"RTDS:{rtds_tag} BTC:{self._btc_tag_rich}[cyan]${bn:,.0f}[/cyan] | "
            f"OFI:[{ofi_col}]{ofi_r:.1f}x z={ofi_z:.1f}→{ofi_sig or 'BEKLE'}[/] | "
            f"{lat_str} {circuit_str} | "
            f"Bankroll:[bold yellow]${self.bankroll:.2f}[/bold yellow]"
        )
        lay["h"].update(Panel(Text.from_markup(hdr), border_style="white"))

        # Piyasa tablosu
        tbl = Table(box=box.SIMPLE_HEAVY, expand=True, show_header=True)
        tbl.add_column("Süre",     width=9)
        tbl.add_column("Piyasa",   ratio=1)
        tbl.add_column("Fee",      width=6)
        tbl.add_column("YES p",    width=7)
        tbl.add_column("Liq $",    width=8)
        tbl.add_column("Sinyal",   width=27)
        tbl.add_column("Pozisyon", width=30)

        active_markets = sorted(
            [m for m in self.markets.values() if not m.settled],
            key=lambda m: m.secs_left,
        )
        for ms in active_markets[:12]:
            h_left = ms.hours_left
            if h_left < 0.017:
                time_str = f"{int(ms.secs_left)}s"
            elif h_left < 1:
                _m = int(ms.mins_left)
                _s = int(ms.secs_left % 60)
                time_str = f"{_m}m{_s:02d}s"
            else:
                time_str = f"{h_left:.1f}h"

            yes_p     = (ms.best_bid + ms.best_ask) / 2.0
            liq_str   = f"${ms.liquidity:.0f}"
            pos_str   = ""
            row_style = ""

            if ms.active_trade:
                t    = ms.active_trade
                gain = (yes_p - t.entry_price if t.side == "YES"
                        else (1 - yes_p) - t.entry_price)
                pos_str   = f"{t.side}@{t.entry_price:.2f}({gain:+.3f}) EV=${t.ev_usd:.2f}"
                row_style = "green" if gain >= 0 else "red"
            elif ms.has_traded:
                pos_str   = "[dim]KAPANDI[/dim]"
                row_style = "dim"

            tbl.add_row(
                time_str, ms.short_name, ms.fee_label,
                f"{yes_p:.3f}", liq_str, ms.signal, pos_str,
                style=row_style,
            )

        lay["mt"].update(Panel(
            tbl,
            title=(
                f"V17 BTC 5dk/15dk Radar "
                f"({sum(1 for m in self.markets.values() if not m.settled)} aktif"
                f" | {self._open_positions} açık pozisyon)"
            ),
            border_style="cyan",
        ))

        # Sağ panel: istatistik
        kelly_frac = float(self.risk.get("kelly_fraction", 0.5))
        wr_target  = float(self.strat.get("target_win_rate", 0.60))
        min_stake  = float(self.risk.get("min_stake_usd", 2.5))

        stat = (
            f"[bold cyan]OFI BUFFER (15dk kümülatif)[/bold cyan]\n"
            f"  Oran:   [{ofi_col}]{ofi_r:.2f}x[/] (eşik:{float(self.strat.get('ofi_ratio_threshold',3.0)):.1f}x)\n"
            f"  Z-skor: [{ofi_col}]{ofi_z:+.2f}[/] (±{float(self.strat.get('ofi_z_threshold',3.0)):.1f})\n"
            f"  Örnek:  {self._ofi_buf.sample_count} snap\n"
            f"  Yaş:    {self._ofi_buf.data_age_ms:.0f}ms\n"
            f"  Sinyal: [{ofi_col}]{ofi_sig or 'BEKLE'}[/]\n\n"
            f"[bold cyan]KELLY + BANKROLL[/bold cyan]\n"
            f"  Bankroll:  [bold yellow]${self.bankroll:.2f}[/bold yellow]\n"
            f"  Kelly:     ×{kelly_frac} (Half-Kelly)\n"
            f"  Min stake: ${min_stake:.2f}\n"
            f"  WR hedef:  {wr_target:.0%}\n"
            f"  WR gerç:   [{'green' if wr>=wr_target*100 else 'red'}]{wr:.1f}%[/]\n\n"
            f"[bold cyan]MONTE CARLO[/bold cyan]\n"
            f"  {mc_str}\n"
            f"  P10/P90: ${mc.get('p10_final',0):.2f}/${mc.get('p90_final',0):.2f}\n\n"
            f"[bold cyan]CIRCUIT BREAKER[/bold cyan]\n"
            f"  Ard arda zarar: {self._consec_losses}/{int(self.risk.get('circuit_breaker_losses',3))}\n"
            f"  Durum: {circuit_str}\n\n"
            f"[bold cyan]RATE LİMİTER[/bold cyan]\n"
            f"  {self._rate_limiter.status_rich}\n\n"
            f"[bold cyan]SEANS[/bold cyan]\n"
            f"  İşlem:  {self.trades} ({self.wins}W/{self.losses}L)\n"
            f"  Seans:  [{'green' if self.session_pnl>=0 else 'red'}]${self.session_pnl:+.3f}[/]\n"
            f"  Günlük: [{'green' if self.daily_pnl>=0 else 'red'}]${self.daily_pnl:+.3f}[/]"
        )
        lay["s"].update(Panel(
            Text.from_markup(stat),
            title="V17 İstatistik",
            border_style="yellow",
        ))
        lay["l"].update(Panel(
            Text.from_markup("\n".join(list(self.logs))),
            title="Log [V17.0 | 5DK/15DK PAPER | CB+Kelly Fix+RateLimit+REST Fallback]",
            border_style="cyan",
        ))
        return lay

    # ------------------------------------------------------------------ Ana döngü

    async def main_run(self) -> None:
        self._run_startup_monte_carlo()

        connector = aiohttp.TCPConnector(
            limit=20,
            resolver=aiohttp.ThreadedResolver(),
            family=socket.AF_INET,
        )
        async with aiohttp.ClientSession(connector=connector) as session:
            self._rtds_tasks = [
                asyncio.create_task(self._rtds_binance_ws(),    name="rtds_binance"),
                asyncio.create_task(self._btc_price_rest_task(), name="btc_rest"),
            ]
            try:
                if self.live_mode:
                    appr = await self.order_mgr.ensure_approvals()
                    self._log(f"Live Onay: {appr}", "INFO")

                self._log(
                    f"V17.0 5DK/15DK Basladi "
                    f"({'CANLI' if self.live_mode else 'PAPER'}) | "
                    f"Bankroll=${self.bankroll:.2f} "
                    f"Kelly×{self.risk.get('kelly_fraction',0.5)} "
                    f"WR={float(self.strat.get('target_win_rate',0.60)):.0%} | "
                    f"CB:{self.risk.get('circuit_breaker_losses',3)}L/"
                    f"{self.risk.get('circuit_breaker_pause_min',10)}dk",
                    "PAPER" if not self.live_mode else "LIVE",
                )

                scan_interval = int(self.strat.get("scan_interval_secs", 10))

                with Live(self._render(), refresh_per_second=2, screen=True) as live:
                    cycle = 0
                    while self._running:
                        self._check_daily_reset()

                        if cycle % scan_interval == 0:
                            await self._update_markets(session)

                        for ms in list(self.markets.values()):
                            if ms.settled:
                                continue
                            if ms.secs_left > 0:
                                await self._analyze(ms, session)
                            else:
                                await self._settle(ms.mid, session)

                        live.update(self._render())
                        await asyncio.sleep(1)
                        cycle += 1

            finally:
                self._running = False
                for task in self._rtds_tasks:
                    task.cancel()
                await asyncio.gather(*self._rtds_tasks, return_exceptions=True)

        if self.live_mode:
            await self.order_mgr.cancel_all()


# ============================================================ entry point

if __name__ == "__main__":
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else "config_v17.json"
    try:
        cfg = load_config(cfg_path)
    except FileNotFoundError as e:
        print(f"Hata: {e}")
        sys.exit(1)

    bot = KrajekisSniperV17(cfg)
    if bot.live_mode:
        print(
            f"\nKRAJEKIS V17.0 CANLI MOD\n"
            f"Bankroll: ${bot.bankroll:.2f}\n"
            f"Kelly:    x{bot.risk.get('kelly_fraction', 0.5)}\n"
            f"WR hedef: {float(bot.strat.get('target_win_rate',0.60)):.0%}\n"
        )
        ans = input("CANLI PARA modunda. Devam? [evet/hayir]: ").strip().lower()
        if ans not in ("evet", "e", "yes", "y"):
            sys.exit(0)
    try:
        asyncio.run(bot.main_run())
    except KeyboardInterrupt:
        pass
