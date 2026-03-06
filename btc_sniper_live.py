#!/usr/bin/env python3
"""
Polymarket Krajekis Sniper V16.0
=================================
  Brain 1 (YES): Erken TA Trend stratejisi (3/4 skor sistemi).
  Brain 2 (NO) : Late Convergence stratejisi (son saniye, stop-loss yok).
  Oracle       : 4x Polygon RPC ile Chainlink + Binance fallback.
"""
import sys
import json
import os
import time
import asyncio
import socket
import ssl
import pandas as pd
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Tuple

import aiohttp
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

# ---------------------------------------------------------------------------
# Yapılandırma
# ---------------------------------------------------------------------------

def load_config(path: str = "config.json") -> dict:
    if not os.path.exists(path):
        raise FileNotFoundError(f"{path} bulunamadi!")
    with open(path, encoding="utf-8") as f:
        return json.load(f)

def _has_creds(cfg: dict) -> bool:
    c = cfg.get("credentials", {})
    pk = c.get("private_key", "")
    return bool(c.get("api_key") and pk and pk not in ("", "0x"))

def _parse_token_ids(raw) -> list:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            res = json.loads(raw)
            return res if isinstance(res, list) else []
        except Exception:
            return []
    return []

# ---------------------------------------------------------------------------
# Yardımcı fonksiyonlar
# ---------------------------------------------------------------------------

def _clamp_price(p: float) -> float:
    return round(max(0.01, min(0.99, float(p))), 4)

def _safe_amounts(price: float, stake: float) -> Tuple[float, float]:
    pr = round(max(0.01, min(0.99, float(price))), 2)
    shares = float(int(round(stake / pr, 4)))
    return pr, shares

def _fee_shares(price: float, raw_shares: float) -> float:
    p = max(0.01, min(0.99, price))
    return raw_shares * 0.25 * (p * (1.0 - p)) ** 2

# ---------------------------------------------------------------------------
# Veri yapıları
# ---------------------------------------------------------------------------

@dataclass
class Trade:
    market_id:      str
    token_id:       str
    side:           str          # "YES" ya da "NO"
    entry_price:    float
    raw_shares:     float
    net_shares:     float
    stake:          float
    entry_btc_px:   float
    order_id:       str = ""
    entry_time:     datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class MarketState:
    def __init__(self, mid: str, question: str, end_time: datetime,
                 yes_id: str = "", no_id: str = "", horizon_min: int = 5):
        self.mid          = mid
        self.question     = question
        self.end_time     = end_time
        self.yes_id       = yes_id
        self.no_id        = no_id
        self.horizon_min  = horizon_min
        self.start_time   = end_time - timedelta(minutes=horizon_min)

        self.best_ask: float = 0.5
        self.best_bid: float = 0.5
        self.signal:   str   = "BEKLE"

        self.active_trade:      Optional[Trade] = None
        self.has_traded:        bool  = False
        self.sl_strikes:        int   = 0
        self.last_buy_ts:       float = 0.0
        self.ref_chainlink:     float = 0.0
        self.ref_chainlink_ts:  Optional[datetime] = None

    @property
    def secs_left(self) -> float:
        return max(0.0, (self.end_time - datetime.now(timezone.utc)).total_seconds())

    @property
    def mins_left(self) -> float:
        return self.secs_left / 60.0

    @property
    def short_name(self) -> str:
        return (self.question[:34] + "…") if len(self.question) > 35 else self.question

# ---------------------------------------------------------------------------
# Emir yöneticisi
# ---------------------------------------------------------------------------

class OrderManager:
    def __init__(self, cfg: dict, live_mode: bool):
        self.cfg       = cfg
        self.live_mode = live_mode
        self._client: Optional["ClobClient"] = None
        self._pool     = ThreadPoolExecutor(max_workers=3)
        self._seq      = 0

    def _get_client(self) -> "ClobClient":
        if self._client is None:
            cr = self.cfg["credentials"]
            funder = cr.get("wallet_address", "") or None
            self._client = ClobClient(
                host=self.cfg["network"]["clob_url"],
                chain_id=self.cfg["network"]["chain_id"],
                key=cr["private_key"],
                creds=ApiCreds(
                    api_key=cr["api_key"],
                    api_secret=cr["api_secret"],
                    api_passphrase=cr["api_passphrase"],
                ),
                funder=funder,
                signature_type=1 if funder else 0,
            )
        return self._client

    def _paper_id(self, prefix: str = "BUY") -> str:
        self._seq += 1
        return f"PAPER-{prefix}-{self._seq:04d}"

    async def _run(self, fn):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._pool, fn)

    async def place_buy(self, token_id: str, price: float, stake: float) -> str:
        if not self.live_mode:
            return self._paper_id("BUY")

        def _do():
            try:
                pr, shares = _safe_amounts(price, stake)
                args   = OrderArgs(price=pr, size=shares, side=BUY, token_id=token_id)
                signed = self._get_client().create_order(args)
                resp   = self._get_client().post_order(signed, OrderType.FOK)
                return resp.get("orderID") or resp.get("order_id") or resp.get("id", "")
            except Exception as e:
                return f"ERR:{e}"

        return await self._run(_do)

    async def place_sell(self, token_id: str, price: float, shares: float) -> str:
        if not self.live_mode:
            return self._paper_id("SELL")

        def _do():
            try:
                pr, sh = _safe_amounts(price, shares * price)
                args   = OrderArgs(price=pr, size=sh, side=SELL, token_id=token_id)
                signed = self._get_client().create_order(args)
                resp   = self._get_client().post_order(signed, OrderType.GTC)
                return resp.get("orderID") or resp.get("order_id") or resp.get("id", "")
            except Exception as e:
                return f"ERR:{e}"

        return await self._run(_do)

    async def approve_collateral(self) -> str:
        if not self.live_mode:
            return "PAPER"

        def _do():
            import time as _t
            try:
                from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
                client  = self._get_client()
                results = []
                for label, at in [("COLLATERAL", AssetType.COLLATERAL), ("CONDITIONAL", AssetType.CONDITIONAL)]:
                    try:
                        resp = client.get_balance_allowance(params=BalanceAllowanceParams(asset_type=at))
                        allowance = int((resp or {}).get("allowance", "0") or "0")
                        if allowance == 0:
                            client.update_balance_allowance(params=BalanceAllowanceParams(asset_type=at))
                            _t.sleep(4)
                            results.append(f"{label}:SET")
                        else:
                            results.append(f"{label}:OK")
                    except Exception as e:
                        results.append(f"{label}:ERR:{e}")
                return ",".join(results)
            except ImportError:
                return "IMPORT_ERR"

        return await self._run(_do)

    async def cancel_all(self) -> None:
        if not self.live_mode:
            return

        def _do():
            try:
                self._get_client().cancel_all_orders()
            except Exception:
                pass

        await self._run(_do)

# ---------------------------------------------------------------------------
# Ana bot
# ---------------------------------------------------------------------------

_CHAINLINK_RPCS = [
    "https://polygon-rpc.com",
    "https://rpc.ankr.com/polygon",
    "https://polygon.llamarpc.com",
    "https://1rpc.io/matic",
]
_CHAINLINK_CONTRACT = "0xc907E116054Ad103354f2D350FD2514433D57F6f"
_CHAINLINK_DATA     = "0xfeaf968c"


class SniperBot:
    def __init__(self, cfg: dict):
        self.cfg   = cfg
        self.risk  = cfg["risk"]
        self.strat = cfg["strategy"]
        self.net   = cfg["network"]

        self.live_mode = (
            not self.strat.get("paper_only", True)
            and CLOB_OK
            and _has_creds(cfg)
        )

        self.console   = Console()
        self.orders    = OrderManager(cfg, self.live_mode)
        self.markets:  Dict[str, MarketState] = {}
        self.logs:     deque = deque(maxlen=14)

        self.btc_binance:   float = 0.0
        self.btc_chainlink: float = 0.0
        self.ta:            dict  = {}

        self.trades:      int   = 0
        self.wins:        int   = 0
        self.losses:      int   = 0
        self.session_pnl: float = 0.0
        self.daily_pnl:   float = 0.0
        self._next_reset  = (
            datetime.now(timezone.utc)
            .replace(hour=0, minute=0, second=0, microsecond=0)
            + timedelta(days=1)
        )
        self._running = True

    # ------------------------------------------------------------------
    # Loglama
    # ------------------------------------------------------------------

    def _log(self, msg: str, level: str = "INFO") -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        color_map = {
            "INFO": "cyan", "TRADE": "bold green",
            "WARNING": "yellow", "ERROR": "bold red",
            "LIVE": "bold green", "PAPER": "dim cyan",
        }
        c = color_map.get(level, "white")
        self.logs.append(f"[{c}][{ts}] {level}[/] {msg}")
        try:
            with open("debug.log", "a", encoding="utf-8") as f:
                f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] [{level}] {msg}\n")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Günlük limit
    # ------------------------------------------------------------------

    def _check_daily_reset(self) -> None:
        if datetime.now(timezone.utc) >= self._next_reset:
            self.daily_pnl = 0.0
            self._next_reset += timedelta(days=1)
            self._log("Gunluk PnL sifirlandi")

    @property
    def _daily_limit_hit(self) -> bool:
        return self.daily_pnl <= -abs(self.risk.get("max_daily_loss_usd", 30.0))

    @property
    def _open_positions(self) -> int:
        return sum(1 for m in self.markets.values() if m.active_trade)

    # ------------------------------------------------------------------
    # Piyasa keşfi
    # ------------------------------------------------------------------

    async def _update_markets(self, sess: aiohttp.ClientSession) -> None:
        now     = int(datetime.now(timezone.utc).timestamp())
        base5   = (now // 300) * 300
        base15  = (now // 900) * 900
        slugs   = []
        for i in range(-1, 4):
            slugs.append(f"btc-updown-5m-{base5  + i * 300}")
            slugs.append(f"btc-updown-15m-{base15 + i * 900}")

        for slug in slugs:
            try:
                url = f"{self.net['gamma_url']}/events"
                async with sess.get(url, params={"slug": slug},
                                    timeout=aiohttp.ClientTimeout(total=5)) as r:
                    if r.status != 200:
                        continue
                    data = await r.json()
                    for ev in (data if isinstance(data, list) else [data]):
                        if not isinstance(ev, dict):
                            continue
                        for m in ev.get("markets", []):
                            self._register_market(m, ev)
            except Exception:
                pass

    def _register_market(self, m: dict, ev: dict) -> None:
        if not isinstance(m, dict) or m.get("closed") or m.get("active") is False:
            return
        mid     = m.get("id")
        end_str = m.get("endDate", "")
        try:
            end_t = datetime.fromisoformat(
                end_str.replace("Z", "+00:00")
            ).replace(tzinfo=timezone.utc)
        except Exception:
            return

        if (end_t - datetime.now(timezone.utc)).total_seconds() < -60:
            return

        cids   = _parse_token_ids(m.get("clobTokenIds", []))
        yes_id = cids[0] if len(cids) > 0 else ""
        no_id  = cids[1] if len(cids) > 1 else ""

        if mid in self.markets:
            return

        question    = m.get("question") or ev.get("title") or "BTC Up/Down"
        slug_txt    = m.get("slug", "") + question
        horizon_min = 15 if "15m" in slug_txt.lower() else 5
        ms          = MarketState(mid, question, end_t, yes_id, no_id, horizon_min)
        self.markets[mid] = ms
        self._log(f"Radar [{horizon_min}m]: {question[:40]}", "INFO")

    # ------------------------------------------------------------------
    # Fiyat & TA
    # ------------------------------------------------------------------

    async def _fetch_chainlink(self, sess: aiohttp.ClientSession) -> float:
        payload = {
            "jsonrpc": "2.0",
            "method":  "eth_call",
            "params":  [{"to": _CHAINLINK_CONTRACT, "data": _CHAINLINK_DATA}, "latest"],
            "id":      int(time.time() * 1000),
        }
        for rpc in _CHAINLINK_RPCS:
            try:
                async with sess.post(
                    rpc, json=payload,
                    timeout=aiohttp.ClientTimeout(total=3),
                    ssl=False,
                ) as r:
                    if r.status == 200:
                        res = await r.json(content_type=None)
                        hex_val = res.get("result", "")
                        if hex_val and len(hex_val) >= 130:
                            return int(hex_val[66:130], 16) / 1e8
            except Exception:
                continue
        return 0.0

    async def _fetch_binance_klines(self, sess: aiohttp.ClientSession) -> None:
        try:
            params = {"symbol": "BTCUSDT", "interval": "1m", "limit": "100"}
            async with sess.get(
                "https://api.binance.com/api/v3/klines",
                params=params,
                timeout=aiohttp.ClientTimeout(total=6),
                ssl=False,
            ) as r:
                if r.status != 200:
                    return
                raw = await r.json(content_type=None)
                df  = pd.DataFrame(
                    raw,
                    columns=["ts","open","high","low","close","vol",
                             "ct","qav","nt","tbv","tqv","ig"],
                )
                for col in ("close", "high", "low", "vol"):
                    df[col] = df[col].astype(float)

                self.btc_binance = float(df["close"].iloc[-1])

                # Chainlink gelmemişse Binance ile doldur
                if self.btc_chainlink == 0.0:
                    self.btc_chainlink = self.btc_binance
                    self._log("BINANCE LOCK devrede (Chainlink 0)", "WARNING")

                # Teknik analiz
                df["EMA21"] = df["close"].ewm(span=21, adjust=False).mean()
                df["EMA50"] = df["close"].ewm(span=50, adjust=False).mean()

                delta = df["close"].diff()
                gain  = delta.where(delta > 0, 0).ewm(alpha=1/14, adjust=False).mean()
                loss  = (-delta.where(delta < 0, 0)).ewm(alpha=1/14, adjust=False).mean()
                df["RSI"] = 100 - (100 / (1 + gain / loss))

                tp           = (df["high"] + df["low"] + df["close"]) / 3
                df["VWAP"]   = (df["vol"] * tp).cumsum() / df["vol"].cumsum()

                ema12       = df["close"].ewm(span=12, adjust=False).mean()
                ema26       = df["close"].ewm(span=26, adjust=False).mean()
                macd_line   = ema12 - ema26
                df["MACD"]  = macd_line - macd_line.ewm(span=9, adjust=False).mean()

                last = df.iloc[-1]
                self.ta = {
                    "vwap":  float(last["VWAP"]),
                    "rsi":   float(last["RSI"]),
                    "ema21": float(last["EMA21"]),
                    "ema50": float(last["EMA50"]),
                    "macd":  float(last["MACD"]),
                }
        except Exception:
            pass

    async def _update_prices(self, sess: aiohttp.ClientSession) -> None:
        cl = await self._fetch_chainlink(sess)
        if cl > 0:
            self.btc_chainlink = cl

        await self._fetch_binance_klines(sess)

        # Chainlink referans kilidi
        now = datetime.now(timezone.utc)
        if self.btc_chainlink > 0:
            for ms in list(self.markets.values()):
                if ms.ref_chainlink == 0.0 and ms.secs_left > 0:
                    if (now - ms.start_time).total_seconds() >= 0:
                        ms.ref_chainlink    = self.btc_chainlink
                        ms.ref_chainlink_ts = now
                        src = "BINANCE-LOCK" if self.btc_chainlink == self.btc_binance else "CHAINLINK"
                        self._log(f"REF KILIT [{ms.horizon_min}m] {src} ${self.btc_chainlink:,.0f}", "INFO")

        # Order book
        tasks = [
            self._fetch_book(sess, ms)
            for ms in self.markets.values()
            if ms.secs_left > 0 and ms.yes_id
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _fetch_book(self, sess: aiohttp.ClientSession, ms: MarketState) -> None:
        try:
            url = f"{self.net['clob_url']}/book"
            async with sess.get(
                url, params={"token_id": ms.yes_id},
                timeout=aiohttp.ClientTimeout(total=3),
                ssl=False,
            ) as r:
                if r.status != 200:
                    return
                d    = await r.json(content_type=None)
                bids = sorted(d.get("bids", []), key=lambda x: float(x.get("price", 0)), reverse=True)
                asks = sorted(d.get("asks", []), key=lambda x: float(x.get("price", 0)))
                if bids and asks:
                    ms.best_bid = float(bids[0]["price"])
                    ms.best_ask = float(asks[0]["price"])
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Sinyal üretimi
    # ------------------------------------------------------------------

    def _signal(self, ms: MarketState) -> str:
        if ms.secs_left <= 0:
            return "BITTI"
        if not self.ta or pd.isna(self.ta.get("vwap", float("nan"))):
            return "TA BEKLENIYOR"

        spread = ms.best_ask - ms.best_bid
        if spread > float(self.strat.get("max_spread", 0.04)):
            return "GENIS MAKAS"

        px   = self.btc_binance
        vwap = self.ta["vwap"]
        rsi  = self.ta["rsi"]
        e21  = self.ta["ema21"]
        e50  = self.ta["ema50"]
        macd = self.ta["macd"]
        cl   = self.btc_chainlink

        is_15m = (ms.horizon_min == 15)

        # --- Brain 1: YES (YUKARI) ---
        yes_start = float(self.strat.get("sweet_spot_15m_start" if is_15m else "sweet_spot_5m_start", 10.0))
        yes_end   = float(self.strat.get("sweet_spot_15m_end"   if is_15m else "sweet_spot_5m_end",    5.0))

        if yes_end <= ms.mins_left <= yes_start:
            score = sum([
                px > vwap,
                e21 > e50,
                rsi < float(self.strat.get("rsi_overbought", 70)),
                macd > 0,
            ])
            if score >= int(self.strat.get("min_signal_score", 3)):
                return "UP (LONG)"

        # --- Brain 2: NO (ASAGI) ---
        no_start_s = float(self.strat.get("no_window_secs_start", 150.0))
        no_end_s   = float(self.strat.get("no_window_secs_end",    30.0))
        no_start_m = no_start_s / 60.0
        no_end_m   = no_end_s   / 60.0

        if no_end_m <= ms.mins_left <= no_start_m:
            ref = ms.ref_chainlink
            if ref > 0:
                req_drop = float(self.strat.get(
                    "no_min_btc_drop_15m" if is_15m else "no_min_btc_drop_5m",
                    100.0 if is_15m else 60.0,
                ))
                if (ref - cl) >= req_drop:
                    no_px = 1.0 - ms.best_bid
                    if no_px <= float(self.strat.get("max_entry_no", 0.83)):
                        return "DN (LATE-SHORT)"
                    return "PAHALI (NO)"

        return "BEKLE"

    # ------------------------------------------------------------------
    # Çıkış kontrolü
    # ------------------------------------------------------------------

    def _check_exit(self, ms: MarketState) -> Tuple[bool, str, float, float]:
        t = ms.active_trade
        if not t:
            return False, "", 0.0, 0.0

        cur_poly = _clamp_price(ms.best_bid if t.side == "YES" else 1.0 - ms.best_ask)
        move_pct = (cur_poly - t.entry_price) / t.entry_price

        if t.side == "YES":
            tp       = float(self.strat.get("tp_pct_gain",  0.20))
            sl       = float(self.strat.get("sl_pct_loss",  0.15))
            hard_sl  = float(self.strat.get("hard_sl_pct",  0.25))

            if move_pct >= tp:
                pnl = t.net_shares * cur_poly - t.raw_shares * t.entry_price
                ms.sl_strikes = 0
                return True, "TAKE_PROFIT", round(pnl, 4), cur_poly

            if move_pct <= -hard_sl:
                pnl = t.net_shares * cur_poly - t.raw_shares * t.entry_price
                ms.sl_strikes = 0
                return True, "STOP_LOSS", round(pnl, 4), cur_poly

            if move_pct <= -sl:
                ms.sl_strikes += 1
                if ms.sl_strikes >= 3:
                    pnl = t.net_shares * cur_poly - t.raw_shares * t.entry_price
                    ms.sl_strikes = 0
                    return True, "STOP_LOSS", round(pnl, 4), cur_poly
            else:
                ms.sl_strikes = 0

        elif t.side == "NO":
            # NO pozisyonunda stop-loss yok (wick koruması)
            tp_no = 0.18
            if move_pct >= tp_no:
                pnl = t.net_shares * cur_poly - t.raw_shares * t.entry_price
                return True, "TAKE_PROFIT (NO)", round(pnl, 4), cur_poly

        return False, "", 0.0, 0.0

    # ------------------------------------------------------------------
    # Analiz & emir
    # ------------------------------------------------------------------

    async def _analyze(self, ms: MarketState) -> None:
        if self._daily_limit_hit:
            ms.signal = "GUNLUK LIMIT"
            return

        # Açık pozisyon çıkış kontrolü
        if ms.active_trade:
            ok, reason, pnl, exit_px = self._check_exit(ms)
            if ok:
                await self.orders.place_sell(ms.active_trade.token_id, exit_px, ms.active_trade.net_shares)
                self._record(ms, pnl, reason, exit_px)
                self._log(f"CIKIS | {reason} | PnL: ${pnl:+.3f}", "TRADE")
                ms.has_traded = True
            return

        # Zaten işlem yapıldıysa tekrar girme
        if ms.has_traded:
            return

        ms.signal = self._signal(ms)
        if "UP" not in ms.signal and "DN" not in ms.signal:
            return

        # Pozisyon limiti
        max_pos = int(self.risk.get("max_open_positions", 2))
        if self._open_positions >= max_pos:
            ms.signal = "POS DOLU"
            return

        side     = "YES" if "UP" in ms.signal else "NO"
        token_id = ms.yes_id if side == "YES" else ms.no_id
        entry    = ms.best_ask if side == "YES" else 1.0 - ms.best_bid

        min_e = float(self.strat.get("min_entry_price", 0.52))
        max_e = float(self.strat.get("max_entry_yes" if side == "YES" else "max_entry_no", 0.87))
        if not (min_e <= entry <= max_e) or not token_id:
            return

        # FOK cooldown
        now_ts = time.time()
        fok_cd = float(self.strat.get("fok_cooldown", 20))
        if now_ts - ms.last_buy_ts < fok_cd:
            return

        stake      = float(self.risk["stake_usd"])
        _, raw_sh  = _safe_amounts(entry, stake)
        net_sh     = raw_sh - _fee_shares(entry, raw_sh)

        ms.last_buy_ts = now_ts
        oid = await self.orders.place_buy(token_id, entry, stake)

        if oid and not oid.startswith("ERR:"):
            ms.active_trade = Trade(
                market_id  = ms.mid,
                token_id   = token_id,
                side       = side,
                entry_price= entry,
                raw_shares = raw_sh,
                net_shares = net_sh,
                stake      = stake,
                entry_btc_px = self.btc_chainlink,
                order_id   = oid,
            )
            mode = "PAPER" if not self.live_mode else "LIVE"
            self._log(
                f"SNIPE ({side}) | {entry:.3f} | BTC ${self.btc_chainlink:,.0f} | {oid}",
                mode,
            )
        else:
            self._log(f"FOK iptal (cd:{fok_cd:.0f}s) | {oid}", "WARNING")

    # ------------------------------------------------------------------
    # Kapanış
    # ------------------------------------------------------------------

    async def _settle(self, mid: str) -> None:
        ms = self.markets.get(mid)
        if not ms:
            return

        if ms.active_trade:
            t       = ms.active_trade
            btc_now = self.btc_chainlink
            btc_ref = ms.ref_chainlink or t.entry_btc_px

            if btc_ref > 0 and btc_now > 0:
                btc_up = btc_now >= btc_ref
                won    = (btc_up and t.side == "YES") or (not btc_up and t.side == "NO")
            else:
                cur_poly = _clamp_price(ms.best_bid if t.side == "YES" else 1.0 - ms.best_ask)
                won      = cur_poly > t.entry_price

            if won:
                pnl    = round(t.net_shares * 1.0 - t.raw_shares * t.entry_price, 4)
                reason = "SETTL_WIN"
            else:
                pnl    = round(-(t.raw_shares * t.entry_price), 4)
                reason = "SETTL_LOSS"

            exit_px = 1.0 if won else 0.0
            self._record(ms, pnl, reason, exit_px)
            self._log(
                f"HAKEM ({t.side}) | {reason} | PnL: ${pnl:+.3f}",
                "TRADE" if won else "WARNING",
            )

        del self.markets[mid]

    # ------------------------------------------------------------------
    # İşlem kaydı
    # ------------------------------------------------------------------

    def _record(self, ms: MarketState, pnl: float, rtype: str, exit_px: float) -> None:
        t = ms.active_trade
        if not t:
            return

        self.session_pnl += pnl
        self.daily_pnl   += pnl
        self.trades += 1
        if pnl > 0:
            self.wins += 1
        else:
            self.losses += 1

        row = {
            "ts":           datetime.now(timezone.utc).isoformat(),
            "mid":          ms.mid,
            "side":         t.side,
            "horizon_min":  ms.horizon_min,
            "entry_price":  t.entry_price,
            "exit_price":   exit_px,
            "raw_shares":   t.raw_shares,
            "net_shares":   t.net_shares,
            "pnl":          pnl,
            "result":       rtype,
            "btc_ref":      ms.ref_chainlink,
            "btc_entry":    t.entry_btc_px,
        }
        try:
            mem = self.cfg.get("memory_file", "trades_paper.jsonl")
            with open(mem, "a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
        except Exception:
            pass

        ms.active_trade = None

    # ------------------------------------------------------------------
    # Ekran
    # ------------------------------------------------------------------

    def _render(self) -> Layout:
        wr = (self.wins / self.trades * 100) if self.trades else 0.0

        # Başlık
        hdr = (
            f"[bold white]KRAJEKIS V16.0[/] "
            f"{'[bold red]CANLI[/bold red]' if self.live_mode else '[dim]PAPER[/dim]'} | "
            f"ChainLink:[bold green]${self.btc_chainlink:,.0f}[/] "
            f"(Bin:[cyan]${self.btc_binance:,.0f}[/cyan]) | "
            f"PnL:[{'green' if self.session_pnl >= 0 else 'red'}]${self.session_pnl:+.3f}[/] | "
            f"W/L:[green]{self.wins}[/]/[red]{self.losses}[/]({wr:.0f}%)"
        )

        lay = Layout()
        lay.split_column(
            Layout(name="h", size=3),
            Layout(name="b", ratio=1),
            Layout(name="l", size=14),
        )
        lay["b"].split_row(Layout(name="mt", ratio=4), Layout(name="s", size=38))
        lay["h"].update(Panel(Text.from_markup(hdr), border_style="cyan"))

        # Piyasa tablosu
        tbl = Table(box=box.MINIMAL_DOUBLE_HEAD, expand=True)
        for col in ["Zaman", "BTC Pazar", "YES", "NO", "Sinyal", "Pozisyon"]:
            tbl.add_column(col, no_wrap=True)

        for ms in sorted(self.markets.values(), key=lambda x: x.secs_left):
            if ms.secs_left <= 0:
                continue
            pos_str    = ""
            row_style  = "white"
            if ms.active_trade:
                t         = ms.active_trade
                pos_str   = f"{t.side}@{t.entry_price:.2f}"
                row_style = "green"
            elif ms.has_traded:
                pos_str   = "[dim]KAPANDI[/dim]"
                row_style = "dim"

            time_str = f"[{ms.horizon_min}m] {int(ms.mins_left)}m {int(ms.secs_left % 60)}s"
            tbl.add_row(
                time_str, ms.short_name,
                f"{ms.best_ask:.2f}", f"{1.0 - ms.best_bid:.2f}",
                ms.signal, pos_str,
                style=row_style,
            )

        lay["mt"].update(Panel(tbl, title="Aktif Radar Pazarlari", border_style="cyan"))

        # Sağ panel
        ta    = self.ta
        vwap  = ta.get("vwap", 0.0)
        rsi   = ta.get("rsi",  0.0)
        stat  = (
            f"[bold cyan]HYBRID BEYIN (V16.0)[/bold cyan]\n"
            f"  [green]YES:[/green] Erken Gir / TA {self.strat.get('min_signal_score',3)}/4\n"
            f"  [red]NO:[/red]  Late Gir / No SL\n\n"
            f"[bold cyan]ORACLE[/bold cyan]\n"
            f"  Chainlink: ${self.btc_chainlink:,.0f}\n"
            f"  Binance:   ${self.btc_binance:,.0f}\n"
            f"  Fark: ${abs(self.btc_chainlink - self.btc_binance):,.0f}\n"
            f"  VWAP: ${vwap:,.0f}  RSI: {rsi:.1f}\n\n"
            f"[bold cyan]KASA[/bold cyan]\n"
            f"  Gunluk: [{'green' if self.daily_pnl>=0 else 'red'}]${self.daily_pnl:+.3f}[/]\n"
            f"  Toplam: {self.trades} islem | WR: {wr:.1f}%"
        )
        lay["s"].update(Panel(Text.from_markup(stat), title="Analiz", border_style="yellow"))
        lay["l"].update(Panel(
            Text.from_markup("\n".join(self.logs)),
            title="Sistem Logu",
            border_style="cyan",
        ))
        return lay

    # ------------------------------------------------------------------
    # Ana döngü
    # ------------------------------------------------------------------

    async def run(self) -> None:
        ssl_ctx   = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode    = ssl.CERT_NONE

        connector = aiohttp.TCPConnector(
            family=socket.AF_INET,
            ssl=ssl_ctx,
            limit=20,
            resolver=aiohttp.ThreadedResolver(),
        )

        async with aiohttp.ClientSession(connector=connector) as sess:
            if self.live_mode:
                appr = await self.orders.approve_collateral()
                self._log(f"Onay Kontrolu: {appr}", "INFO")

            mode = "PAPER" if not self.live_mode else "LIVE"
            self._log("V16.0 basliyor...", mode)

            with Live(self._render(), refresh_per_second=2, screen=True) as live:
                cycle = 0
                while self._running:
                    self._check_daily_reset()

                    if cycle % 15 == 0:
                        await self._update_markets(sess)

                    await self._update_prices(sess)

                    for ms in list(self.markets.values()):
                        if ms.secs_left > 0:
                            await self._analyze(ms)
                        else:
                            await self._settle(ms.mid)

                    live.update(self._render())
                    await asyncio.sleep(2)
                    cycle += 1

        if self.live_mode:
            await self.orders.cancel_all()


# ---------------------------------------------------------------------------
# Giriş
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else "config.json"
    try:
        cfg = load_config(cfg_path)
    except FileNotFoundError as e:
        print(e)
        sys.exit(1)

    bot = SniperBot(cfg)

    if bot.live_mode:
        ans = input("CANLI PARA modu! Devam etmek istiyor musun? [evet/hayir]: ").strip().lower()
        if ans not in ("evet", "e", "yes", "y"):
            print("Iptal edildi.")
            sys.exit(0)

    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        pass
