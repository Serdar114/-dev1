#!/usr/bin/env python3
"""
Polymarket Krajekis Auto-Sniper V15.8 (EXECUTION ARMOR EDITION)
================================================================
  V15.8 değişiklikleri (Execution & Exit Armor):

  SL AGGRESSIVE FOK:
  + OrderManager.place_sell_sl(): FOK emir — anında doldur veya iptal.
    Fiyat: nominal_px - (sl_aggr_ticks × tick_size). Likidite boşluklarında
    ghost order riski yoktur.

  TP MAKER GTC + CANCEL/REPLACE:
  + OrderManager.place_sell_tp(): GTC maker emir (spread içinde) — rebate hedef.
  + MarketState.pending_sell: dict — {order_id, placed_at, reason, pnl, exit_px,
    is_sl, attempts}. Bekleyen satış emri takibi.
  + _handle_pending_sell(): tp_maker_timeout_s (default 15s) sonra emir iptal
    edilir ve daha agresif fiyatla yenilenir. N deneme sonrası Let Settle.

  LET SETTLE FLAG:
  + MarketState.letting_settle: bool — YES pozisyonu kârlıyken expiry'ye
    bırakılır; 1.0 settlement payout beklenir. Timeout veya TP başarısız
    olduğunda da tetiklenir.

  DEFERRED _record():
  + Paper modda: eski davranış korunur (anında kayıt).
  + Live modda: _record() sadece emir fill onaylandıktan sonra çağrılır.
    Ghost order senaryosunda pozisyon açık kalır, gerçek fill olmadan kapanmaz.

  OrderManager yeni metodlar:
  + get_order_status(order_id) → "MATCHED" | "OPEN" | "CANCELLED" | "ERR:..."
  + cancel_order(order_id) → "OK" | "ERR:..."

  Config yeni parametreler:
    sl_aggr_ticks=2, tp_maker_timeout_s=15, sl_cancel_timeout_s=5,
    let_settle_fallback_secs=30

  V15.7 değişiklikleri (Araştırma Entegrasyonu):

  CONVERGENCE MODEL — Ana Sinyal (Araştırma #1):
  + _norm_cdf(): scipy gerektirmez, Φ(z) yaklaşımı (Abramowitz & Stegun).
  + _calc_ev_settle(): Binary payout EV = P_win × net_shares - raw × entry.
  + _fair_prob_up(): Dijital opsiyon modeli — P_up = Φ( Δ / (σ × √τ) )
      Δ = (btc_now - ref_chainlink) / ref_chainlink  (strike'a mesafe)
      σ = realized_vol (5m veya 15m ufka göre)
      τ = secs_left / horizon_secs (0→1 normalize kalan süre)
  + _in_yes_window() / _in_no_window(): Zaman penceresi yardımcıları.
  + _pick_best_side(): YES ve NO EV hesapla → en yüksek EV'li taraf.
  + Brain1/Brain2 unifikasyonu: _analyze() artık convergence bazlı.
    conv_ev_threshold_usd (default $0.08): minimum EV eşiği.
    conv_use_ta_filter (default false): YES için TA onayı isteğe bağlı.
    conv_require_drop_filter (default false): NO için drop filtresi isteğe bağlı.

  ORDER FLOW IMBALANCE — Araştırma #2:
  + _fetch_book(): Top-5 derinlik toplanıyor, OFI hesaplanıyor.
    OFI = (bid_depth - ask_depth) / total ∈ [-1, +1]
  + MarketState.ofi / tick_size: her döngüde güncelleniyor.
  + LiveTrade.ofi_entry: giriş anındaki OFI loglanıyor.

  REALIZED VOLATILITY — Convergence modeli için:
  + realized_vol_5m  = std(son 20 1m return) × √5
  + realized_vol_15m = std(son 20 1m return) × √15
  + ta_data["BTC"]'ye eklendi, _render()'da gösteriliyor.

  V15.6: NO SL + Let YES Settle + R/R filtresi + SYNC filtresi.
  V15.5: RTDS WebSocket (Binance aggTrade + Chainlink Polygon).
  V15.4: Brain1 TA skor + Brain2 NO-late + max_open_positions.
"""
import sys
import asyncio
import socket
import ssl
import aiohttp
import json
import math
import time
import os
import pandas as pd
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


def load_config(path: str = "config.json") -> dict:
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


def _calculate_fee_shares(price: float, raw_shares: float) -> float:
    """Polymarket CLOB taker fee: fee_shares = raw * 0.25 * (p*(1-p))^2"""
    p = max(0.01, min(0.99, price))
    return raw_shares * 0.25 * (p * (1.0 - p)) ** 2


def _calc_fee_usd(price: float, raw_shares: float) -> float:
    """Fee USD maliyeti (settlement'ta 1.0 olduğu için fee_shares × 1.0)."""
    return _calculate_fee_shares(price, raw_shares)


def _calc_net_profit_if_tp(entry: float, raw_shares: float, tp_pct: float) -> float:
    """TP'ye ulaşıldığında fee sonrası net kâr."""
    net_shares = raw_shares - _calculate_fee_shares(entry, raw_shares)
    exit_price = min(0.99, entry * (1.0 + tp_pct))
    return net_shares * exit_price - raw_shares * entry


def _norm_cdf(z: float) -> float:
    """
    Standart normal CDF yaklaşımı — scipy gerektirmez.
    Abramowitz & Stegun 7.1.26, maks hata < 7.5e-8.
    """
    t    = 1.0 / (1.0 + 0.2316419 * abs(z))
    poly = t * (0.319381530
                + t * (-0.356563782
                       + t * (1.781477937
                              + t * (-1.821255978
                                     + t * 1.330274429))))
    cdf  = 1.0 - (1.0 / math.sqrt(2 * math.pi)) * math.exp(-0.5 * z * z) * poly
    return cdf if z >= 0 else 1.0 - cdf


def _calc_ev_settle(entry: float, raw_shares: float, p_win: float) -> float:
    """
    Settlement-bazlı beklenen değer (binary payout 0 veya 1).
    EV = P_win × net_shares - raw_shares × entry
    Pozitif → beklenen kâr var.
    """
    net_shares = raw_shares - _calculate_fee_shares(entry, raw_shares)
    return p_win * net_shares - raw_shares * entry


@dataclass
class LiveTrade:
    market_id:        str
    token_id:         str
    side:             str
    entry_price:      float
    raw_shares:       float
    net_shares:       float
    stake:            float
    entry_asset_px:   float
    order_id:         str      = ""
    entry_time:       datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    entry_ts_src_ms:  int      = 0      # Chainlink kaynak zaman damgası (ms)
    p_fair:           float    = 0.5    # V15.7: convergence fair probability
    ev_usd:           float    = 0.0    # V15.7: expected value (USD) giriş anında
    ofi_entry:        float    = 0.0    # V15.7: order flow imbalance giriş anında


class MarketState:
    def __init__(self, mid: str, question: str, end_time: datetime,
                 yes_id: str = "", no_id: str = "", horizon_min: int = 15):
        self.mid         = mid
        self.question    = question
        self.end_time    = end_time
        self.yes_id      = yes_id
        self.no_id       = no_id
        self.horizon_min = horizon_min
        self.start_time  = end_time - timedelta(minutes=horizon_min)

        self.best_ask:  float = 0.5
        self.best_bid:  float = 0.5
        self.signal:    str   = "BEKLE"
        self.active_trade: Optional[LiveTrade] = None
        self.has_traded:   bool  = False
        self.sl_strikes:   int   = 0
        self.last_buy_attempt: float = 0.0

        self.ref_chainlink:    float            = 0.0
        self.ref_chainlink_ts: Optional[datetime] = None

        # V15.7: Orderbook derinlik / OFI
        self.ofi:       float = 0.0    # order flow imbalance [-1, +1]
        self.tick_size: float = 0.01   # güncel tick size

        # V15.8: Execution Armor
        self.pending_sell:  Optional[dict] = None  # bekleyen satış emri takibi
        self.letting_settle: bool          = False  # YES pozisyonu settle'a bırakıldı

    @property
    def secs_left(self) -> float:
        return max(0.0, (self.end_time - datetime.now(timezone.utc)).total_seconds())

    @property
    def mins_left(self) -> float:
        return self.secs_left / 60.0

    @property
    def short_name(self) -> str:
        return (self.question[:34] + "...") if len(self.question) > 35 else self.question


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
                args   = OrderArgs(price=price_r, size=shares_r, side=BUY, token_id=token_id)
                signed = client.create_order(args)
                resp   = client.post_order(signed, OrderType.FOK)
                return (resp.get("orderID") or resp.get("order_id") or resp.get("id", ""))
            except Exception as e:
                return f"ERR:{e}"
        return await asyncio.get_running_loop().run_in_executor(self._executor, _do)

    async def place_sell(self, token_id: str, price: float, shares: float) -> str:
        if not self.live_mode:
            self._paper_seq += 1
            return f"PAPER-SELL-{self._paper_seq:04d}"
        def _do():
            try:
                client = self._client_or_raise()
                price_r, shares_r = _safe_amounts(price, shares * price)
                args   = OrderArgs(price=price_r, size=shares_r, side=SELL, token_id=token_id)
                signed = client.create_order(args)
                resp   = client.post_order(signed, OrderType.GTC)
                return (resp.get("orderID") or resp.get("order_id") or resp.get("id", ""))
            except Exception as e:
                return f"ERR:{e}"
        return await asyncio.get_running_loop().run_in_executor(self._executor, _do)

    async def place_sell_sl(self, token_id: str, price: float, shares: float) -> str:
        """V15.8: Stop-Loss satışı — FOK (Fill-Or-Kill). Ghost order riski yok."""
        if not self.live_mode:
            self._paper_seq += 1
            return f"PAPER-SL-{self._paper_seq:04d}"
        def _do():
            try:
                client = self._client_or_raise()
                price_r, shares_r = _safe_amounts(price, shares * price)
                args   = OrderArgs(price=price_r, size=shares_r, side=SELL, token_id=token_id)
                signed = client.create_order(args)
                resp   = client.post_order(signed, OrderType.FOK)
                return (resp.get("orderID") or resp.get("order_id") or resp.get("id", ""))
            except Exception as e:
                return f"ERR:{e}"
        return await asyncio.get_running_loop().run_in_executor(self._executor, _do)

    async def place_sell_tp(self, token_id: str, price: float, shares: float) -> str:
        """V15.8: Take-Profit satışı — GTC maker (spread içinde, rebate hedef)."""
        if not self.live_mode:
            self._paper_seq += 1
            return f"PAPER-TP-{self._paper_seq:04d}"
        def _do():
            try:
                client = self._client_or_raise()
                price_r, shares_r = _safe_amounts(price, shares * price)
                args   = OrderArgs(price=price_r, size=shares_r, side=SELL, token_id=token_id)
                signed = client.create_order(args)
                resp   = client.post_order(signed, OrderType.GTC)
                return (resp.get("orderID") or resp.get("order_id") or resp.get("id", ""))
            except Exception as e:
                return f"ERR:{e}"
        return await asyncio.get_running_loop().run_in_executor(self._executor, _do)

    async def get_order_status(self, order_id: str) -> str:
        """V15.8: Emir durumu sorgula. Döndürür: MATCHED | OPEN | CANCELLED | ERR:..."""
        if not self.live_mode:
            return "MATCHED"   # paper: her zaman anında dolu
        if not order_id or order_id.startswith("ERR"):
            return f"ERR:{order_id}"
        def _do():
            try:
                client = self._client_or_raise()
                resp   = client.get_order(order_id)
                status = (resp.get("status") or resp.get("orderStatus") or "UNKNOWN").upper()
                # Normalize: Polymarket "MATCHED" = dolu, "LIVE"/"OPEN" = açık
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

    async def cancel_order(self, order_id: str) -> str:
        """V15.8: Tek emri iptal et."""
        if not self.live_mode:
            return "OK"
        if not order_id or order_id.startswith("ERR"):
            return f"ERR:invalid_id"
        def _do():
            try:
                client = self._client_or_raise()
                try:
                    from py_clob_client.clob_types import OrderCancelParams
                    resp = client.cancel_order(OrderCancelParams(order_id=order_id))
                except (ImportError, AttributeError):
                    # Fallback: bazı versiyonlarda doğrudan dict gönderilir
                    resp = client.cancel({"orderID": order_id})
                return "OK"
            except Exception as e:
                return f"ERR:{e}"
        return await asyncio.get_running_loop().run_in_executor(self._executor, _do)

    async def approve_token(self, token_id: str) -> str:
        if not self.live_mode:
            return "PAPER"
        def _do():
            try:
                from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
                client = self._client_or_raise()
                resp   = client.update_balance_allowance(
                    params=BalanceAllowanceParams(
                        asset_type=AssetType.CONDITIONAL, token_id=token_id
                    )
                )
                return f"OK:{resp}"
            except Exception as e:
                return f"ERR:{e}"
        return await asyncio.get_running_loop().run_in_executor(self._executor, _do)

    async def ensure_approvals(self) -> str:
        if not self.live_mode:
            return "PAPER"
        def _do():
            import time as _time
            client  = self._client_or_raise()
            results = []
            try:
                from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
                for label, at in [("COLLATERAL", AssetType.COLLATERAL),
                                   ("CONDITIONAL", AssetType.CONDITIONAL)]:
                    try:
                        resp      = client.get_balance_allowance(
                            params=BalanceAllowanceParams(asset_type=at))
                        allowance = int(resp.get("allowance", "0") or "0") \
                            if isinstance(resp, dict) else 0
                        if allowance == 0:
                            client.update_balance_allowance(
                                params=BalanceAllowanceParams(asset_type=at))
                            _time.sleep(5.0)
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


class KrajekisSniperBot:
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

        self.prices: Dict[str, float] = {
            "BTC_BINANCE":   0.0,
            "BTC_CHAINLINK": 0.0,
        }
        self.prices_ts: Dict[str, int] = {
            "BTC_BINANCE_ts_src_ms":   0,
            "BTC_CHAINLINK_ts_src_ms": 0,
        }

        self.ta_data:  Dict[str, dict] = {}

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
        self._running: bool = True

        self._rtds_binance_ok:   bool = False
        self._rtds_chainlink_ok: bool = False
        self._rtds_tasks:        List[asyncio.Task] = []

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
    def _daily_limit_hit(self) -> bool:
        return self.daily_pnl <= -abs(self.risk.get("max_daily_loss_usd", 30.0))

    @property
    def _open_positions(self) -> int:
        return sum(1 for m in self.markets.values() if m.active_trade)

    # ------------------------------------------------------------------ RTDS yardımcı

    def _rtds_fresh(self, key_ts: str, max_age_s: float = 30.0) -> bool:
        ts_ms = self.prices_ts.get(key_ts, 0)
        if ts_ms == 0:
            return False
        return (time.time() * 1000 - ts_ms) < max_age_s * 1000

    # ------------------------------------------------------------------ RTDS: Binance WS

    async def _rtds_binance_ws(self) -> None:
        """Background task — Binance REST polling (1s), WebSocket yerine HTTP kullanır."""
        _URL    = "https://api.binance.com/api/v3/ticker/price"
        _PARAMS = {"symbol": "BTCUSDT"}
        backoff = 1.0
        logged  = False

        connector = aiohttp.TCPConnector(family=socket.AF_INET, limit=4)

        async with aiohttp.ClientSession(connector=connector) as sess:
            while self._running:
                try:
                    async with sess.get(
                        _URL, params=_PARAMS,
                        timeout=aiohttp.ClientTimeout(total=3),
                    ) as r:
                        if r.status == 200:
                            data  = await r.json(content_type=None)
                            price = float(data.get("price", 0))
                            if price > 0:
                                self.prices["BTC_BINANCE"]              = price
                                self.prices_ts["BTC_BINANCE_ts_src_ms"] = int(time.time() * 1000)
                                if not self._rtds_binance_ok:
                                    self._rtds_binance_ok = True
                                    self._log("RTDS Binance REST polling basladi (1s)", "INFO")
                                    logged = True
                            backoff = 1.0
                    await asyncio.sleep(1.0)

                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self._rtds_binance_ok = False
                    if self._running:
                        self._log(f"RTDS Binance HTTP hata: {str(e)[:50]} — {backoff:.0f}s", "WARNING")
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, 30.0)

        self._rtds_binance_ok = False

    # ------------------------------------------------------------------ RTDS: Chainlink WS

    async def _rtds_chainlink_ws(self) -> None:
        """Background task — Chainlink REST polling (5s), WebSocket yerine HTTP kullanır."""
        _RPC_ENDPOINTS = [
            "https://polygon-rpc.com",
            "https://rpc.ankr.com/polygon",
            "https://polygon.llamarpc.com",
            "https://1rpc.io/matic",
        ]
        _CONTRACT = "0xc907E116054Ad103354f2D350FD2514433D57F6f"
        _DATA     = "0xfeaf968c"
        backoff   = 1.0

        connector = aiohttp.TCPConnector(family=socket.AF_INET, limit=4)

        async with aiohttp.ClientSession(connector=connector) as sess:
            while self._running:
                payload = {
                    "jsonrpc": "2.0", "method": "eth_call",
                    "params": [{"to": _CONTRACT, "data": _DATA}, "latest"],
                    "id": int(time.time() * 1000),
                }
                got = False
                for rpc in _RPC_ENDPOINTS:
                    try:
                        async with sess.post(
                            rpc, json=payload,
                            timeout=aiohttp.ClientTimeout(total=4),
                        ) as r:
                            if r.status == 200:
                                res     = await r.json(content_type=None)
                                hex_val = res.get("result", "")
                                if hex_val and len(hex_val) >= 130:
                                    price = int(hex_val[66:130], 16) / 1e8
                                    if 10_000 < price < 1_000_000:
                                        self.prices["BTC_CHAINLINK"]              = price
                                        self.prices_ts["BTC_CHAINLINK_ts_src_ms"] = int(time.time() * 1000)
                                        if not self._rtds_chainlink_ok:
                                            self._rtds_chainlink_ok = True
                                            self._log("RTDS Chainlink REST polling basladi (5s)", "INFO")
                                        got = True
                                        backoff = 1.0
                                        break
                    except Exception:
                        continue

                if not got:
                    self._rtds_chainlink_ok = False

                try:
                    await asyncio.sleep(5.0 if got else backoff)
                    if not got:
                        backoff = min(backoff * 2, 30.0)
                except asyncio.CancelledError:
                    raise

        self._rtds_chainlink_ok = False

    # ------------------------------------------------------------------ pazar radar

    async def _update_markets(self, session: aiohttp.ClientSession) -> None:
        now      = int(datetime.now(timezone.utc).timestamp())
        base_5m  = (now // 300) * 300
        base_15m = (now // 900) * 900
        sluglar  = []
        for i in range(-1, 4):
            sluglar.append(f"btc-updown-5m-{base_5m  + i * 300}")
            sluglar.append(f"btc-updown-15m-{base_15m + i * 900}")

        for slug in sluglar:
            try:
                async with session.get(
                    f"{self.net['gamma_url']}/events",
                    params={"slug": slug},
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as r:
                    if r.status == 200:
                        data = await r.json()
                        for ev in (data if isinstance(data, list) else [data]):
                            if not ev or not isinstance(ev, dict):
                                continue
                            for m in ev.get("markets", []):
                                self._process_market(m, ev)
            except Exception:
                pass

    def _process_market(self, m: dict, ev: dict) -> int:
        if not isinstance(m, dict) or m.get("closed") or m.get("active") is False:
            return 0
        mid     = m.get("id")
        end_str = m.get("endDate") or ""
        try:
            end_t = datetime.fromisoformat(
                end_str.replace("Z", "+00:00")
            ).replace(tzinfo=timezone.utc)
        except Exception:
            return 0
        if (end_t - datetime.now(timezone.utc)).total_seconds() < -60:
            return 0
        cids   = _parse_clob_token_ids(m.get("clobTokenIds", []))
        yes_id = cids[0] if len(cids) > 0 else ""
        no_id  = cids[1] if len(cids) > 1 else ""

        if mid not in self.markets:
            question    = m.get("question") or ev.get("title") or "BTC Up/Down"
            slug        = m.get("slug", "") + question
            horizon_min = 15 if "15m" in slug.lower() or "15 min" in slug.lower() else 5
            ms_new      = MarketState(mid, question, end_t, yes_id, no_id, horizon_min)
            self.markets[mid] = ms_new
            self._log(
                f"Radar [{horizon_min}m]: {question[:40]} "
                f"start={ms_new.start_time.strftime('%H:%M')}UTC",
                "INFO"
            )
            return 1
        return 0

    # ------------------------------------------------------------------ fiyat + TA

    async def _fetch_chainlink_btc(self, session: aiohttp.ClientSession) -> Optional[float]:
        """HTTP fallback — RTDS taze değilse çağrılır."""
        _RPC_ENDPOINTS = [
            "https://polygon-rpc.com",
            "https://rpc.ankr.com/polygon",
            "https://polygon.llamarpc.com",
            "https://1rpc.io/matic",
        ]
        payload = {
            "jsonrpc": "2.0",
            "method":  "eth_call",
            "params":  [{"to": "0xc907E116054Ad103354f2D350FD2514433D57F6f",
                         "data": "0xfeaf968c"}, "latest"],
            "id": int(time.time() * 1000),
        }
        for endpoint in _RPC_ENDPOINTS:
            try:
                async with session.post(
                    endpoint, json=payload,
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as r:
                    if r.status == 200:
                        res     = await r.json()
                        hex_val = res.get("result", "")
                        if hex_val and len(hex_val) >= 130:
                            return int(hex_val[66:130], 16) / 1e8
            except Exception:
                continue
        return None

    async def _fetch_prices_and_ta(self, session: aiohttp.ClientSession) -> None:
        now_utc = datetime.now(timezone.utc)
        now_ms  = time.time() * 1000

        # --- CHAINLINK: RTDS taze ise HTTP'yi atla ---
        rtds_cl_fresh = self._rtds_fresh("BTC_CHAINLINK_ts_src_ms", max_age_s=30.0)
        if not rtds_cl_fresh:
            cl_price = await self._fetch_chainlink_btc(session)
            if cl_price:
                self.prices["BTC_CHAINLINK"]              = cl_price
                self.prices_ts["BTC_CHAINLINK_ts_src_ms"] = int(now_ms)
        cl_price = self.prices.get("BTC_CHAINLINK", 0.0) or None

        # Pazar referans fiyat kilitleme
        if cl_price:
            for ms in list(self.markets.values()):
                if ms.ref_chainlink == 0.0 and ms.secs_left > 0:
                    lag_secs = (now_utc - ms.start_time).total_seconds()
                    if lag_secs >= 0:
                        ms.ref_chainlink    = cl_price
                        ms.ref_chainlink_ts = now_utc
                        src_tag = "[RTDS]" if rtds_cl_fresh else "[HTTP]"
                        self._log(
                            f"ORACLE LOCK [{ms.horizon_min}m] {ms.short_name[:20]}... "
                            f"| CL=${cl_price:,.0f} | +{int(lag_secs)}s {src_tag}",
                            "INFO",
                        )
        else:
            bn_now = self.prices.get("BTC_BINANCE", 0.0)
            if bn_now > 0:
                for ms in list(self.markets.values()):
                    if ms.ref_chainlink == 0.0 and ms.secs_left > 0:
                        lag_secs = (now_utc - ms.start_time).total_seconds()
                        if lag_secs >= 0:
                            ms.ref_chainlink    = bn_now
                            ms.ref_chainlink_ts = now_utc
                            self._log(
                                f"BINANCE LOCK [{ms.horizon_min}m] {ms.short_name[:20]}... "
                                f"| BN=${bn_now:,.0f} | +{int(lag_secs)}s (CL yok)",
                                "WARNING",
                            )

        # --- BINANCE KLINES: TA + Realized Volatility ---
        rtds_bn_fresh = self._rtds_fresh("BTC_BINANCE_ts_src_ms", max_age_s=5.0)
        try:
            async with session.get(
                "https://api.binance.com/api/v3/klines",
                params={"symbol": "BTCUSDT", "interval": "1m", "limit": "100"},
                timeout=aiohttp.ClientTimeout(total=5)
            ) as r:
                if r.status == 200:
                    df = pd.DataFrame(
                        await r.json(),
                        columns=["ts", "open", "high", "low", "close", "vol",
                                 "ct", "qav", "nt", "tbv", "tqv", "ig"]
                    )
                    for col in ("open", "close", "high", "low", "vol"):
                        df[col] = df[col].astype(float)

                    if not rtds_bn_fresh:
                        self.prices["BTC_BINANCE"] = df["close"].iloc[-1]
                    if not cl_price:
                        self.prices["BTC_CHAINLINK"] = self.prices["BTC_BINANCE"]

                    # TA indikatörleri
                    df["EMA_21"] = df["close"].ewm(span=21, adjust=False).mean()
                    df["EMA_50"] = df["close"].ewm(span=50, adjust=False).mean()

                    delta = df["close"].diff()
                    gain  = delta.where(delta > 0, 0).ewm(alpha=1/14, adjust=False).mean()
                    loss  = (-delta.where(delta < 0, 0)).ewm(alpha=1/14, adjust=False).mean()
                    df["RSI_14"] = 100 - (100 / (1 + gain / loss))

                    tp         = (df["high"] + df["low"] + df["close"]) / 3
                    df["VWAP"] = (df["vol"] * tp).cumsum() / df["vol"].cumsum()

                    ema12 = df["close"].ewm(span=12, adjust=False).mean()
                    ema26 = df["close"].ewm(span=26, adjust=False).mean()
                    ml    = ema12 - ema26
                    df["MACD_Hist"] = ml - ml.ewm(span=9, adjust=False).mean()

                    last          = df.iloc[-1]
                    last3_bullish = all(
                        df["close"].iloc[i] > df["open"].iloc[i]
                        for i in [-3, -2, -1]
                    )

                    # V15.7: Realized Volatility — son 20 1m bar'dan
                    returns  = df["close"].pct_change().dropna()
                    tail20   = returns.tail(20)
                    sigma_1m = float(tail20.std()) if len(tail20) > 1 else 0.001
                    vol_5m   = max(sigma_1m * math.sqrt(5),  0.0005)
                    vol_15m  = max(sigma_1m * math.sqrt(15), 0.0009)

                    self.ta_data["BTC"] = {
                        "vwap":             float(last["VWAP"]),
                        "rsi":              float(last["RSI_14"]),
                        "ema21":            float(last["EMA_21"]),
                        "ema50":            float(last["EMA_50"]),
                        "macd":             float(last["MACD_Hist"]),
                        "last3_bullish":    last3_bullish,
                        "realized_vol_5m":  vol_5m,   # V15.7
                        "realized_vol_15m": vol_15m,  # V15.7
                    }
        except Exception:
            pass

        tasks = [
            self._fetch_book(session, ms)
            for ms in list(self.markets.values())
            if ms.secs_left > 0 and ms.yes_id
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _fetch_book(self, session: aiohttp.ClientSession, ms: MarketState) -> None:
        """Orderbook çek + OFI hesapla (V15.7: top-5 derinlik)."""
        try:
            async with session.get(
                f"{self.net['clob_url']}/book",
                params={"token_id": ms.yes_id},
                timeout=aiohttp.ClientTimeout(total=3)
            ) as r:
                if r.status == 200:
                    d    = await r.json()
                    bids = sorted(d.get("bids", []),
                                  key=lambda x: float(x.get("price", 0)), reverse=True)
                    asks = sorted(d.get("asks", []),
                                  key=lambda x: float(x.get("price", 0)))
                    if bids and asks:
                        ms.best_bid  = float(bids[0]["price"])
                        ms.best_ask  = float(asks[0]["price"])
                        ms.tick_size = float(d.get("tick_size", 0.01))

                        # V15.7: OFI — top-5 derinlik imbalance
                        bid_depth = sum(float(b.get("size", 0)) for b in bids[:5])
                        ask_depth = sum(float(a.get("size", 0)) for a in asks[:5])
                        total     = bid_depth + ask_depth
                        ms.ofi    = (bid_depth - ask_depth) / total if total > 0 else 0.0
        except Exception:
            pass

    # ------------------------------------------------------------------ TA sinyal (ikincil)

    def _signal(self, ms: MarketState) -> str:
        """
        Brain1 TA sinyali — V15.7'de sadece conv_use_ta_filter=True ise çağrılır.
        Convergence modeli ana sinyal; bu metod isteğe bağlı TA onay filtresi.
        """
        if ms.secs_left <= 0:
            return "BEKLE"

        is_15m = ms.horizon_min == 15
        st = float(self.strat.get("sweet_spot_15m_start" if is_15m else "sweet_spot_5m_start", 10.0))
        en = float(self.strat.get("sweet_spot_15m_end"   if is_15m else "sweet_spot_5m_end",   1.0))
        if not (en <= ms.mins_left <= st):
            return "ZAMAN DISI"

        ta = self.ta_data.get("BTC")
        if not ta or pd.isna(ta["vwap"]):
            return "TA BEKLENIYOR"

        px    = self.prices["BTC_BINANCE"]
        max_spread = float(self.strat.get("max_spread", 0.04))
        if ms.best_ask - ms.best_bid > max_spread:
            return "GENIS MAKAS"

        score = 0
        if px > ta["vwap"]:                                           score += 1
        if ta["ema21"] > ta["ema50"]:                                 score += 1
        if ta["rsi"] < float(self.strat.get("rsi_overbought", 70)):   score += 1
        if ta["macd"] > 0:                                            score += 1

        min_score = int(self.strat.get("min_signal_score", 2))
        if score >= min_score and ta.get("last3_bullish", False):
            return f"UP (LONG) {score}/4"
        return "YAPI BOZUK"

    def _signal_no_late(self, ms: MarketState) -> bool:
        """
        Brain2 drop filtresi — V15.7'de sadece conv_require_drop_filter=True ise çağrılır.
        """
        secs      = ms.secs_left
        win_start = float(self.strat.get("no_window_secs_start", 200.0))
        win_end   = float(self.strat.get("no_window_secs_end",    20.0))
        if not (win_end <= secs <= win_start):
            return False
        ref = ms.ref_chainlink
        if ref <= 0:
            return False
        btc_now     = self.prices["BTC_BINANCE"]
        drop_needed = float(self.strat.get(
            "no_min_btc_drop_5m" if ms.horizon_min == 5 else "no_min_btc_drop_15m", 60.0
        ))
        if btc_now >= ref - drop_needed:
            return False
        if ms.best_ask - ms.best_bid > float(self.strat.get("max_spread", 0.04)):
            return False
        return True

    # ------------------------------------------------------------------ V15.7: zaman penceresi

    def _in_yes_window(self, ms: MarketState) -> bool:
        is_15m = ms.horizon_min == 15
        st = float(self.strat.get("sweet_spot_15m_start" if is_15m else "sweet_spot_5m_start", 10.0))
        en = float(self.strat.get("sweet_spot_15m_end"   if is_15m else "sweet_spot_5m_end",   1.0))
        return en <= ms.mins_left <= st

    def _in_no_window(self, ms: MarketState) -> bool:
        secs  = ms.secs_left
        start = float(self.strat.get("no_window_secs_start", 200.0))
        end   = float(self.strat.get("no_window_secs_end",    20.0))
        return end <= secs <= start

    # ------------------------------------------------------------------ V15.7: convergence

    def _fair_prob_up(self, ms: MarketState) -> float:
        """
        Dijital opsiyon yaklaşımıyla UP settle olasılığı.
        P_up = Φ( Δ / (σ × √τ) )
          Δ = (btc_now - ref_chainlink) / ref_chainlink
          σ = realized_vol (5m veya 15m ufka göre)
          τ = secs_left / horizon_secs (0→1)
        """
        btc_now = self.prices["BTC_BINANCE"]
        strike  = ms.ref_chainlink
        if strike <= 0 or btc_now <= 0:
            return 0.5

        horizon_secs = ms.horizon_min * 60
        tau          = max(1.0, ms.secs_left) / horizon_secs

        vol_key = "realized_vol_5m" if ms.horizon_min == 5 else "realized_vol_15m"
        sigma   = self.ta_data.get("BTC", {}).get(vol_key)
        if not sigma or sigma <= 1e-6:
            sigma_5m = self.ta_data.get("BTC", {}).get("realized_vol_5m", 0.003)
            sigma    = sigma_5m * math.sqrt(ms.horizon_min / 5.0)

        delta = (btc_now - strike) / strike
        denom = sigma * math.sqrt(max(tau, 0.01))
        if denom < 1e-9:
            return 1.0 if delta >= 0 else 0.0
        return _norm_cdf(delta / denom)

    def _pick_best_side(
        self, ms: MarketState
    ) -> Tuple[str, str, float, float, float]:
        """
        YES ve NO için convergence EV hesapla, en yüksek EV'li tarafı döndür.
        Returns: (signal_str, side, entry, p_win, ev_usd)
        """
        max_spread = float(self.strat.get("max_spread", 0.04))
        if ms.best_ask - ms.best_bid > max_spread:
            return "GENIS MAKAS", "", 0.0, 0.5, -999.0

        p_up  = self._fair_prob_up(ms)
        p_dn  = 1.0 - p_up
        stake = float(self.risk["stake_usd"])
        min_e = float(self.strat.get("min_entry_price", 0.55))
        best  = ("CONV LOW", "", 0.0, 0.5, -999.0)

        # YES — sweet-spot zaman penceresindeyse
        if self._in_yes_window(ms) and ms.yes_id:
            entry = ms.best_ask
            max_e = float(self.strat.get("max_entry_yes", 0.87))
            if min_e <= entry <= max_e:
                _, raw = _safe_amounts(entry, stake)
                if raw > 0:
                    ev = _calc_ev_settle(entry, raw, p_up)
                    if ev > best[4]:
                        best = (f"CONV YES P={p_up:.2f} EV=${ev:.3f}",
                                "YES", entry, p_up, ev)

        # NO — son-dakika zaman penceresindeyse
        if self._in_no_window(ms) and ms.no_id:
            entry = 1.0 - ms.best_bid
            max_e = float(self.strat.get("max_entry_no", 0.83))
            if min_e <= entry <= max_e:
                _, raw = _safe_amounts(entry, stake)
                if raw > 0:
                    ev = _calc_ev_settle(entry, raw, p_dn)
                    if ev > best[4]:
                        best = (f"CONV NO  P={p_dn:.2f} EV=${ev:.3f}",
                                "NO", entry, p_dn, ev)

        return best

    # ------------------------------------------------------------------ V15.6: yardımcılar

    def _validate_entry_edge(self, entry: float, raw_shares: float, side: str) -> bool:
        """TP'deki minimum net kâr kontrolü (EV tamamlayıcısı)."""
        min_edge = float(self.strat.get("min_edge_usd", 0.08))
        tp_yes   = float(self.strat.get("tp_pct_gain", 0.20))
        tp_no    = float(self.strat.get("tp_pct_gain_no", tp_yes))
        tp       = tp_no if side == "NO" else tp_yes
        net_pnl  = _calc_net_profit_if_tp(entry, raw_shares, tp)
        if net_pnl < min_edge:
            self._log(
                f"EDGE RED: entry={entry:.3f} tp_pnl=${net_pnl:.3f} < ${min_edge:.2f}",
                "WARNING",
            )
            return False
        return True

    def _validate_price_sync(self) -> Tuple[bool, str]:
        """Chainlink–Binance senkronizasyon + anomali kontrolü."""
        cl     = self.prices.get("BTC_CHAINLINK", 0.0)
        bn     = self.prices.get("BTC_BINANCE",   0.0)
        now_ms = time.time() * 1000

        if cl <= 0 or bn <= 0:
            return True, ""

        max_dev   = float(self.strat.get("max_cl_deviation_pct", 0.06))
        deviation = abs(cl - bn) / bn
        if deviation > max_dev:
            msg = f"SYNC RED: CL=${cl:,.0f} BN=${bn:,.0f} sapma=%{deviation*100:.1f}"
            self._log(msg, "WARNING")
            return False, msg

        cl_ts_ms  = self.prices_ts.get("BTC_CHAINLINK_ts_src_ms", 0)
        max_stale = float(self.strat.get("max_cl_staleness_ms", 30_000))
        cl_age_ms = now_ms - cl_ts_ms if cl_ts_ms > 0 else 999_999
        if cl_ts_ms > 0 and cl_age_ms > max_stale:
            msg = f"SYNC RED: CL eskimiş ({cl_age_ms/1000:.0f}s)"
            self._log(msg, "WARNING")
            return False, msg

        bn_ts_ms = self.prices_ts.get("BTC_BINANCE_ts_src_ms", 0)
        if bn_ts_ms > 0 and cl_ts_ms > 0:
            if abs(bn_ts_ms - cl_ts_ms) > 60_000:
                self._log(
                    f"SYNC UYARI: BN-CL fark {abs(bn_ts_ms-cl_ts_ms)/1000:.0f}s",
                    "WARNING"
                )

        # V15.8.1: CL–Binance yönsel uyum kontrolü (referans varsa)
        # CL yukarı, Binance aşağı (veya tam tersi) ise Polymarket CL'ı değil
        # Binance'ı fiyatlar — sinyal geçersiz.
        if self.strat.get("conv_require_direction_agreement", True):
            for ms in self.markets.values():
                if ms.ref_chainlink > 0 and ms.active_trade is None:
                    cl_up = cl > ms.ref_chainlink
                    bn_up = bn > ms.ref_chainlink
                    if cl_up != bn_up:
                        msg = (f"DIR RED: CL={'UP' if cl_up else 'DN'} "
                               f"BN={'UP' if bn_up else 'DN'} ref={ms.ref_chainlink:,.0f}")
                        self._log(msg, "WARNING")
                        return False, msg
                    break   # İlk açık pazar referansını kullan

        return True, ""

    def _calc_rr_ratio(self, entry: float, raw_shares: float, side: str) -> float:
        """R/R oranı (EV tamamlayıcısı)."""
        tp_yes = float(self.strat.get("tp_pct_gain",    0.20))
        tp_no  = float(self.strat.get("tp_pct_gain_no", tp_yes))
        sl_yes = float(self.strat.get("hard_sl_pct",    0.25))
        sl_no  = float(self.strat.get("hard_sl_no_pct", 0.30))
        tp = tp_no if side == "NO" else tp_yes
        sl = sl_no if side == "NO" else sl_yes
        net_shares = raw_shares - _calculate_fee_shares(entry, raw_shares)
        profit     = _calc_net_profit_if_tp(entry, raw_shares, tp)
        exit_sl    = max(0.01, entry * (1.0 - sl))
        max_loss   = abs(net_shares * exit_sl - raw_shares * entry)
        if max_loss <= 0:
            return 99.0
        return profit / max_loss

    # ------------------------------------------------------------------ V15.8 armor

    def _sl_aggressive_price(self, ms: MarketState, side: str,
                              nominal_px: float, extra_ticks: int = 0) -> float:
        """
        SL için agresif fiyat hesapla.
        Nominal fiyattan (sl_aggr_ticks + extra_ticks) × tick_size kadar düşük.
        Bu sayede emir defterinde az likidite olsa bile FOK dolar.
        """
        tick  = ms.tick_size if ms.tick_size > 0 else 0.01
        ticks = int(self.strat.get("sl_aggr_ticks", 2)) + extra_ticks
        return _safe_price(nominal_px - tick * ticks)

    def _tp_maker_price(self, ms: MarketState, side: str, nominal_px: float) -> float:
        """
        TP için maker fiyat hesapla (spread içinde, rebate hedef).
        YES: best_ask'ın bir tick altında → emir defterinin en önüne girer.
        NO:  (1 - best_bid)'nin bir tick altında.
        Eğer nominal_px daha düşükse nominal_px kullanılır (TP hedefini korum).
        """
        tick = ms.tick_size if ms.tick_size > 0 else 0.01
        if side == "YES":
            maker_px = ms.best_ask - tick
        else:
            maker_px = (1.0 - ms.best_bid) - tick
        return _safe_price(min(maker_px, nominal_px))

    async def _handle_pending_sell(self, ms: MarketState) -> None:
        """
        V15.8: Bekleyen satış emrini takip et.
        - Fill onayı geldiyse _record() çağır (pozisyonu kapat).
        - Timeout dolmuşsa: SL → daha agresif FOK; TP → yeniden fiyatla veya Let Settle.
        """
        ps = ms.pending_sell
        if not ps:
            return

        now     = time.time()
        elapsed = now - ps["placed_at"]
        is_sl   = ps.get("is_sl", False)
        timeout = float(self.strat.get(
            "sl_cancel_timeout_s" if is_sl else "tp_maker_timeout_s", 15
        ))
        oid     = ps.get("order_id", "")

        # --- Doldurulma kontrolü ---
        status = await self.order_mgr.get_order_status(oid)
        if status == "MATCHED":
            self._record(ms, ps["pnl"], ps["reason"], ps["exit_px"])
            ms.pending_sell = None
            ms.has_traded   = True
            self._log(
                f"CIKIS ONAYLANDI | {ps['reason']} | PnL: ${ps['pnl']:+.3f} | oid={oid}",
                "TRADE",
            )
            return

        # --- Henüz zaman dolmadı ---
        if elapsed < timeout:
            return

        # --- Timeout: iptal et ve yeniden dene ---
        t = ms.active_trade
        if not t:
            ms.pending_sell = None
            return

        if status not in ("CANCELLED",):   # Henüz iptal edilmediyse iptal et
            cancel_r = await self.order_mgr.cancel_order(oid)
            self._log(f"EMIR IPTAL | oid={oid} | {cancel_r}", "WARNING")

        attempts = ps.get("attempts", 1)

        if is_sl:
            # SL retry: her denemede 1 tick daha agresif
            if attempts >= 3:
                self._log(
                    f"SL {attempts} DENEME BASARISIZ — LET SETTLE modu",
                    "WARNING",
                )
                ms.pending_sell  = None
                ms.letting_settle = True
                return
            new_px  = self._sl_aggressive_price(ms, t.side, ps["exit_px"],
                                                 extra_ticks=attempts)
            new_oid = await self.order_mgr.place_sell_sl(t.token_id, new_px, t.net_shares)
            ms.pending_sell = {
                "order_id":  new_oid,
                "placed_at": now,
                "reason":    ps["reason"],
                "pnl":       ps["pnl"],
                "exit_px":   new_px,
                "is_sl":     True,
                "attempts":  attempts + 1,
            }
            self._log(f"SL YENİDEN {attempts+1}. DENEME | px={new_px:.3f}", "WARNING")
        else:
            # TP timeout: yakın expiryse Let Settle, değilse yeniden fiyatla
            let_fb = float(self.strat.get("let_settle_fallback_secs", 30))
            if t.side == "YES" and ms.secs_left < let_fb:
                self._log(f"TP TIMEOUT → LET SETTLE ({ms.secs_left:.0f}s kaldi)", "TRADE")
                ms.pending_sell  = None
                ms.letting_settle = True
            else:
                cur_px  = ms.best_bid if t.side == "YES" else 1.0 - ms.best_ask
                new_px  = self._tp_maker_price(ms, t.side, cur_px)
                new_oid = await self.order_mgr.place_sell_tp(t.token_id, new_px, t.net_shares)
                ms.pending_sell = {
                    "order_id":  new_oid,
                    "placed_at": now,
                    "reason":    ps["reason"],
                    "pnl":       ps["pnl"],
                    "exit_px":   new_px,
                    "is_sl":     False,
                    "attempts":  attempts + 1,
                }
                self._log(f"TP YENİDEN | px={new_px:.3f}", "INFO")

    # ------------------------------------------------------------------ çıkış

    def _check_exit(self, ms: MarketState) -> Tuple[bool, str, float, float]:
        t = ms.active_trade
        if not t:
            return False, "", 0.0, 0.0

        tp_yes     = float(self.strat.get("tp_pct_gain",    0.20))
        tp_no      = float(self.strat.get("tp_pct_gain_no", tp_yes))
        sl         = float(self.strat.get("sl_pct_loss",    0.15))
        hard_sl    = float(self.strat.get("hard_sl_pct",    0.25))
        hard_sl_no = float(self.strat.get("hard_sl_no_pct", 0.30))

        tp = tp_no if t.side == "NO" else tp_yes

        cur_poly = _safe_price(ms.best_bid if t.side == "YES" else 1.0 - ms.best_ask)
        move_pct = (cur_poly - t.entry_price) / t.entry_price

        # "Let YES Settle" modu
        let_settle_secs    = float(self.strat.get("let_yes_settle_secs",           45.0))
        let_settle_min_pct = float(self.strat.get("let_yes_settle_min_profit_pct", 0.15))
        if (t.side == "YES"
                and let_settle_secs > 0
                and ms.secs_left < let_settle_secs
                and move_pct >= let_settle_min_pct):
            return False, "", 0.0, 0.0

        # Take Profit
        if move_pct >= tp:
            pnl = (t.net_shares * cur_poly) - (t.raw_shares * t.entry_price)
            ms.sl_strikes = 0
            return True, "TAKE_PROFIT", round(pnl, 4), cur_poly

        # NO Stop Loss (V15.6 kritik ekleme — SETTL_LOSS önleme)
        if t.side == "NO":
            if move_pct <= -hard_sl_no:
                pnl = (t.net_shares * cur_poly) - (t.raw_shares * t.entry_price)
                ms.sl_strikes = 0
                self._log(
                    f"NO SL ({hard_sl_no*100:.0f}%) | cur={cur_poly:.3f} "
                    f"entry={t.entry_price:.3f} | pnl=${pnl:+.3f}",
                    "WARNING",
                )
                return True, "STOP_LOSS", round(pnl, 4), cur_poly
            return False, "", 0.0, 0.0

        # YES Stop Loss
        if move_pct <= -hard_sl:
            pnl = (t.net_shares * cur_poly) - (t.raw_shares * t.entry_price)
            ms.sl_strikes = 0
            return True, "STOP_LOSS", round(pnl, 4), cur_poly

        if move_pct <= -sl:
            ms.sl_strikes += 1
            if ms.sl_strikes >= 3:
                pnl = (t.net_shares * cur_poly) - (t.raw_shares * t.entry_price)
                ms.sl_strikes = 0
                return True, "STOP_LOSS", round(pnl, 4), cur_poly
        else:
            ms.sl_strikes = 0

        return False, "", 0.0, 0.0

    # ------------------------------------------------------------------ analiz (V15.7)

    async def _analyze(self, ms: MarketState) -> None:
        if self._daily_limit_hit:
            ms.signal = "GUNLUK LIMIT"
            return

        if ms.has_traded and not ms.active_trade:
            return

        # Mevcut pozisyon çıkış kontrolü (V15.8 Execution Armor)
        if ms.active_trade:
            # Live modda önce bekleyen emir kontrol et
            if ms.pending_sell:
                await self._handle_pending_sell(ms)
                return

            # Let Settle modundaysa artık çıkış emri gönderme
            if ms.letting_settle:
                ms.signal = "LET SETTLE"
                return

            ok, reason, pnl, exit_px = self._check_exit(ms)
            if ok:
                t    = ms.active_trade
                is_sl = (reason == "STOP_LOSS")

                if not self.live_mode:
                    # Paper modu: anında kaydet (ghost order riski yok)
                    self._record(ms, pnl, reason, exit_px)
                    ms.has_traded = True
                    self._log(f"CIKIS | {reason} | PnL: ${pnl:+.3f}", "TRADE")
                else:
                    # Live modu: emir gönder, fill onayı gelince _record()
                    if is_sl:
                        aggr_px = self._sl_aggressive_price(ms, t.side, exit_px)
                        oid     = await self.order_mgr.place_sell_sl(
                            t.token_id, aggr_px, t.net_shares
                        )
                        ms.pending_sell = {
                            "order_id":  oid,
                            "placed_at": time.time(),
                            "reason":    reason,
                            "pnl":       pnl,
                            "exit_px":   aggr_px,
                            "is_sl":     True,
                            "attempts":  1,
                        }
                        self._log(
                            f"SL FOK GONDERILDI | px={aggr_px:.3f} oid={oid}",
                            "WARNING",
                        )
                    else:
                        tp_px = self._tp_maker_price(ms, t.side, exit_px)
                        oid   = await self.order_mgr.place_sell_tp(
                            t.token_id, tp_px, t.net_shares
                        )
                        ms.pending_sell = {
                            "order_id":  oid,
                            "placed_at": time.time(),
                            "reason":    reason,
                            "pnl":       pnl,
                            "exit_px":   tp_px,
                            "is_sl":     False,
                            "attempts":  1,
                        }
                        self._log(
                            f"TP MAKER GONDERILDI | px={tp_px:.3f} oid={oid}",
                            "TRADE",
                        )
            return

        # Maksimum pozisyon limiti
        max_pos = int(self.risk.get("max_open_positions", 5))
        if self._open_positions >= max_pos:
            ms.signal = "POS DOLU"
            return

        # Referans fiyat henüz kilitlenmemişse bekle
        if ms.ref_chainlink <= 0:
            ms.signal = "REF BEKLE"
            return

        # --- V15.7: Convergence modeli (ana sinyal) ---
        signal, side, entry, p_win, ev = self._pick_best_side(ms)
        ms.signal = signal

        min_ev = float(self.strat.get("conv_ev_threshold_usd", 0.08))
        if not side or ev < min_ev:
            return

        # TA onay filtresi (isteğe bağlı — paper aggressive'de kapalı)
        if side == "YES" and self.strat.get("conv_use_ta_filter", False):
            ta_sig = self._signal(ms)
            if "UP" not in ta_sig:
                ms.signal = f"CONV YES TA✗ {ta_sig[:8]}"
                return

        # NO drop filtresi (isteğe bağlı — paper aggressive'de kapalı)
        if side == "NO" and self.strat.get("conv_require_drop_filter", False):
            if not self._signal_no_late(ms):
                ms.signal = "CONV NO DROP✗"
                return

        # FOK cooldown
        now_ts = datetime.now(timezone.utc).timestamp()
        fok_cd = float(self.strat.get("fok_cooldown", 10))
        if now_ts - ms.last_buy_attempt < fok_cd:
            return

        # Sync kontrolü (Chainlink anomali filtresi)
        sync_ok, _ = self._validate_price_sync()
        if not sync_ok:
            ms.signal = "SYNC RED"
            return

        stake = float(self.risk["stake_usd"])
        _, raw_shares = _safe_amounts(entry, stake)
        net_shares    = raw_shares - _calculate_fee_shares(entry, raw_shares)

        # Edge güvencesi (EV tamamlayıcısı)
        if not self._validate_entry_edge(entry, raw_shares, side):
            ms.signal = "EDGE RED"
            return

        token_id            = ms.yes_id if side == "YES" else ms.no_id
        ms.last_buy_attempt = now_ts
        oid                 = await self.order_mgr.place_buy(token_id, entry, raw_shares)

        if not oid or oid.startswith("ERR:"):
            self._log(f"FOK iptal ({int(fok_cd)}s bekle) | {oid}", "WARNING")
            return

        cl_now    = self.prices["BTC_CHAINLINK"]
        cl_ts_src = self.prices_ts.get("BTC_CHAINLINK_ts_src_ms", 0)
        fee_usd   = _calc_fee_usd(entry, raw_shares)
        vol_key   = "realized_vol_5m" if ms.horizon_min == 5 else "realized_vol_15m"
        sigma_now = self.ta_data.get("BTC", {}).get(vol_key, 0.003)

        ms.active_trade = LiveTrade(
            market_id=ms.mid, token_id=token_id, side=side,
            entry_price=entry, raw_shares=raw_shares, net_shares=net_shares,
            stake=stake, entry_asset_px=cl_now, order_id=oid,
            entry_ts_src_ms=cl_ts_src,
            p_fair=p_win, ev_usd=ev, ofi_entry=ms.ofi,
        )

        self._log(
            f"{'LIVE' if self.live_mode else 'PAPER'} SNIPE ({side}) [{ms.horizon_min}m] | "
            f"entry={entry:.3f} P_fair={p_win:.3f} EV=${ev:.3f} OFI={ms.ofi:+.2f} | "
            f"σ={sigma_now*100:.2f}% τ={int(ms.secs_left)}s | "
            f"fee=${fee_usd:.3f} | CL=${cl_now:,.0f}",
            "LIVE" if self.live_mode else "PAPER",
        )
        appr = await self.order_mgr.approve_token(token_id)
        if appr not in ("PAPER",):
            self._log(f"Approval: {appr[:60]}", "INFO")

    # ------------------------------------------------------------------ settle

    async def _settle(self, mid: str) -> None:
        ms = self.markets.get(mid)
        if not ms:
            return

        if ms.active_trade:
            t       = ms.active_trade
            btc_now = self.prices["BTC_CHAINLINK"]
            btc_ref = ms.ref_chainlink if ms.ref_chainlink > 0 else t.entry_asset_px

            if btc_now > 0 and abs(btc_now - btc_ref) < 0.01:
                self._log(
                    f"UYARI: Chainlink stale! ref={btc_ref:,.2f} exit={btc_now:,.2f}",
                    "WARNING",
                )

            if ms.ref_chainlink > 0 and ms.ref_chainlink_ts:
                lag     = (ms.ref_chainlink_ts - ms.start_time).total_seconds()
                ref_src = "CHAINLINK_OPEN" if lag < 30 else "CHAINLINK_LATE"
            else:
                ref_src = "ENTRY_FALLBACK"

            if btc_ref > 0 and btc_now > 0:
                btc_up = btc_now >= btc_ref
                won    = (btc_up and t.side == "YES") or (not btc_up and t.side == "NO")
            else:
                cur_poly = _safe_price(
                    ms.best_bid if t.side == "YES" else 1.0 - ms.best_ask
                )
                won     = cur_poly > t.entry_price
                ref_src = "ORDERBOOK_FALLBACK"

            if won:
                pnl    = round(t.net_shares * 1.0 - t.raw_shares * t.entry_price, 4)
                reason = "SETTL_WIN"
            else:
                pnl    = round(-(t.raw_shares * t.entry_price), 4)
                reason = "SETTL_LOSS"

            exit_px = 1.0 if won else 0.0
            self._record(ms, pnl, reason, exit_px, btc_ref, ref_src)
            self._log(
                f"SETTLED | {reason} | [{ref_src}] "
                f"CL:{btc_ref:,.0f}→{btc_now:,.0f} "
                f"(Δ${btc_now-btc_ref:+,.0f}) | PnL:${pnl:+.3f}",
                "TRADE" if won else "WARNING",
            )

        del self.markets[mid]

    # ------------------------------------------------------------------ kayıt

    def _record(self, ms: MarketState, pnl: float, rtype: str, exit_px: float,
                btc_ref_used: float = 0.0, ref_src: str = "") -> None:
        t = ms.active_trade
        if not t:
            return

        self.trades      += 1
        self.session_pnl += pnl
        self.daily_pnl   += pnl
        if pnl > 0:
            self.wins   += 1
        else:
            self.losses += 1

        vol_key = "realized_vol_5m" if ms.horizon_min == 5 else "realized_vol_15m"

        record = {
            "ts":                         datetime.now(timezone.utc).isoformat(),
            "mid":                        ms.mid,
            "question":                   ms.question[:60],
            "horizon_min":                ms.horizon_min,
            "market_start_utc":           ms.start_time.isoformat(),
            "side":                       t.side,
            "entry":                      t.entry_price,
            "exit":                       exit_px,
            "raw_shares":                 t.raw_shares,
            "net_shares":                 round(t.net_shares, 4),
            "stake":                      t.stake,
            "pnl":                        pnl,
            "result":                     rtype,
            "btc_chainlink_market_open":  ms.ref_chainlink,
            "btc_chainlink_entry":        t.entry_asset_px,
            "btc_chainlink_exit":         self.prices.get("BTC_CHAINLINK", 0.0),
            "btc_chainlink_ref_used":     btc_ref_used,
            "ref_source":                 ref_src,
            "btc_binance":                self.prices.get("BTC_BINANCE", 0.0),
            "live":                       self.live_mode,
            # RTDS zaman damgaları
            "ts_src_ms_chainlink_entry":  t.entry_ts_src_ms,
            "ts_src_ms_chainlink_exit":   self.prices_ts.get("BTC_CHAINLINK_ts_src_ms", 0),
            "ts_src_ms_binance_exit":     self.prices_ts.get("BTC_BINANCE_ts_src_ms", 0),
            "rtds_binance_live":          self._rtds_binance_ok,
            "rtds_chainlink_live":        self._rtds_chainlink_ok,
            "ts_latency_ms": abs(
                self.prices_ts.get("BTC_BINANCE_ts_src_ms", 0) -
                self.prices_ts.get("BTC_CHAINLINK_ts_src_ms", 0)
            ),
            "cl_deviation_pct": round(
                abs(self.prices.get("BTC_CHAINLINK", 0) - self.prices.get("BTC_BINANCE", 1)) /
                max(self.prices.get("BTC_BINANCE", 1), 1) * 100, 4
            ),
            # Fee / edge
            "fee_usd": round(_calc_fee_usd(t.entry_price, t.raw_shares), 4),
            "net_profit_if_tp": round(
                _calc_net_profit_if_tp(
                    t.entry_price, t.raw_shares,
                    float(self.strat.get(
                        "tp_pct_gain_no" if t.side == "NO" else "tp_pct_gain", 0.20
                    ))
                ), 4
            ),
            # V15.7: convergence metrikleri
            "p_fair":        round(t.p_fair, 4),
            "ev_usd":        round(t.ev_usd, 4),
            "ofi_entry":     round(t.ofi_entry, 4),
            "realized_vol":  round(self.ta_data.get("BTC", {}).get(vol_key, 0.0), 6),
        }
        try:
            mem = self.cfg.get("memory_file", "trades_v15_paper.jsonl")
            with open(mem, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        except Exception:
            pass

        ms.active_trade = None

    # ------------------------------------------------------------------ render

    def _render(self) -> Layout:
        lay = Layout()
        lay.split_column(
            Layout(name="h", size=3),
            Layout(name="b", ratio=1),
            Layout(name="l", size=14),
        )
        lay["b"].split_row(Layout(name="mt", ratio=4), Layout(name="s", size=42))

        cl  = self.prices.get("BTC_CHAINLINK", 0.0)
        bn  = self.prices.get("BTC_BINANCE",   0.0)
        ta  = self.ta_data.get("BTC", {})
        rsi = ta.get("rsi", 0.0)
        wr  = (self.wins / self.trades * 100) if self.trades else 0.0
        lim = abs(self.risk.get("max_daily_loss_usd", 30.0))

        bn_rtds  = "[bold green]BN✓[/]" if self._rtds_binance_ok   else "[red]BN✗[/]"
        cl_rtds  = "[bold green]CL✓[/]" if self._rtds_chainlink_ok else "[red]CL✗[/]"
        vol_5m   = ta.get("realized_vol_5m", 0.0)

        hdr = (
            f"[bold white]KRAJEKIS V15.8 EXECUTION ARMOR[/bold white] "
            f"{'[bold red]CANLI[/bold red]' if self.live_mode else '[dim]PAPER[/dim]'} | "
            f"RTDS:{bn_rtds}/{cl_rtds} | "
            f"Link:[bold green]${cl:,.0f}[/bold green] "
            f"(Bin:[cyan]${bn:,.0f}[/cyan] Δ${abs(cl-bn):,.0f}) | "
            f"σ5m:[yellow]{vol_5m*100:.2f}%[/yellow] | "
            f"RSI:[magenta]{rsi:.1f}[/magenta] | "
            f"PnL:[{'green' if self.session_pnl>=0 else 'red'}]${self.session_pnl:+.3f}[/] | "
            f"W/L:[green]{self.wins}[/green]/[red]{self.losses}[/red]({wr:.0f}%)"
        )
        lay["h"].update(Panel(Text.from_markup(hdr), border_style="cyan"))

        tbl = Table(box=box.MINIMAL_DOUBLE_HEAD, expand=True)
        for col in ["Kalan", "Pazar", "YES", "NO", "Sinyal / Convergence", "Pozisyon"]:
            tbl.add_column(col, no_wrap=True)

        for ms in sorted(self.markets.values(), key=lambda x: x.secs_left):
            if ms.secs_left <= 0:
                continue
            pos_str   = ""
            row_style = "white"
            if ms.active_trade:
                t        = ms.active_trade
                cur_poly = _safe_price(ms.best_bid if t.side == "YES" else 1.0 - ms.best_ask)
                gain     = cur_poly - t.entry_price
                if ms.letting_settle:
                    armor_tag = " [yellow]⚓SETTLE[/yellow]"
                elif ms.pending_sell:
                    ps = ms.pending_sell
                    tag = "SL" if ps.get("is_sl") else "TP"
                    armor_tag = f" [cyan]⏳{tag}#{ps.get('attempts',1)}[/cyan]"
                else:
                    armor_tag = ""
                pos_str  = (
                    f"{t.side}@{t.entry_price:.2f}({gain:+.2f}) "
                    f"P={t.p_fair:.2f} EV=${t.ev_usd:.2f}{armor_tag}"
                )
                row_style = "green" if gain >= 0 else "red"
            elif ms.has_traded:
                pos_str   = "[dim]KAPANDI[/dim]"
                row_style = "dim"

            tbl.add_row(
                f"[{ms.horizon_min}m] {int(ms.mins_left)}m {int(ms.secs_left%60)}s",
                ms.short_name,
                f"{ms.best_ask:.3f}", f"{1.0-ms.best_bid:.3f}",
                ms.signal, pos_str,
                style=row_style,
            )

        lay["mt"].update(Panel(
            tbl,
            title=(f"Radar 5m+15m "
                   f"({sum(1 for m in self.markets.values() if m.secs_left>0)} pazar"
                   f" | {self._open_positions} acik)"),
            border_style="cyan",
        ))

        bn_age   = int((time.time()*1000 - self.prices_ts.get("BTC_BINANCE_ts_src_ms", 0))/1000)
        cl_age   = int((time.time()*1000 - self.prices_ts.get("BTC_CHAINLINK_ts_src_ms", 0))/1000)
        avg_ofi  = sum(m.ofi for m in self.markets.values() if m.secs_left > 0)
        n_active = sum(1 for m in self.markets.values() if m.secs_left > 0)
        avg_ofi  = avg_ofi / n_active if n_active > 0 else 0.0

        stat = (
            f"[bold cyan]TA (Binance)[/bold cyan]\n"
            f"  Fiyat: [cyan]${bn:,.0f}[/cyan] ({bn_age}s)\n"
            f"  VWAP:  [yellow]${ta.get('vwap',0):,.0f}[/yellow]\n"
            f"  EMA21: ${ta.get('ema21',0):,.0f}  EMA50: ${ta.get('ema50',0):,.0f}\n"
            f"  RSI:   [magenta]{rsi:.1f}[/magenta]  "
            f"MACD: {'[green]' if ta.get('macd',0)>0 else '[red]'}{ta.get('macd',0):+.2f}[/]\n\n"
            f"[bold cyan]CONVERGENCE + ARMOR (V15.8)[/bold cyan]\n"
            f"  σ5m:   [yellow]{vol_5m*100:.3f}%[/yellow]  "
            f"σ15m: {ta.get('realized_vol_15m',0)*100:.3f}%\n"
            f"  OFI:   {'[green]' if avg_ofi>=0 else '[red]'}{avg_ofi:+.3f}[/] (ort.)\n"
            f"  EV eşiği: ${self.strat.get('conv_ev_threshold_usd',0.08):.2f}  "
            f"TA filtre: {'açık' if self.strat.get('conv_use_ta_filter',False) else 'kapalı'}\n"
            f"  SL ticks: {int(self.strat.get('sl_aggr_ticks',2))}  "
            f"TP timeout: {int(self.strat.get('tp_maker_timeout_s',15))}s  "
            f"Let settle: {int(self.strat.get('let_settle_fallback_secs',30))}s\n\n"
            f"[bold cyan]ORACLE (Chainlink)[/bold cyan]\n"
            f"  Fiyat: [bold green]${cl:,.0f}[/bold green] ({cl_age}s)\n"
            f"  Fark:  ${abs(cl-bn):,.1f}  "
            f"RTDS: {'OK' if self._rtds_chainlink_ok else 'HTTP'}\n"
            f"  Dev: max {self.strat.get('max_cl_deviation_pct',0.06)*100:.0f}%"
            f" | stale <{int(self.strat.get('max_cl_staleness_ms',30000)/1000)}s\n\n"
            f"[bold cyan]FILTRELER[/bold cyan]\n"
            f"  Makas: max {self.strat.get('max_spread',0.04):.2f}\n"
            f"  YES: {self.strat.get('sweet_spot_5m_end',1.0)}-"
            f"{self.strat.get('sweet_spot_5m_start',4.5)} dk (5m)\n"
            f"  NO:  {int(self.strat.get('no_window_secs_end',20))}-"
            f"{int(self.strat.get('no_window_secs_start',200))} sn\n"
            f"  max_pos: {self.risk.get('max_open_positions',5)}  "
            f"cooldown: {int(self.strat.get('fok_cooldown',10))}s\n\n"
            f"[bold cyan]KASA[/bold cyan]\n"
            f"  Gunluk: [{'green' if self.daily_pnl>=0 else 'red'}]${self.daily_pnl:+.3f}[/] "
            f"(limit: -${lim:.2f})\n"
            f"  Toplam: {self.trades} islem | WR: [green]{wr:.1f}%[/green]"
        )
        lay["s"].update(Panel(
            Text.from_markup(stat),
            title="Krajekis V15.8 EXECUTION ARMOR",
            border_style="yellow",
        ))
        lay["l"].update(Panel(
            Text.from_markup("\n".join(list(self.logs))),
            title="Sistem Log [V15.8 | CONVERGENCE+OFI+VOL | FOK-SL+TP-MAKER+CANCEL-REPLACE]",
            border_style="cyan",
        ))
        return lay

    # ------------------------------------------------------------------ ana döngü

    async def main_run(self) -> None:
        connector = aiohttp.TCPConnector(
            limit=20,
            resolver=aiohttp.ThreadedResolver(),
            family=socket.AF_INET,
        )
        async with aiohttp.ClientSession(connector=connector) as session:
            self._rtds_tasks = [
                asyncio.create_task(self._rtds_binance_ws(),   name="rtds_binance"),
                asyncio.create_task(self._rtds_chainlink_ws(), name="rtds_chainlink"),
            ]

            try:
                if self.live_mode:
                    appr = await self.order_mgr.ensure_approvals()
                    self._log(f"Live Onay: {appr}", "INFO")

                self._log(
                    f"V15.8 EXECUTION ARMOR Basladi "
                    f"({'CANLI' if self.live_mode else 'PAPER'}) | "
                    f"EV>${self.strat.get('conv_ev_threshold_usd',0.08):.2f} "
                    f"SL_ticks={int(self.strat.get('sl_aggr_ticks',2))} "
                    f"TP_timeout={int(self.strat.get('tp_maker_timeout_s',15))}s "
                    f"let_settle_fb={int(self.strat.get('let_settle_fallback_secs',30))}s",
                    "LIVE" if self.live_mode else "PAPER",
                )

                with Live(self._render(), refresh_per_second=2, screen=True) as live:
                    cycle = 0
                    while self._running:
                        self._check_daily_reset()
                        if cycle % 30 == 0:   # hâlâ her ~30s market güncelle
                            await self._update_markets(session)
                        await self._fetch_prices_and_ta(session)
                        for ms in list(self.markets.values()):
                            if ms.secs_left > 0:
                                await self._analyze(ms)
                            else:
                                await self._settle(ms.mid)
                        live.update(self._render())
                        await asyncio.sleep(1)   # V15.8.1: 2s→1s — SL gap riski yarıya düşer
                        cycle += 1

            finally:
                self._running = False
                for task in self._rtds_tasks:
                    task.cancel()
                await asyncio.gather(*self._rtds_tasks, return_exceptions=True)

        if self.live_mode:
            await self.order_mgr.cancel_all()


if __name__ == "__main__":
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else "config.json"
    try:
        cfg = load_config(cfg_path)
    except FileNotFoundError:
        sys.exit(1)

    bot = KrajekisSniperBot(cfg)
    if bot.live_mode:
        ans = input("CANLI PARA modunda. Devam? [evet/hayir]: ").strip().lower()
        if ans not in ("evet", "e", "yes", "y"):
            sys.exit(0)
    try:
        asyncio.run(bot.main_run())
    except KeyboardInterrupt:
        pass
