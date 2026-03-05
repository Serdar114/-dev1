#!/usr/bin/env python3
"""
Polymarket Krajekis Auto-Sniper V15.2 (HYBRID EDITION)
=======================================================
  V15.1 uzerine degisiklikler:
  + BRAIN 1 (YES): TA tabanli yiikselis sinyali, TP ile cikis, SL korundu
  + BRAIN 2 (NO):  Son 30-150 saniyede, BTC acilisin altindaysa giris,
                   Stop-Loss YOK, settlement'a kadar tut, max_entry_no=0.83
  + YES ve NO icin ayri max_entry parametreleri (max_entry_yes / max_entry_no)
  + TA tabanli DN sinyali kaldirildi (NO icin artik _signal_no_late kullaniliyor)
"""
import sys
import asyncio
import socket
import aiohttp
import json
import time
import os
import pandas as pd
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Tuple
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
    price_r = round(max(0.01, min(0.99, float(price))), 2)
    shares_int = float(int(round(stake / price_r, 4)))
    return price_r, shares_int


def _calculate_fee_shares(price: float, raw_shares: float) -> float:
    p = max(0.01, min(0.99, price))
    return raw_shares * 0.25 * (p * (1.0 - p)) ** 2


@dataclass
class LiveTrade:
    market_id:      str
    token_id:       str
    side:           str
    entry_price:    float
    raw_shares:     float
    net_shares:     float
    stake:          float
    entry_asset_px: float
    order_id:       str = ""
    entry_time:     datetime = field(default_factory=lambda: datetime.now(timezone.utc))


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

        self.ref_chainlink:    float = 0.0
        self.ref_chainlink_ts: Optional[datetime] = None

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

        self.prices:   Dict[str, float] = {
            "BTC_BINANCE":   0.0,
            "BTC_CHAINLINK": 0.0,
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
        return self.daily_pnl <= -abs(self.risk.get("max_daily_loss_usd", 3.0))

    @property
    def _open_positions(self) -> int:
        return sum(1 for m in self.markets.values() if m.active_trade)

    # ------------------------------------------------------------------ pazar radar

    async def _update_markets(self, session: aiohttp.ClientSession) -> None:
        now      = int(datetime.now(timezone.utc).timestamp())
        # V15.1: Hem 5m hem 15m pazarlar izleniyor
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
            ms_new = MarketState(mid, question, end_t, yes_id, no_id, horizon_min)
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
        # Birden fazla ücretsiz Polygon RPC endpoint — biri çökerse diğeri devreye girer
        _RPC_ENDPOINTS = [
            "https://polygon-rpc.com",
            "https://rpc.ankr.com/polygon",
            "https://polygon.llamarpc.com",
            "https://1rpc.io/matic",
        ]
        req_id = int(time.time() * 1000)
        payload = {
            "jsonrpc": "2.0",
            "method":  "eth_call",
            "params":  [{
                "to":   "0xc907E116054Ad103354f2D350FD2514433D57F6f",
                "data": "0xfeaf968c"
            }, "latest"],
            "id": req_id,
        }
        for endpoint in _RPC_ENDPOINTS:
            try:
                async with session.post(
                    endpoint,
                    json=payload,
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
        cl_price = await self._fetch_chainlink_btc(session)
        now_utc  = datetime.now(timezone.utc)

        if cl_price:
            self.prices["BTC_CHAINLINK"] = cl_price
            for ms in list(self.markets.values()):
                if ms.ref_chainlink == 0.0 and ms.secs_left > 0:
                    lag_secs = (now_utc - ms.start_time).total_seconds()
                    if lag_secs >= 0:
                        ms.ref_chainlink    = cl_price
                        ms.ref_chainlink_ts = now_utc
                        self._log(
                            f"ORACLE LOCK [{ms.horizon_min}m] {ms.short_name[:20]}... "
                            f"| CL=${cl_price:,.0f} | +{int(lag_secs)}s gecikmeli",
                            "INFO"
                        )
        else:
            # Chainlink alinamazsa Binance ile ref_chainlink'i doldur (ENTRY_FALLBACK engeli)
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
                                "WARNING"
                            )

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
                    for col in ("close", "high", "low", "vol"):
                        df[col] = df[col].astype(float)

                    self.prices["BTC_BINANCE"] = df["close"].iloc[-1]

                    if not cl_price:
                        self.prices["BTC_CHAINLINK"] = self.prices["BTC_BINANCE"]

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

                    last = df.iloc[-1]
                    self.ta_data["BTC"] = {
                        "vwap":  float(last["VWAP"]),
                        "rsi":   float(last["RSI_14"]),
                        "ema21": float(last["EMA_21"]),
                        "ema50": float(last["EMA_50"]),
                        "macd":  float(last["MACD_Hist"]),
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
                        ms.best_bid = float(bids[0]["price"])
                        ms.best_ask = float(asks[0]["price"])
        except Exception:
            pass

    # ------------------------------------------------------------------ sinyal

    def _signal(self, ms: MarketState) -> str:
        if ms.secs_left <= 0:
            return "BEKLE"

        is_15m = ms.horizon_min == 15
        if is_15m:
            st = float(self.strat.get("sweet_spot_15m_start", 10.0))
            en = float(self.strat.get("sweet_spot_15m_end",   5.0))
        else:
            st = float(self.strat.get("sweet_spot_5m_start", 3.0))
            en = float(self.strat.get("sweet_spot_5m_end",   1.0))

        if not (en <= ms.mins_left <= st):
            return "ZAMAN DISI"

        ta = self.ta_data.get("BTC")
        if not ta or pd.isna(ta["vwap"]):
            return "TA BEKLENIYOR"

        px    = self.prices["BTC_BINANCE"]
        vwap  = ta["vwap"]
        rsi   = ta["rsi"]
        ema21 = ta["ema21"]
        ema50 = ta["ema50"]
        macd  = ta["macd"]

        # V15.1: Spread 0.04 (0.03'ten biraz genis, daha fazla giris)
        max_spread = float(self.strat.get("max_spread", 0.04))
        if ms.best_ask - ms.best_bid > max_spread:
            return "GENIS MAKAS"

        # Brain 1: Sadece YES (yukselis) sinyali - DN artik Brain 2'ye ait
        if (px > vwap and ema21 > ema50
                and rsi < float(self.strat.get("rsi_overbought", 70)) and macd > 0):
            return "UP (LONG)"

        return "YAPI BOZUK"

    # ------------------------------------------------------------------ Brain 2: NO

    def _signal_no_late(self, ms: MarketState) -> bool:
        """
        Brain 2 (NO): TA kullanmaz. Son 30-150 saniyede, BTC pazar acilisindan
        yeterince asagidaysa NO al. Stop-Loss yok, settlement'a kadar bekle.
        """
        secs = ms.secs_left
        win_start = float(self.strat.get("no_window_secs_start", 150.0))
        win_end   = float(self.strat.get("no_window_secs_end",   30.0))
        if not (win_end <= secs <= win_start):
            return False

        # Pazar acilisindaki BTC fiyati gerekli
        ref = ms.ref_chainlink
        if ref <= 0:
            return False

        btc_now = self.prices["BTC_BINANCE"]
        # 5m ve 15m icin farkli dusus esigi
        if ms.horizon_min == 5:
            drop_needed = float(self.strat.get("no_min_btc_drop_5m", 80.0))
        else:
            drop_needed = float(self.strat.get("no_min_btc_drop_15m", 150.0))

        if btc_now >= ref - drop_needed:
            return False

        # Spread kontrolu
        max_spread = float(self.strat.get("max_spread", 0.04))
        if ms.best_ask - ms.best_bid > max_spread:
            return False

        return True

    # ------------------------------------------------------------------ cikis

    def _check_exit(self, ms: MarketState) -> Tuple[bool, str, float, float]:
        t = ms.active_trade
        if not t:
            return False, "", 0.0, 0.0

        tp      = float(self.strat.get("tp_pct_gain", 0.20))
        sl      = float(self.strat.get("sl_pct_loss", 0.15))
        hard_sl = float(self.strat.get("hard_sl_pct", 0.25))

        cur_poly  = _safe_price(ms.best_bid if t.side == "YES" else 1.0 - ms.best_ask)
        move_pct  = (cur_poly - t.entry_price) / t.entry_price

        if move_pct >= tp:
            pnl = (t.net_shares * cur_poly) - (t.raw_shares * t.entry_price)
            ms.sl_strikes = 0
            return True, "TAKE_PROFIT", round(pnl, 4), cur_poly

        # Brain 2 (NO): Stop-Loss yok - settlement'a kadar tut
        if t.side == "NO":
            return False, "", 0.0, 0.0

        # Brain 1 (YES): Normal SL mantigi
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

    # ------------------------------------------------------------------ analiz

    async def _analyze(self, ms: MarketState) -> None:
        if self._daily_limit_hit:
            ms.signal = "GUNLUK LIMIT"
            return

        if ms.has_traded and not ms.active_trade:
            return

        if ms.active_trade:
            ok, reason, pnl, exit_px = self._check_exit(ms)
            if ok:
                await self.order_mgr.place_sell(
                    ms.active_trade.token_id, exit_px, ms.active_trade.net_shares
                )
                self._record(ms, pnl, reason, exit_px)
                ms.has_traded = True
                self._log(f"CIKIS | {reason} | PnL: ${pnl:+.3f}", "TRADE")
            return

        max_pos = int(self.risk.get("max_open_positions", 2))
        if self._open_positions >= max_pos:
            ms.signal = "POS DOLU"
            return

        min_e = float(self.strat.get("min_entry_price", 0.70))

        # --- Brain 1: YES (TA tabanli yukselis) ---
        ms.signal = self._signal(ms)
        if "UP" in ms.signal:
            side     = "YES"
            token_id = ms.yes_id
            entry    = ms.best_ask
            max_e    = float(self.strat.get("max_entry_yes", 0.87))
            if entry < min_e or entry > max_e or not token_id:
                return

        # --- Brain 2: NO (gec giris, BTC dusus teyidi) ---
        elif self._signal_no_late(ms):
            side     = "NO"
            token_id = ms.no_id
            entry    = 1.0 - ms.best_bid
            max_e    = float(self.strat.get("max_entry_no", 0.83))
            ms.signal = f"DN (LATE-NO) ref={ms.ref_chainlink:,.0f}"
            if entry < min_e or entry > max_e or not token_id:
                return

        else:
            return

        # FOK cooldown
        now_ts = datetime.now(timezone.utc).timestamp()
        fok_cd = float(self.strat.get("fok_cooldown", 20))
        if now_ts - ms.last_buy_attempt < fok_cd:
            return

        stake = float(self.risk["stake_usd"])
        _, raw_shares = _safe_amounts(entry, stake)
        net_shares    = raw_shares - _calculate_fee_shares(entry, raw_shares)

        ms.last_buy_attempt = now_ts
        oid = await self.order_mgr.place_buy(token_id, entry, raw_shares)

        if not oid or oid.startswith("ERR:"):
            self._log(f"FOK iptal ({int(fok_cd)}s bekle) | {oid}", "WARNING")
            return

        cl_now = self.prices["BTC_CHAINLINK"]
        ms.active_trade = LiveTrade(
            market_id=ms.mid, token_id=token_id, side=side,
            entry_price=entry, raw_shares=raw_shares, net_shares=net_shares,
            stake=stake, entry_asset_px=cl_now, order_id=oid
        )
        self._log(
            f"{'LIVE' if self.live_mode else 'PAPER'} SNIPE ({side}) [{ms.horizon_min}m] | "
            f"{entry:.3f} | {raw_shares:.0f} hisse (net:{net_shares:.2f}) | "
            f"Link:${cl_now:,.0f}",
            "LIVE" if self.live_mode else "PAPER"
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
                    "WARNING"
                )

            if ms.ref_chainlink > 0 and ms.ref_chainlink_ts:
                lag = (ms.ref_chainlink_ts - ms.start_time).total_seconds()
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
                won    = cur_poly > t.entry_price
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
                "TRADE" if won else "WARNING"
            )

        del self.markets[mid]

    # ------------------------------------------------------------------ kayit

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

        record = {
            "ts":                        datetime.now(timezone.utc).isoformat(),
            "mid":                       ms.mid,
            "question":                  ms.question[:60],
            "horizon_min":               ms.horizon_min,
            "market_start_utc":          ms.start_time.isoformat(),
            "side":                      t.side,
            "entry":                     t.entry_price,
            "exit":                      exit_px,
            "raw_shares":                t.raw_shares,
            "net_shares":                round(t.net_shares, 4),
            "stake":                     t.stake,
            "pnl":                       pnl,
            "result":                    rtype,
            "btc_chainlink_market_open": ms.ref_chainlink,
            "btc_chainlink_entry":       t.entry_asset_px,
            "btc_chainlink_exit":        self.prices.get("BTC_CHAINLINK", 0.0),
            "btc_chainlink_ref_used":    btc_ref_used,
            "ref_source":                ref_src,
            "btc_binance":               self.prices.get("BTC_BINANCE", 0.0),
            "live":                      self.live_mode,
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
        lay["b"].split_row(Layout(name="mt", ratio=4), Layout(name="s", size=40))

        cl  = self.prices.get("BTC_CHAINLINK", 0.0)
        bn  = self.prices.get("BTC_BINANCE",   0.0)
        ta  = self.ta_data.get("BTC", {})
        rsi = ta.get("rsi", 0.0)
        wr  = (self.wins / self.trades * 100) if self.trades else 0.0
        lim = abs(self.risk.get("max_daily_loss_usd", 3.0))

        hdr = (
            f"[bold white]KRAJEKIS V15.2 (Hybrid Edition)[/bold white] "
            f"{'[bold red]CANLI[/bold red]' if self.live_mode else '[dim]PAPER[/dim]'} | "
            f"Link:[bold green]${cl:,.0f}[/bold green] "
            f"(Bin:[cyan]${bn:,.0f}[/cyan] Δ${abs(cl-bn):,.0f}) | "
            f"VWAP:[yellow]${ta.get('vwap',0):,.0f}[/yellow] | "
            f"RSI:[magenta]{rsi:.1f}[/magenta] | "
            f"PnL:[{'green' if self.session_pnl>=0 else 'red'}]${self.session_pnl:+.3f}[/] | "
            f"W/L:[green]{self.wins}[/green]/[red]{self.losses}[/red]({wr:.0f}%)"
        )
        lay["h"].update(Panel(Text.from_markup(hdr), border_style="cyan"))

        tbl = Table(box=box.MINIMAL_DOUBLE_HEAD, expand=True)
        for col in ["Kalan", "BTC Pazar", "YES", "NO", "Sinyal", "Pozisyon"]:
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
                pos_str  = f"{t.side}@{t.entry_price:.2f} ({gain:+.2f})"
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
            title=f"Radar 5m+15m ({sum(1 for m in self.markets.values() if m.secs_left>0)} pazar"
                  f" | {self._open_positions} acik)",
            border_style="cyan",
        ))

        stat = (
            f"[bold cyan]TA (Binance)[/bold cyan]\n"
            f"  Fiyat: [cyan]${bn:,.0f}[/cyan]\n"
            f"  VWAP:  [yellow]${ta.get('vwap',0):,.0f}[/yellow]\n"
            f"  EMA21: ${ta.get('ema21',0):,.0f}\n"
            f"  EMA50: ${ta.get('ema50',0):,.0f}\n"
            f"  RSI:   [magenta]{rsi:.1f}[/magenta]\n"
            f"  MACD:  {'[green]' if ta.get('macd',0)>0 else '[red]'}{ta.get('macd',0):+.2f}[/]\n\n"
            f"[bold cyan]ORACLE (Chainlink)[/bold cyan]\n"
            f"  Fiyat: [bold green]${cl:,.0f}[/bold green]\n"
            f"  Fark:  ${abs(cl-bn):,.1f}\n\n"
            f"[bold cyan]FILTRELER[/bold cyan]\n"
            f"  Makas: max {self.strat.get('max_spread', 0.04):.2f}\n"
            f"  [cyan]Brain1 YES[/cyan]: max {self.strat.get('max_entry_yes', 0.87):.2f}\n"
            f"  [yellow]Brain2 NO[/yellow]:  max {self.strat.get('max_entry_no', 0.83):.2f} "
            f"(son {int(self.strat.get('no_window_secs_start',150))}s)\n"
            f"  NO drop 5m: ${self.strat.get('no_min_btc_drop_5m',80):.0f} "
            f"| 15m: ${self.strat.get('no_min_btc_drop_15m',150):.0f}\n"
            f"  5m YES pencere: {self.strat.get('sweet_spot_5m_end',1)}-{self.strat.get('sweet_spot_5m_start',3)} dk\n"
            f"  15m YES pencere: {self.strat.get('sweet_spot_15m_end',5)}-{self.strat.get('sweet_spot_15m_start',10)} dk\n\n"
            f"[bold cyan]KASA[/bold cyan]\n"
            f"  Gunluk: [{'green' if self.daily_pnl>=0 else 'red'}]${self.daily_pnl:+.3f}[/] "
            f"(limit: -${lim:.2f})\n"
            f"  Toplam: {self.trades} islem | WR: [green]{wr:.1f}%[/green]"
        )
        lay["s"].update(Panel(Text.from_markup(stat), title="Krajekis V15.2 Hybrid", border_style="yellow"))
        lay["l"].update(Panel(
            Text.from_markup("\n".join(list(self.logs))),
            title="Sistem Log [V15.2 Hybrid | Brain1=YES-TA Brain2=NO-Late]",
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
            if self.live_mode:
                appr = await self.order_mgr.ensure_approvals()
                self._log(f"Live Onay: {appr}", "INFO")

            self._log(
                f"V15.2 Hybrid Edition Basladi ({'CANLI' if self.live_mode else 'PAPER'})",
                "LIVE" if self.live_mode else "PAPER",
            )

            with Live(self._render(), refresh_per_second=2, screen=True) as live:
                cycle = 0
                while self._running:
                    self._check_daily_reset()
                    if cycle % 15 == 0:
                        await self._update_markets(session)
                    await self._fetch_prices_and_ta(session)
                    for ms in list(self.markets.values()):
                        if ms.secs_left > 0:
                            await self._analyze(ms)
                        else:
                            await self._settle(ms.mid)
                    live.update(self._render())
                    await asyncio.sleep(2)
                    cycle += 1

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
