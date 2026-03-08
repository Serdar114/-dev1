#!/usr/bin/env python3
"""
Polymarket Krajekis Auto-Sniper V16.0 (KELLY + LONG-HORIZON EDITION)
====================================================================
Araştırma Raporuna Dayalı Radikal Mimari Revizyon (Mart 2026):

TEMEL DEĞİŞİKLİKLER (V15'ten V16'ya):

  PIYASA SEÇİMİ (V16 YENİ):
  + 5dk/15dk BTC piyasası KAPATILDI (negatif EV, kanıtlandı)
  + Gamma API geniş tarama: feesEnabled=false öncelik, kripto 1H-7gün
  + Süre filtresi: 1 saat - 7 gün (yapılandırılabilir)
  + Fiyat bandı: 0.30-0.45 VEYA 0.55-0.70 (asimetrik sinyal bölgesi)
  + Likidite filtresi: min $1000 açık faiz
  + feesEnabled durumu piyasa bazında takip edilir (EV'ye dahil)

  OFI KALİBRASYON (V16 YENİ — Araştırma #2 düzeltmesi):
  + OFIBuffer: 15 dakikalık kümülatif deque-tabanlı OFI
    Eski anlık top-5 snapshot → Yeni: her 10 saniyede bir delta birikimi
  + Eşik: 3:1 hacim oranı (alıcı/satıcı) — eski ±0.55 iptal
  + Formül: bid_cumulative / ask_cumulative > 3.0 → YES sinyali
             ask_cumulative / bid_cumulative > 3.0 → NO sinyali
  + Z-skor doğrulama: |z| > 2.0 (son 15 nokta rolling std)

  STAKE YÖNETİMİ (V16 YENİ — Kelly Kriteri):
  + _calc_kelly_stake(): dinamik Kelly hesabı
    f* = (b×p - q) / b   (b=net_oran, p=win_rate, q=1-p)
  + kelly_fraction: 0.50 (Half-Kelly, araştırma önerisi)
  + Bankroll dinamik takip: her işlem sonrası güncellenir
  + Hard limitler: min $2.50, max bankroll×%50

  FEE MODELİ (V16 YENİ — Araştırma doğrulaması):
  + Quadratic fee: fee = shares × feeRate × (p×(1-p))^exponent
    Kripto piyasaları: feeRate=0.25, exponent=2
    feesEnabled=false piyasaları: fee = $0.00
  + Tüm EV hesaplamalarına fee dahil

  EXIT STRATEJİSİ (V16 YENİ):
  + 1H+ piyasalar: LET SETTLE ONLY — erken çıkış YASAK
    (likidite düşük, spread yüksek, erken çıkış değer kaybettirir)
  + Settlement: Gamma API'den resolved sonuç sorgulanır
  + TP/SL mantığı 1H+ için tamamen kaldırıldı

  MONTE CARLO (V16 YENİ):
  + Başlangıçta risk simülasyonu
  + 1000 yol × N işlem → başarı/ruin olasılıkları

  DEĞIŞMEYEN PARÇALAR:
  + OrderManager (CLOB auth, FOK/GTC, cancel/approve)
  + RTDS Binance REST polling (fiyat takibi)
  + Rich UI terminali
  + debug.log + trades JSONL kaydı
"""
import sys
import asyncio
import socket
import aiohttp
import json
import math
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

def load_config(path: str = "config_v16.json") -> dict:
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


# ============================================================ V16: Fee formülü (Araştırma #1)

def _calc_fee_shares_v16(price: float, raw_shares: float,
                          fee_rate: float = 0.25, exponent: int = 2,
                          fees_enabled: bool = True) -> float:
    """
    Polymarket dinamik taker ücreti (Mart 2026 güncellemesi):
    fee = shares × feeRate × (p × (1-p))^exponent
    feesEnabled=false → fee = 0
    Kripto piyasaları: feeRate=0.25, exponent=2 → max %1.56 @ p=0.50
    """
    if not fees_enabled:
        return 0.0
    p = max(0.01, min(0.99, price))
    return raw_shares * fee_rate * (p * (1.0 - p)) ** exponent


def _calc_fee_usd_v16(price: float, raw_shares: float,
                       fee_rate: float = 0.25, exponent: int = 2,
                       fees_enabled: bool = True) -> float:
    """Fee USD = fee_shares × 1.0 (settlement'ta 1.0)."""
    return _calc_fee_shares_v16(price, raw_shares, fee_rate, exponent, fees_enabled)


def _calc_ev_settle_v16(entry: float, raw_shares: float, p_win: float,
                         fee_rate: float = 0.25, exponent: int = 2,
                         fees_enabled: bool = True) -> float:
    """
    Settlement-bazlı beklenen değer (binary payout).
    EV = P_win × net_shares - raw_shares × entry
    Pozitif → beklenen kâr var.
    """
    net_shares = raw_shares - _calc_fee_shares_v16(
        entry, raw_shares, fee_rate, exponent, fees_enabled)
    return p_win * net_shares - raw_shares * entry


# ============================================================ V16: Kelly Kriteri

def _calc_kelly_stake(bankroll: float, win_rate: float, entry_price: float,
                       kelly_fraction: float = 0.5,
                       min_stake: float = 2.5,
                       max_stake_pct: float = 0.5) -> float:
    """
    Kelly Kriteri ile optimal stake hesabı.

    Formül: f* = (b×p - q) / b
      b = net kazanç oranı = (1 - entry) / entry
      p = win_rate
      q = 1 - win_rate

    Half-Kelly (fraction=0.5) varsayılan — araştırma önerisi.
    İflas riskini azaltır, büyümeyi sürdürür.
    """
    entry = max(0.01, min(0.99, entry_price))
    b = (1.0 - entry) / entry   # net odds
    p = max(0.01, min(0.99, win_rate))
    q = 1.0 - p

    f_full = (b * p - q) / b
    if f_full <= 0:
        return 0.0  # negatif EV — işlem yapma

    f_applied = f_full * kelly_fraction
    stake = bankroll * f_applied

    # Hard limitler
    max_stake = bankroll * max_stake_pct
    stake = min(stake, max_stake)
    stake = max(stake, min_stake)

    # Bakiye yeterliliği
    if stake > bankroll:
        return 0.0
    return round(stake, 2)


# ============================================================ V16: OFI Buffer (15dk kümülatif)

class OFIBuffer:
    """
    15 dakikalık kümülatif Order Flow Imbalance (OFI) takipçisi.

    Araştırma kalibrasyonu:
    - Lookback: 15 dakika (5dk piyasalar için ±0.55 yetersiz)
    - Eşik: 3:1 oran (bid_cumulative / ask_cumulative)
    - Her 10 saniyede bir Binance depth snapshot alınır
    - Delta birikimi: Δbid = bid_depth[t] - bid_depth[t-1]
    """
    def __init__(self, lookback_minutes: int = 15, snapshot_interval_s: float = 10.0):
        self.lookback_secs = lookback_minutes * 60
        self.snapshot_interval_s = snapshot_interval_s
        # (timestamp, bid_delta, ask_delta)
        self._deltas: deque = deque()
        self._prev_bid: float = 0.0
        self._prev_ask: float = 0.0
        self._last_snapshot: float = 0.0

    def update(self, bid_depth: float, ask_depth: float) -> None:
        """Yeni depth snapshot ile OFI buffer'ı güncelle."""
        now = time.time()
        if now - self._last_snapshot < self.snapshot_interval_s:
            return
        self._last_snapshot = now

        # Delta hesaplama
        bid_delta = bid_depth - self._prev_bid if self._prev_bid > 0 else 0.0
        ask_delta = ask_depth - self._prev_ask if self._prev_ask > 0 else 0.0
        self._prev_bid = bid_depth
        self._prev_ask = ask_depth

        self._deltas.append((now, bid_delta, ask_delta))

        # Eski verileri temizle
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
        """Bid/Ask oranı. >1 = alıcı baskısı, <1 = satıcı baskısı."""
        ask = self.cumulative_ask
        if ask <= 0:
            return 1.0
        return self.cumulative_bid / ask

    @property
    def z_score(self) -> float:
        """Son N deltanın Z-skoru — 3:1 eşiği doğrulama için."""
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

    def signal(self, ratio_threshold: float = 3.0, z_threshold: float = 2.0) -> Optional[str]:
        """
        OFI yön sinyali.
        YES: bid/ask oranı > threshold VE z_score > z_threshold
        NO:  ask/bid oranı > threshold VE z_score < -z_threshold
        None: yetersiz sinyal
        """
        if self.sample_count < 5:
            return None  # Yeterli veri yok

        r   = self.ratio
        z   = self.z_score
        inv = (1.0 / r) if r > 0 else 0.0

        if r > ratio_threshold and z > z_threshold:
            return "YES"
        if inv > ratio_threshold and z < -z_threshold:
            return "NO"
        return None


# ============================================================ V16: Monte Carlo

def run_monte_carlo(
    win_rate: float,
    kelly_fraction: float,
    start_bankroll: float,
    target: float,
    min_stake: float,
    trades_per_day: int,
    days: int,
    n_simulations: int = 1000,
    avg_entry: float = 0.40,
) -> dict:
    """
    Monte Carlo simülasyonu — $30→$900 başarı/ruin olasılıkları.

    Araştırma raporu parametreleri:
    - Senaryo A: WR=0.60, kelly=0.33
    - Senaryo B: WR=0.65, kelly=0.46 (Half → 0.23)

    Not: Polymarket min $2.50 limiti ile overbetting riski dahil.
    """
    total_trades = trades_per_day * days
    b = (1.0 - avg_entry) / avg_entry   # net odds

    success  = 0
    ruin     = 0
    finals   = []

    for _ in range(n_simulations):
        bankroll = start_bankroll
        reached_target = False
        hit_ruin       = False

        for _ in range(total_trades):
            if bankroll < min_stake:
                hit_ruin = True
                break

            # Kelly stake
            f_full    = (b * win_rate - (1 - win_rate)) / b
            f_applied = max(0.0, f_full * kelly_fraction)
            stake     = bankroll * f_applied

            # Polymarket hard limiti — overbetting riski
            stake = max(stake, min_stake)
            stake = min(stake, bankroll * 0.5)

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
    median_final = finals[n_simulations // 2] if finals else 0.0
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
    side:           str            # "YES" | "NO"
    entry_price:    float
    raw_shares:     float
    net_shares:     float
    stake:          float
    kelly_fraction: float          # V16: uygulanan Kelly oranı
    win_rate_used:  float          # V16: hesapta kullanılan win rate
    fees_enabled:   bool           # V16: piyasanın ücret durumu
    fee_usd:        float          # V16: ödenen ücret USD
    ev_usd:         float          # V16: entry anındaki EV
    ofi_ratio:      float          # V16: entry anındaki OFI oranı (bid/ask)
    ofi_z:          float          # V16: entry anındaki Z-skor
    order_id:       str            = ""
    entry_time:     datetime       = field(
        default_factory=lambda: datetime.now(timezone.utc))


class MarketState:
    def __init__(self, mid: str, question: str, end_time: datetime,
                 yes_id: str = "", no_id: str = "",
                 duration_hours: float = 1.0, fees_enabled: bool = True):
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
        self.active_trade: Optional[LiveTrade] = None
        self.has_traded:   bool  = False
        self.last_buy_attempt: float = 0.0
        self.liquidity:    float = 0.0   # V16: USD likidite

        # V16: resolved sonuç (settle sonrası)
        self.resolved:     Optional[bool] = None  # True=YES kazandı, False=NO kazandı
        self.settled:      bool = False

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

    async def get_order_status(self, order_id: str) -> str:
        if not self.live_mode:
            return "MATCHED"
        if not order_id or order_id.startswith("ERR"):
            return f"ERR:{order_id}"
        def _do():
            try:
                client = self._client_or_raise()
                resp   = client.get_order(order_id)
                status = (resp.get("status") or
                          resp.get("orderStatus") or "UNKNOWN").upper()
                if status in ("MATCHED", "FILLED", "COMPLETE"):
                    return "MATCHED"
                if status in ("LIVE", "OPEN", "ACTIVE"):
                    return "OPEN"
                if status in ("CANCELLED", "CANCELED"):
                    return "CANCELLED"
                return status
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


# ============================================================ V16: Ana Bot

class KrajekisSniperV16:
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

        # V16: Bankroll dinamik takip
        self.bankroll: float = float(self.risk.get("bankroll_usd", 30.0))

        # V16: Tek global OFI buffer (Binance BTC/USDT için)
        self._ofi_buf = OFIBuffer(
            lookback_minutes=int(self.strat.get("ofi_lookback_minutes", 15)),
            snapshot_interval_s=10.0,
        )

        self.prices: Dict[str, float] = {"BTC_BINANCE": 0.0}
        self.prices_ts: Dict[str, int] = {"BTC_BINANCE_ts_src_ms": 0}

        self.trades:      int   = 0
        self.wins:        int   = 0
        self.losses:      int   = 0
        self.session_pnl: float = 0.0
        self.daily_pnl:   float = 0.0

        self._daily_reset: datetime = (
            datetime.now(timezone.utc)
            .replace(hour=0, minute=0, second=0, microsecond=0)
            + timedelta(days=1)
        )
        self._running:  bool = True
        self._rtds_ok:  bool = False
        self._rtds_tasks: List[asyncio.Task] = []

        # V16: Monte Carlo sonuçları (başlangıçta hesaplanır)
        self._mc_results: dict = {}

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

    # ------------------------------------------------------------------ Monte Carlo başlangıç

    def _run_startup_monte_carlo(self) -> None:
        """Bot başlamadan önce risk simülasyonu."""
        wr       = float(self.strat.get("target_win_rate", 0.65))
        kf       = float(self.risk.get("kelly_fraction",   0.5))
        bankroll = self.bankroll
        target   = float(self.risk.get("target_bankroll", 900.0))
        days     = int(self.risk.get("target_days", 45))
        tpd      = int(self.strat.get("trades_per_day", 3))

        self._mc_results = run_monte_carlo(
            win_rate=wr, kelly_fraction=kf,
            start_bankroll=bankroll, target=target,
            min_stake=float(self.risk.get("min_stake_usd", 2.5)),
            trades_per_day=tpd, days=days,
            n_simulations=1000,
        )

        self._log(
            f"Monte Carlo ({days}g, {tpd}x/g, WR={wr:.0%}, Kelly×{kf}): "
            f"Hedef={self._mc_results['success_pct']}% | "
            f"Ruin={self._mc_results['ruin_pct']}% | "
            f"Medyan=${self._mc_results['median_final']}",
            "INFO",
        )

    # ------------------------------------------------------------------ RTDS: Binance REST

    async def _rtds_binance_ws(self) -> None:
        """Binance BTC/USDT fiyat + derinlik (10s aralıkla OFI buffer güncellemesi)."""
        _PRICE_URL = "https://api.binance.com/api/v3/ticker/price"
        _DEPTH_URL = "https://api.binance.com/api/v3/depth"
        _PARAMS_P  = {"symbol": "BTCUSDT"}
        _PARAMS_D  = {"symbol": "BTCUSDT", "limit": 20}
        backoff    = 1.0

        connector = aiohttp.TCPConnector(
            family=socket.AF_INET,
            resolver=aiohttp.ThreadedResolver(),
            limit=4,
        )
        async with aiohttp.ClientSession(connector=connector) as sess:
            cycle = 0
            while self._running:
                try:
                    # Fiyat polling (her tur)
                    async with sess.get(
                        _PRICE_URL, params=_PARAMS_P,
                        timeout=aiohttp.ClientTimeout(total=3),
                    ) as r:
                        if r.status == 200:
                            data  = await r.json(content_type=None)
                            price = float(data.get("price", 0))
                            if price > 0:
                                self.prices["BTC_BINANCE"]              = price
                                self.prices_ts["BTC_BINANCE_ts_src_ms"] = int(
                                    time.time() * 1000)
                                if not self._rtds_ok:
                                    self._rtds_ok = True
                                    self._log("RTDS Binance polling basladi", "INFO")

                    # Derinlik polling (her 3 turda = ~3s)
                    if cycle % 3 == 0:
                        async with sess.get(
                            _DEPTH_URL, params=_PARAMS_D,
                            timeout=aiohttp.ClientTimeout(total=3),
                        ) as r2:
                            if r2.status == 200:
                                d = await r2.json(content_type=None)
                                bids = d.get("bids", [])
                                asks = d.get("asks", [])
                                bid_depth = sum(
                                    float(b[1]) for b in bids[:10]) if bids else 0.0
                                ask_depth = sum(
                                    float(a[1]) for a in asks[:10]) if asks else 0.0
                                # V16: 15dk kümülatif OFI güncelle
                                self._ofi_buf.update(bid_depth, ask_depth)

                    backoff = 1.0
                    await asyncio.sleep(1.0)
                    cycle += 1

                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self._rtds_ok = False
                    if self._running:
                        self._log(
                            f"RTDS Binance hata: {str(e)[:50]} — {backoff:.0f}s",
                            "WARNING",
                        )
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, 30.0)

        self._rtds_ok = False

    # ------------------------------------------------------------------ V16: Piyasa Tarayıcı

    async def _update_markets(self, session: aiohttp.ClientSession) -> None:
        """
        Gamma API taraması — V16 kriterleri:
        1. active=true, closed=false
        2. Süre: 1 saat - 7 gün
        3. Fiyat bandı: 0.30-0.45 VEYA 0.55-0.70
        4. Likidite: min $1000
        5. feesEnabled durumu kaydedilir
        """
        min_dur_h = float(self.strat.get("min_market_duration_hours", 1.0))
        max_dur_d = float(self.strat.get("max_market_duration_days",  7.0))
        pb_lo_min = float(self.strat.get("price_band_low_min",  0.30))
        pb_lo_max = float(self.strat.get("price_band_low_max",  0.45))
        pb_hi_min = float(self.strat.get("price_band_high_min", 0.55))
        pb_hi_max = float(self.strat.get("price_band_high_max", 0.70))
        min_liq   = float(self.strat.get("min_liquidity_usd", 1000.0))

        now = datetime.now(timezone.utc)
        params = {"active": "true", "closed": "false", "limit": 200}

        try:
            async with session.get(
                f"{self.net['gamma_url']}/events",
                params=params,
                timeout=aiohttp.ClientTimeout(total=8),
            ) as r:
                if r.status != 200:
                    return
                events = await r.json(content_type=None)
        except Exception as e:
            self._log(f"Gamma API hata: {str(e)[:60]}", "WARNING")
            return

        if not isinstance(events, list):
            events = [events] if isinstance(events, dict) else []

        new_count = 0
        for ev in events:
            if not isinstance(ev, dict):
                continue

            for m in ev.get("markets", []):
                if not isinstance(m, dict):
                    continue
                if m.get("closed") or m.get("active") is False:
                    continue

                mid = m.get("id")
                if not mid:
                    continue

                # Süre filtresi
                end_str = m.get("endDate", "")
                try:
                    end_t = datetime.fromisoformat(
                        end_str.replace("Z", "+00:00")
                    ).replace(tzinfo=timezone.utc)
                except Exception:
                    continue

                secs_left = (end_t - now).total_seconds()
                if secs_left < min_dur_h * 3600:
                    continue
                if secs_left > max_dur_d * 86400:
                    continue

                # Fiyat bandı filtresi
                try:
                    prices_raw = m.get("outcomePrices", "[]")
                    if isinstance(prices_raw, str):
                        prices_raw = json.loads(prices_raw)
                    yes_price = float(prices_raw[0]) if prices_raw else 0.5
                except Exception:
                    yes_price = 0.5

                in_low_band  = pb_lo_min <= yes_price <= pb_lo_max
                in_high_band = pb_hi_min <= yes_price <= pb_hi_max
                if not (in_low_band or in_high_band):
                    continue

                # Likidite filtresi
                liquidity = float(m.get("liquidity", 0) or 0)
                if liquidity < min_liq:
                    continue

                # feesEnabled durumu
                fees_enabled = bool(m.get("feesEnabled", True))

                # Token ID'leri
                cids   = _parse_clob_token_ids(m.get("clobTokenIds", []))
                yes_id = cids[0] if len(cids) > 0 else ""
                no_id  = cids[1] if len(cids) > 1 else ""
                if not yes_id:
                    continue

                if mid not in self.markets:
                    question = (m.get("question") or
                                ev.get("title") or "Bilinmeyen Piyasa")
                    dur_hours = secs_left / 3600.0
                    ms_new = MarketState(
                        mid, question, end_t, yes_id, no_id,
                        duration_hours=dur_hours,
                        fees_enabled=fees_enabled,
                    )
                    ms_new.liquidity  = liquidity
                    ms_new.best_ask   = yes_price + 0.01
                    ms_new.best_bid   = yes_price - 0.01
                    self.markets[mid] = ms_new
                    new_count += 1
                    fee_tag = "FREE" if not fees_enabled else "FEE"
                    self._log(
                        f"Radar [{fee_tag}] {dur_hours:.1f}h "
                        f"p={yes_price:.2f} liq=${liquidity:.0f}: "
                        f"{question[:40]}",
                        "INFO",
                    )

        if new_count > 0:
            self._log(f"Tarama: {new_count} yeni piyasa eklendi", "INFO")

    # ------------------------------------------------------------------ V16: Orderbook

    async def _fetch_book(self, session: aiohttp.ClientSession,
                           ms: MarketState) -> None:
        """Polymarket CLOB orderbook: best bid/ask güncelleme."""
        try:
            async with session.get(
                f"{self.net['clob_url']}/book",
                params={"token_id": ms.yes_id},
                timeout=aiohttp.ClientTimeout(total=3),
            ) as r:
                if r.status == 200:
                    d    = await r.json(content_type=None)
                    bids = sorted(d.get("bids", []),
                                  key=lambda x: float(x.get("price", 0)),
                                  reverse=True)
                    asks = sorted(d.get("asks", []),
                                  key=lambda x: float(x.get("price", 0)))
                    if bids and asks:
                        ms.best_bid = float(bids[0]["price"])
                        ms.best_ask = float(asks[0]["price"])
        except Exception:
            pass

    # ------------------------------------------------------------------ V16: Settled kontrol

    async def _check_resolved(self, session: aiohttp.ClientSession,
                               ms: MarketState) -> None:
        """
        Gamma API'den piyasa çözüm sonucunu sorgula.
        resolved=true ve resolution değerine göre YES/NO kazananı belirlenir.
        """
        try:
            async with session.get(
                f"{self.net['gamma_url']}/markets/{ms.mid}",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as r:
                if r.status == 200:
                    d = await r.json(content_type=None)
                    if isinstance(d, list):
                        d = d[0] if d else {}
                    if d.get("resolved") or d.get("closed"):
                        # "Yes" veya "No" kazananı
                        resolution = (d.get("resolution") or "").lower()
                        if resolution in ("yes", "1", "true"):
                            ms.resolved = True
                        elif resolution in ("no", "0", "false"):
                            ms.resolved = False
                        # Çözülmediyse None kalır
        except Exception:
            pass

    # ------------------------------------------------------------------ V16: Ana Analiz

    async def _analyze(self, ms: MarketState, session: aiohttp.ClientSession) -> None:
        if self._daily_loss_hit:
            ms.signal = "GUNLUK LIMIT"
            return

        if ms.has_traded and not ms.active_trade:
            ms.signal = "TAMAMLANDI"
            return

        # Pozisyon let-settle (1H+ binary → kapanışa bırak)
        if ms.active_trade:
            ms.signal = (
                f"LET SETTLE "
                f"{ms.hours_left:.1f}h "
                f"({ms.active_trade.side}@{ms.active_trade.entry_price:.2f})"
            )
            return

        # Max pozisyon
        max_pos = int(self.risk.get("max_open_positions", 2))
        if self._open_positions >= max_pos:
            ms.signal = "POS DOLU"
            return

        # Orderbook güncelle
        await self._fetch_book(session, ms)

        entry = ms.best_ask
        if entry <= 0.01 or entry >= 0.99:
            ms.signal = "FIYAT HATALI"
            return

        # OFI sinyali (global Binance BTC/USDT buffer)
        ofi_threshold = float(self.strat.get("ofi_ratio_threshold", 3.0))
        ofi_z_thresh  = float(self.strat.get("ofi_z_threshold",     2.0))
        ofi_signal    = self._ofi_buf.signal(ofi_threshold, ofi_z_thresh)
        ofi_ratio     = self._ofi_buf.ratio
        ofi_z         = self._ofi_buf.z_score

        if not ofi_signal:
            ms.signal = (
                f"OFI ZAYIF "
                f"r={ofi_ratio:.1f}x z={ofi_z:.1f} "
                f"n={self._ofi_buf.sample_count}"
            )
            return

        # Fiyat bandı uyumu — OFI yönü ile fiyat bandı eşleşmeli
        pb_lo_min = float(self.strat.get("price_band_low_min",  0.30))
        pb_lo_max = float(self.strat.get("price_band_low_max",  0.45))
        pb_hi_min = float(self.strat.get("price_band_high_min", 0.55))
        pb_hi_max = float(self.strat.get("price_band_high_max", 0.70))

        # YES sinyali → düşük bandda al (YES ucuz, OFI alıcı baskısı)
        # NO sinyali  → yüksek bantta NO al (NO = 1-ask_yes, yani ask_yes yüksekken NO ucuz)
        yes_price = (ms.best_bid + ms.best_ask) / 2.0
        if ofi_signal == "YES" and not (pb_lo_min <= yes_price <= pb_lo_max):
            ms.signal = f"BANT UYUMSUZ YES p={yes_price:.2f}"
            return
        if ofi_signal == "NO" and not (pb_hi_min <= yes_price <= pb_hi_max):
            ms.signal = f"BANT UYUMSUZ NO p={yes_price:.2f}"
            return

        # Kelly stake hesabı
        win_rate     = float(self.strat.get("target_win_rate", 0.65))
        kelly_frac   = float(self.risk.get("kelly_fraction",   0.5))
        min_stake    = float(self.risk.get("min_stake_usd",   2.5))
        max_stk_pct  = float(self.risk.get("max_stake_pct",  0.5))

        # NO tarafı için fiyat = 1 - yes_ask
        entry_price = ms.best_ask if ofi_signal == "YES" else (1.0 - ms.best_bid)
        entry_price = _safe_price(entry_price)

        stake = _calc_kelly_stake(
            bankroll=self.bankroll,
            win_rate=win_rate,
            entry_price=entry_price,
            kelly_fraction=kelly_frac,
            min_stake=min_stake,
            max_stake_pct=max_stk_pct,
        )
        if stake <= 0:
            ms.signal = "KELLY NEGATIF EV"
            return
        if stake > self.bankroll:
            ms.signal = "BAKIYE YETERSIZ"
            return

        # EV hesabı
        fee_rate  = float(self.strat.get("fee_rate_bps", 0.25))
        fee_exp   = int(self.strat.get("fee_exponent",   2))
        _, raw_shares = _safe_amounts(entry_price, stake)
        fee_usd   = _calc_fee_usd_v16(
            entry_price, raw_shares, fee_rate, fee_exp, ms.fees_enabled)
        net_shares = raw_shares - _calc_fee_shares_v16(
            entry_price, raw_shares, fee_rate, fee_exp, ms.fees_enabled)
        ev = _calc_ev_settle_v16(
            entry_price, raw_shares, win_rate,
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

        # Emir gönder
        token_id = ms.yes_id if ofi_signal == "YES" else ms.no_id
        ms.last_buy_attempt = now_ts

        oid = await self.order_mgr.place_buy(token_id, entry_price, raw_shares)
        if not oid or oid.startswith("ERR:"):
            self._log(f"FOK red ({int(fok_cd)}s bekleme): {oid}", "WARNING")
            return

        ms.active_trade = LiveTrade(
            market_id=ms.mid,
            token_id=token_id,
            side=ofi_signal,
            entry_price=entry_price,
            raw_shares=raw_shares,
            net_shares=net_shares,
            stake=stake,
            kelly_fraction=kelly_frac,
            win_rate_used=win_rate,
            fees_enabled=ms.fees_enabled,
            fee_usd=fee_usd,
            ev_usd=ev,
            ofi_ratio=ofi_ratio,
            ofi_z=ofi_z,
            order_id=oid,
        )

        fee_tag = "FREE" if not ms.fees_enabled else f"fee=${fee_usd:.3f}"
        self._log(
            f"{'LIVE' if self.live_mode else 'PAPER'} SNIPE ({ofi_signal}) "
            f"entry={entry_price:.3f} stake=${stake:.2f} "
            f"EV=${ev:.3f} OFI={ofi_ratio:.1f}x z={ofi_z:.1f} "
            f"Kelly×{kelly_frac} {fee_tag} "
            f"n={self._ofi_buf.sample_count}",
            "LIVE" if self.live_mode else "PAPER",
        )

        ms.signal = (
            f"{ofi_signal}@{entry_price:.2f} EV=${ev:.3f} "
            f"OFI={ofi_ratio:.1f}x"
        )

    # ------------------------------------------------------------------ V16: Settle

    async def _settle(self, mid: str,
                       session: aiohttp.ClientSession) -> None:
        ms = self.markets.get(mid)
        if not ms or ms.settled:
            return

        if ms.active_trade:
            # Gamma API'den sonuç sorgula
            if ms.resolved is None:
                await self._check_resolved(session, ms)

            if ms.resolved is None:
                # Henüz çözülmedi — bekle
                ms.signal = "SETTLE BEKLENIYOR"
                return

            t = ms.active_trade
            won = (ms.resolved and t.side == "YES") or \
                  (not ms.resolved and t.side == "NO")

            if won:
                pnl    = round(t.net_shares * 1.0 - t.raw_shares * t.entry_price, 4)
                reason = "SETTL_WIN"
                self.wins += 1
            else:
                pnl    = round(-(t.raw_shares * t.entry_price), 4)
                reason = "SETTL_LOSS"
                self.losses += 1

            # Bankroll güncelle (V16 kritik özellik)
            self.bankroll     = max(0.0, self.bankroll + pnl)
            self.session_pnl += pnl
            self.daily_pnl   += pnl
            self.trades      += 1

            self._record_v16(ms, pnl, reason)
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

    # ------------------------------------------------------------------ V16: Kayıt

    def _record_v16(self, ms: MarketState, pnl: float, rtype: str) -> None:
        t = ms.active_trade
        if not t:
            return

        wr_actual = (self.wins / self.trades * 100) if self.trades else 0.0
        record = {
            "ts":            datetime.now(timezone.utc).isoformat(),
            "mid":           ms.mid,
            "question":      ms.question[:60],
            "duration_h":    round(ms.duration_hours, 2),
            "fees_enabled":  ms.fees_enabled,
            "side":          t.side,
            "entry":         t.entry_price,
            "raw_shares":    t.raw_shares,
            "net_shares":    round(t.net_shares, 4),
            "stake":         t.stake,
            "kelly_fraction": t.kelly_fraction,
            "win_rate_used": t.win_rate_used,
            "fee_usd":       round(t.fee_usd, 4),
            "ev_usd":        round(t.ev_usd, 4),
            "ofi_ratio":     round(t.ofi_ratio, 3),
            "ofi_z":         round(t.ofi_z, 3),
            "pnl":           pnl,
            "result":        rtype,
            "bankroll_after": round(self.bankroll, 4),
            "wr_session_pct": round(wr_actual, 2),
            "btc_binance":   self.prices.get("BTC_BINANCE", 0.0),
            "live":          self.live_mode,
        }
        try:
            mem = self.cfg.get("memory_file", "trades_v16.jsonl")
            with open(mem, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        except Exception:
            pass

    # ------------------------------------------------------------------ V16: Render

    def _render(self) -> Layout:
        lay = Layout()
        lay.split_column(
            Layout(name="h", size=3),
            Layout(name="b", ratio=1),
            Layout(name="l", size=14),
        )
        lay["b"].split_row(Layout(name="mt", ratio=4), Layout(name="s", size=44))

        bn  = self.prices.get("BTC_BINANCE", 0.0)
        wr  = (self.wins / self.trades * 100) if self.trades else 0.0

        rtds_tag = "[bold green]OK[/]" if self._rtds_ok else "[red]ERR[/]"
        ofi_r    = self._ofi_buf.ratio
        ofi_z    = self._ofi_buf.z_score
        ofi_sig  = self._ofi_buf.signal(
            float(self.strat.get("ofi_ratio_threshold", 3.0)),
            float(self.strat.get("ofi_z_threshold",     2.0)),
        )
        ofi_col  = "green" if ofi_sig == "YES" else ("red" if ofi_sig == "NO" else "yellow")

        mc = self._mc_results
        mc_str = (
            f"${mc.get('success_pct',0)}% → ${mc.get('ruin_pct',0)}% ruin"
            if mc else "—"
        )

        hdr = (
            f"[bold white]KRAJEKIS V16.0 KELLY+OFI[/bold white] "
            f"{'[bold red]CANLI[/bold red]' if self.live_mode else '[dim]PAPER[/dim]'} | "
            f"RTDS:{rtds_tag} BTC:[cyan]${bn:,.0f}[/cyan] | "
            f"OFI:[{ofi_col}]{ofi_r:.1f}x z={ofi_z:.1f}[/] "
            f"{'→'+ofi_sig if ofi_sig else '→BEKLE'} | "
            f"Bankroll:[bold yellow]${self.bankroll:.2f}[/bold yellow]"
        )
        lay["h"].update(Panel(Text.from_markup(hdr), border_style="white"))

        # Piyasa tablosu
        tbl = Table(box=box.SIMPLE_HEAVY, expand=True, show_header=True)
        tbl.add_column("Süre",      width=12)
        tbl.add_column("Piyasa",    ratio=1)
        tbl.add_column("Ücret",     width=6)
        tbl.add_column("YES Fiyat", width=9)
        tbl.add_column("Liq.$",     width=8)
        tbl.add_column("Sinyal",    width=25)
        tbl.add_column("Pozisyon",  width=30)

        active_markets = sorted(
            [m for m in self.markets.values() if not m.settled],
            key=lambda m: m.secs_left,
        )
        for ms in active_markets[:12]:
            h_left = ms.hours_left
            if h_left < 0.017:
                time_str = f"{int(ms.secs_left)}s"
            elif h_left < 1:
                time_str = f"{ms.mins_left:.0f}m"
            else:
                time_str = f"{h_left:.1f}h"

            yes_p   = (ms.best_bid + ms.best_ask) / 2.0
            liq_str = f"${ms.liquidity:.0f}"
            row_style = ""

            pos_str = ""
            if ms.active_trade:
                t    = ms.active_trade
                gain = yes_p - t.entry_price if t.side == "YES" else \
                       (1 - yes_p) - t.entry_price
                pos_str   = (
                    f"{t.side}@{t.entry_price:.2f} "
                    f"({gain:+.3f}) EV=${t.ev_usd:.2f}"
                )
                row_style = "green" if gain >= 0 else "red"
            elif ms.has_traded:
                pos_str   = "[dim]KAPANDI[/dim]"
                row_style = "dim"

            tbl.add_row(
                time_str,
                ms.short_name,
                ms.fee_label,
                f"{yes_p:.3f}",
                liq_str,
                ms.signal,
                pos_str,
                style=row_style,
            )

        lay["mt"].update(Panel(
            tbl,
            title=(
                f"V16 Piyasa Radarı "
                f"({sum(1 for m in self.markets.values() if not m.settled)} aktif"
                f" | {self._open_positions} açık pozisyon)"
            ),
            border_style="cyan",
        ))

        # Sağ panel: istatistik
        kelly_frac = float(self.risk.get("kelly_fraction", 0.5))
        wr_target  = float(self.strat.get("target_win_rate", 0.65))
        min_dur    = float(self.strat.get("min_market_duration_hours", 1.0))
        max_dur    = float(self.strat.get("max_market_duration_days",  7.0))

        stat = (
            f"[bold cyan]OFI BUFFER (15dk kümülatif)[/bold cyan]\n"
            f"  Oran:    [{ofi_col}]{ofi_r:.2f}x[/] (eşik:{float(self.strat.get('ofi_ratio_threshold',3.0)):.1f}x)\n"
            f"  Z-skor:  [{ofi_col}]{ofi_z:+.2f}[/] (eşik:±{float(self.strat.get('ofi_z_threshold',2.0)):.1f})\n"
            f"  Örnek:   {self._ofi_buf.sample_count} snapshot\n"
            f"  Sinyal:  [{ofi_col}]{ofi_sig or 'BEKLE'}[/]\n\n"
            f"[bold cyan]KELLY + BANKROLL[/bold cyan]\n"
            f"  Bankroll: [bold yellow]${self.bankroll:.2f}[/bold yellow]\n"
            f"  Kelly:    ×{kelly_frac} (Half={kelly_frac==0.5})\n"
            f"  WR hedef: {wr_target:.0%}\n"
            f"  WR gerç:  [{'green' if wr>=wr_target*100 else 'red'}]{wr:.1f}%[/]\n\n"
            f"[bold cyan]MONTE CARLO (başlangıç)[/bold cyan]\n"
            f"  {mc_str}\n"
            f"  Medyan:  ${mc.get('median_final',0):.2f}\n"
            f"  P10/P90: ${mc.get('p10_final',0):.2f}/${mc.get('p90_final',0):.2f}\n\n"
            f"[bold cyan]PIYASA FİLTRELERİ[/bold cyan]\n"
            f"  Süre:    {min_dur:.0f}h - {max_dur:.0f}g\n"
            f"  Bant:    {float(self.strat.get('price_band_low_min',0.30)):.2f}-"
            f"{float(self.strat.get('price_band_low_max',0.45)):.2f} | "
            f"{float(self.strat.get('price_band_high_min',0.55)):.2f}-"
            f"{float(self.strat.get('price_band_high_max',0.70)):.2f}\n"
            f"  Min liq: ${float(self.strat.get('min_liquidity_usd',1000)):.0f}\n"
            f"  EV min:  ${float(self.strat.get('min_ev_threshold',0.05)):.3f}\n\n"
            f"[bold cyan]SEANS[/bold cyan]\n"
            f"  İşlem:   {self.trades} ({self.wins}W/{self.losses}L)\n"
            f"  Seans:   [{'green' if self.session_pnl>=0 else 'red'}]"
            f"${self.session_pnl:+.3f}[/]\n"
            f"  Günlük:  [{'green' if self.daily_pnl>=0 else 'red'}]"
            f"${self.daily_pnl:+.3f}[/]"
        )
        lay["s"].update(Panel(
            Text.from_markup(stat),
            title="V16 Kelly+OFI İstatistik",
            border_style="yellow",
        ))
        lay["l"].update(Panel(
            Text.from_markup("\n".join(list(self.logs))),
            title="Log [V16.0 | KELLY+15dk-OFI+LET-SETTLE | feesEnabled filtresiz]",
            border_style="cyan",
        ))
        return lay

    # ------------------------------------------------------------------ Ana döngü

    async def main_run(self) -> None:
        # Monte Carlo başlangıç simülasyonu
        self._run_startup_monte_carlo()

        connector = aiohttp.TCPConnector(
            limit=20,
            resolver=aiohttp.ThreadedResolver(),
            family=socket.AF_INET,
        )
        async with aiohttp.ClientSession(connector=connector) as session:
            self._rtds_tasks = [
                asyncio.create_task(
                    self._rtds_binance_ws(), name="rtds_binance"),
            ]

            try:
                if self.live_mode:
                    appr = await self.order_mgr.ensure_approvals()
                    self._log(f"Live Onay: {appr}", "INFO")

                self._log(
                    f"V16.0 KELLY+OFI Basladi "
                    f"({'CANLI' if self.live_mode else 'PAPER'}) | "
                    f"Bankroll=${self.bankroll:.2f} "
                    f"Kelly×{self.risk.get('kelly_fraction',0.5)} "
                    f"WR={float(self.strat.get('target_win_rate',0.65)):.0%} "
                    f"OFI={int(self.strat.get('ofi_lookback_minutes',15))}dk@"
                    f"{float(self.strat.get('ofi_ratio_threshold',3.0)):.1f}x",
                    "LIVE" if self.live_mode else "PAPER",
                )

                scan_interval = int(self.strat.get("scan_interval_secs", 300))

                with Live(self._render(), refresh_per_second=2, screen=True) as live:
                    cycle = 0
                    while self._running:
                        self._check_daily_reset()

                        # Piyasa tarama (her scan_interval saniyede)
                        if cycle % scan_interval == 0:
                            await self._update_markets(session)

                        # Her aktif piyasa için analiz
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
                await asyncio.gather(*self._rtds_tasks,
                                     return_exceptions=True)

        if self.live_mode:
            await self.order_mgr.cancel_all()


# ============================================================ entry point

if __name__ == "__main__":
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else "config_v16.json"
    try:
        cfg = load_config(cfg_path)
    except FileNotFoundError as e:
        print(f"Hata: {e}")
        sys.exit(1)

    bot = KrajekisSniperV16(cfg)
    if bot.live_mode:
        print(
            f"\nKRAJEKIS V16.0 KELLY+OFI CANLI MOD\n"
            f"Bankroll: ${bot.bankroll:.2f} → Hedef: "
            f"${bot.risk.get('target_bankroll', 900)}\n"
            f"Kelly: ×{bot.risk.get('kelly_fraction', 0.5)} | "
            f"WR hedef: {float(bot.strat.get('target_win_rate', 0.65)):.0%} | "
            f"OFI: {bot.strat.get('ofi_lookback_minutes', 15)}dk @"
            f"{float(bot.strat.get('ofi_ratio_threshold', 3.0)):.1f}x eşik\n"
        )
        ans = input("CANLI PARA modunda. Devam? [evet/hayir]: ").strip().lower()
        if ans not in ("evet", "e", "yes", "y"):
            sys.exit(0)
    try:
        asyncio.run(bot.main_run())
    except KeyboardInterrupt:
        pass
