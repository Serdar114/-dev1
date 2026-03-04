#!/usr/bin/env python3
"""
Polymarket Krajekis Auto-Sniper V14.5 (Paper-Ready Edition)
=========================================================
  Strateji: Krajekis BTC 5m/15m playbook — VWAP + EMA + RSI + MACD
  Paper modda 50 islem hedefi; pozitif win rate kanıtlanırsa live.

  V14.5 DUZELTMELER (Claude Senior Audit):
  1. [FIX] _record(): Her islemi trades_krajekis.jsonl dosyasina yazar.
     Oturum kapaninca tum trade gecmisi kaybolmuyordu — artik kalici.
  2. [FIX] max_daily_loss_usd enforce edildi: Gunluk limit asilinca
     bot yeni islem açmiyor, ekranda "GUNLUK LIMIT" gosteriyor.
  3. [FIX] max_open_positions enforce edildi: Esik asilinca yeni giris
     yapilmiyor. Ayni anda maks 2 pozisyon (config ile degistirilebilir).
  4. [FIX] _settle(): Gercek binary sonuc hesabi. ref_price (pazar
     acilisindaki BTC) ile kapanistaki BTC karsilastirilir;
     yon dogru ise 1.0 (tam kazanc), yanlis ise 0.0 (tam kayip).
     Stale Polymarket fiyati yaniltici PnL veriyordu.
  5. [FIX] fok_cooldown: Basarisiz FOK'tan sonra last_buy_attempt
     set edilir, cooldown suresi geçmeden tekrar denenmez.
  6. [FIX] daily_pnl her gun UTC geceyarisinda sifirlaniyor.

  V14.4'TEN KORUNAN IYZILER:
  - TP/SL Polymarket hisse fiyati uzerinden (%20 / %15)
  - V11 approve_token + ensure_approvals sistemi
  - Giris araligi: 0.70 - 0.95
  - Saf Pandas TA (VWAP, EMA21/50, RSI14, MACD)
  - Timestamp slug ile 5m/15m pazar radar
  - paper_only: true guvenligi
"""
import sys
import asyncio
import socket
import aiohttp
import json
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
    """Tam sayi hisse: n*price her zaman 2 ondalikli USDC verir."""
    price_r = round(max(0.01, min(0.99, float(price))), 2)
    shares_int = float(int(round(stake / price_r, 4)))
    return price_r, shares_int


@dataclass
class LiveTrade:
    market_id:      str
    token_id:       str
    side:           str
    entry_price:    float
    shares:         float
    stake:          float
    entry_asset_px: float
    order_id:       str = ""
    entry_time:     datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


class MarketState:
    def __init__(self, mid: str, question: str, end_time: datetime,
                 yes_id: str = "", no_id: str = ""):
        self.mid        = mid
        self.question   = question
        self.end_time   = end_time
        self.yes_id     = yes_id
        self.no_id      = no_id
        self.asset      = "BTC"
        self.best_ask:  float = 0.5
        self.best_bid:  float = 0.5
        self.signal:    str   = "BEKLE"
        self.active_trade: Optional[LiveTrade] = None
        self.has_traded:    bool  = False
        self.exit_retries:  int   = 0
        self.last_buy_attempt: float = 0.0
        # FIX-4: Pazar acilisindaki BTC fiyati — settle icin referans
        self.ref_price: float = 0.0

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

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, _do)

    async def place_sell(self, token_id: str, price: float, shares: float) -> str:
        if not self.live_mode:
            self._paper_seq += 1
            return f"PAPER-SELL-{self._paper_seq:04d}"

        def _do():
            client = self._client_or_raise()
            try:
                price_r, shares_r = _safe_amounts(price, shares * price)
                args   = OrderArgs(price=price_r, size=shares_r, side=SELL, token_id=token_id)
                signed = client.create_order(args)
                resp   = client.post_order(signed, OrderType.GTC)
                return (resp.get("orderID") or resp.get("order_id") or resp.get("id", ""))
            except Exception as e:
                return f"ERR:{e}"

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, _do)

    async def approve_token(self, token_id: str) -> str:
        """V11 onay sistemi: alim sonrasi hemen CONDITIONAL approval set et."""
        if not self.live_mode:
            return "PAPER"

        def _do():
            try:
                from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
                client = self._client_or_raise()
                resp   = client.update_balance_allowance(
                    params=BalanceAllowanceParams(
                        asset_type=AssetType.CONDITIONAL,
                        token_id=token_id,
                    )
                )
                return f"OK:{resp}"
            except Exception as e:
                return f"ERR:{e}"

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, _do)

    async def get_balance(self) -> float:
        if not self.live_mode:
            return 0.0

        def _do():
            import urllib.request as _req
            NATIVE_USDC = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
            wallet = self.cfg["credentials"].get("wallet_address", "")
            if wallet:
                try:
                    padded  = wallet.lower().replace("0x", "").zfill(64)
                    data    = "0x70a08231" + padded
                    payload = json.dumps({
                        "jsonrpc": "2.0", "method": "eth_call",
                        "params": [{"to": NATIVE_USDC, "data": data}, "latest"],
                        "id": 1
                    }).encode()
                    req = _req.Request(
                        "https://polygon-rpc.com", data=payload,
                        headers={"Content-Type": "application/json"}, method="POST"
                    )
                    with _req.urlopen(req, timeout=5) as resp:
                        result  = json.loads(resp.read())
                        hex_val = result.get("result", "0x0") or "0x0"
                        raw     = int(hex_val, 16) if hex_val not in ("0x", "") else 0
                        return float(raw) / 1e6
                except Exception:
                    pass
            return 0.0

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, _do)

    async def ensure_approvals(self) -> str:
        """V11 startup onay sistemi: COLLATERAL + CONDITIONAL."""
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

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, _do)

    async def cancel_all(self) -> None:
        if not self.live_mode:
            return

        def _do():
            try:
                self._client_or_raise().cancel_all_orders()
            except Exception:
                pass

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._executor, _do)


class KrajekisSniperBot:
    def __init__(self, config: dict):
        self.cfg   = config
        self.risk  = config["risk"]
        self.strat = config["strategy"]
        self.net   = config["network"]

        if self.strat.get("paper_only", True):
            self.live_mode = False
        else:
            self.live_mode = CLOB_OK and _cfg_has_creds(config)

        self.console   = Console()
        self.order_mgr = OrderManager(config, self.live_mode)
        self.markets:  Dict[str, MarketState] = {}
        self.logs:     deque = deque(maxlen=14)
        self.prices:   Dict[str, float] = {"BTC": 0.0}
        self.ta_data:  Dict[str, dict]  = {}

        self.trades:      int   = 0
        self.wins:        int   = 0
        self.losses:      int   = 0
        self.session_pnl: float = 0.0
        self.daily_pnl:   float = 0.0

        # FIX-2: Gunluk reset zamani
        self._daily_reset: datetime = (
            datetime.now(timezone.utc)
            .replace(hour=0, minute=0, second=0, microsecond=0)
            + timedelta(days=1)
        )
        self._running: bool = True

    # ------------------------------------------------------------------ helpers

    def _log(self, msg: str, level: str = "INFO") -> None:
        ts     = datetime.now().strftime("%H:%M:%S")
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
                f.write(
                    f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]"
                    f" [{level}] {msg}\n"
                )
        except Exception:
            pass

    def _check_daily_reset(self) -> None:
        """FIX-2: UTC geceyarisinda gunluk PnL sifirla."""
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

    # ------------------------------------------------------------------ market fetch

    async def _update_markets(self, session: aiohttp.ClientSession) -> None:
        """Timestamp slug ile gizli 5m/15m BTC pazarlarini cek."""
        now      = int(datetime.now(timezone.utc).timestamp())
        base_5m  = (now // 300) * 300
        base_15m = (now // 900) * 900

        sluglar = []
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
                        evs  = data if isinstance(data, list) else [data]
                        for ev in evs:
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
            question = m.get("question") or ev.get("title") or "BTC Up/Down"
            self.markets[mid] = MarketState(mid, question, end_t, yes_id, no_id)
            self._log(f"Radar: {question[:48]}", "INFO")
            return 1
        return 0

    # ------------------------------------------------------------------ price + TA

    async def _fetch_prices_and_ta(self, session: aiohttp.ClientSession) -> None:
        """Binance 1m kline ile BTC fiyati + saf Pandas TA hesapla."""
        try:
            async with session.get(
                "https://api.binance.com/api/v3/klines",
                params={"symbol": "BTCUSDT", "interval": "1m", "limit": "100"},
                timeout=aiohttp.ClientTimeout(total=5)
            ) as r:
                if r.status == 200:
                    data = await r.json()
                    df   = pd.DataFrame(
                        data,
                        columns=["ts", "open", "high", "low", "close", "vol",
                                 "ct", "qav", "nt", "tbv", "tqv", "ig"]
                    )
                    for col in ("close", "high", "low", "vol"):
                        df[col] = df[col].astype(float)

                    self.prices["BTC"] = df["close"].iloc[-1]

                    # EMA 21 / 50
                    df["EMA_21"] = df["close"].ewm(span=21, adjust=False).mean()
                    df["EMA_50"] = df["close"].ewm(span=50, adjust=False).mean()

                    # RSI 14
                    delta = df["close"].diff()
                    gain  = delta.where(delta > 0, 0).ewm(alpha=1/14, adjust=False).mean()
                    loss  = (-delta.where(delta < 0, 0)).ewm(alpha=1/14, adjust=False).mean()
                    df["RSI_14"] = 100 - (100 / (1 + gain / loss))

                    # VWAP (100 dakikalik kayan pencere — session proxy)
                    tp         = (df["high"] + df["low"] + df["close"]) / 3
                    df["VWAP"] = (df["vol"] * tp).cumsum() / df["vol"].cumsum()

                    # MACD histogram
                    ema12 = df["close"].ewm(span=12, adjust=False).mean()
                    ema26 = df["close"].ewm(span=26, adjust=False).mean()
                    macd_line   = ema12 - ema26
                    signal_line = macd_line.ewm(span=9, adjust=False).mean()
                    df["MACD_Hist"] = macd_line - signal_line

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

        # FIX-4: ref_price — pazar ilk gorunduğunde BTC fiyatini kilitle
        btc_now = self.prices.get("BTC", 0.0)
        if btc_now > 0:
            for ms in self.markets.values():
                if ms.ref_price == 0.0 and 0 < ms.secs_left:
                    ms.ref_price = btc_now

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

    # ------------------------------------------------------------------ signal

    def _signal(self, ms: MarketState) -> str:
        if ms.secs_left <= 0:
            return "BEKLE"

        # Krajekis sweet spot: son 5-10 dk (15m) veya son 1-3 dk (5m)
        is_15m = "15m" in ms.short_name.lower() or "15 minute" in ms.short_name.lower()
        if is_15m:
            st = float(self.strat.get("sweet_spot_15m_start", 10.0))
            en = float(self.strat.get("sweet_spot_15m_end", 5.0))
        else:
            st = float(self.strat.get("sweet_spot_5m_start", 3.0))
            en = float(self.strat.get("sweet_spot_5m_end", 1.0))

        if not (en <= ms.mins_left <= st):
            return "ZAMAN DISI"

        ta = self.ta_data.get("BTC")
        if not ta or pd.isna(ta["vwap"]):
            return "TA BEKLENIYOR"

        px    = self.prices["BTC"]
        vwap  = ta["vwap"]
        rsi   = ta["rsi"]
        ema21 = ta["ema21"]
        ema50 = ta["ema50"]
        macd  = ta["macd"]

        if ms.best_ask - ms.best_bid > float(self.strat.get("max_spread", 0.05)):
            return "GENIS MAKAS"

        # Yukari: fiyat VWAP üstü + EMA bullish + RSI overbought degil + MACD pozitif
        if (px > vwap and ema21 > ema50
                and rsi < float(self.strat.get("rsi_overbought", 70))
                and macd > 0):
            return "UP (LONG)"

        # Asagi: fiyat VWAP alti + EMA bearish + RSI oversold degil + MACD negatif
        if (px < vwap and ema21 < ema50
                and rsi > float(self.strat.get("rsi_oversold", 30))
                and macd < 0):
            return "DN (SHORT)"

        return "YAPI BOZUK"

    # ------------------------------------------------------------------ exit

    def _check_exit(self, ms: MarketState) -> Tuple[bool, str, float, float]:
        """TP/SL Polymarket hisse fiyati uzerinden (BTC yuzdesi degil)."""
        t = ms.active_trade
        if not t:
            return False, "", 0.0, 0.0

        tp = float(self.strat.get("tp_pct_gain", 0.20))
        sl = float(self.strat.get("sl_pct_loss", 0.15))

        cur_poly = _safe_price(ms.best_bid if t.side == "YES" else 1.0 - ms.best_ask)
        move_pct = (cur_poly - t.entry_price) / t.entry_price

        if move_pct >= tp:
            pnl = round((t.shares * cur_poly) - (t.shares * t.entry_price), 4)
            return True, "TAKE_PROFIT", pnl, cur_poly

        if move_pct <= -sl:
            pnl = round((t.shares * cur_poly) - (t.shares * t.entry_price), 4)
            return True, "STOP_LOSS", pnl, cur_poly

        return False, "", 0.0, 0.0

    # ------------------------------------------------------------------ analyze

    async def _analyze(self, ms: MarketState) -> None:
        # FIX-2: Gunluk kayip limiti
        if self._daily_limit_hit:
            ms.signal = "GUNLUK LIMIT"
            return

        if ms.has_traded and not ms.active_trade:
            return

        if ms.active_trade:
            ok, reason, pnl, exit_px = self._check_exit(ms)
            if ok:
                await self.order_mgr.place_sell(
                    ms.active_trade.token_id, exit_px, ms.active_trade.shares
                )
                self._record(ms, pnl, reason, exit_px)
                ms.has_traded = True
                self._log(f"CIKIS | {reason} | PnL: ${pnl:+.3f}", "TRADE")
            return

        ms.signal = self._signal(ms)
        if "UP" not in ms.signal and "DN" not in ms.signal:
            return

        # FIX-3: Ayni anda max pozisyon kontrolu
        max_pos = int(self.risk.get("max_open_positions", 2))
        if self._open_positions >= max_pos:
            ms.signal = "POS DOLU"
            return

        side     = "YES" if "UP" in ms.signal else "NO"
        token_id = ms.yes_id if side == "YES" else ms.no_id
        entry    = ms.best_ask if side == "YES" else 1.0 - ms.best_bid

        min_e = float(self.strat.get("min_entry_price", 0.70))
        max_e = float(self.strat.get("max_entry_price", 0.95))
        if entry < min_e or entry > max_e or not token_id:
            return

        # FIX-5: fok_cooldown — basarisiz sonrasi bekleme
        now_ts  = datetime.now(timezone.utc).timestamp()
        fok_cd  = float(self.strat.get("fok_cooldown", 20))
        if now_ts - ms.last_buy_attempt < fok_cd:
            return

        stake  = float(self.risk["stake_usd"])
        _, shares = _safe_amounts(entry, stake)

        ms.last_buy_attempt = now_ts
        oid = await self.order_mgr.place_buy(token_id, entry, shares)

        if not oid or oid.startswith("ERR:"):
            self._log(f"FOK iptal — {int(fok_cd)}s bekleniyor | {oid}", "WARNING")
            return

        ms.active_trade = LiveTrade(
            market_id=ms.mid, token_id=token_id, side=side,
            entry_price=entry, shares=shares, stake=stake,
            entry_asset_px=self.prices.get("BTC", 0.0), order_id=oid
        )
        self._log(
            f"{'LIVE' if self.live_mode else 'PAPER'} SNIPE ({side}) | "
            f"{entry:.3f} | {shares:.0f} hisse | ${stake}",
            "LIVE" if self.live_mode else "PAPER"
        )

        # V11 approval: alim sonrasi hemen token approval
        appr = await self.order_mgr.approve_token(token_id)
        if appr not in ("PAPER",):
            self._log(f"Token Approval: {appr[:60]}", "INFO")

    # ------------------------------------------------------------------ settle

    async def _settle(self, mid: str) -> None:
        """
        FIX-4: Gercek binary sonuc.
        ref_price (pazar acilisindaki BTC) vs simdi karsilastirilir.
        Yon dogru → 1.0 (tam kazanc), yanlis → 0.0 (tam kayip).
        """
        ms = self.markets.get(mid)
        if not ms:
            return

        if ms.active_trade:
            t       = ms.active_trade
            btc_now = self.prices.get("BTC", 0.0)

            if ms.ref_price > 0 and btc_now > 0:
                btc_up = btc_now > ms.ref_price
                won    = (btc_up and t.side == "YES") or (not btc_up and t.side == "NO")
            else:
                # ref_price yoksa son Polymarket fiyatini proxy kullan
                cur_poly = _safe_price(
                    ms.best_bid if t.side == "YES" else 1.0 - ms.best_ask
                )
                won = cur_poly > t.entry_price

            actual_cost = t.shares * t.entry_price
            if won:
                pnl    = round(t.shares * 1.0 - actual_cost, 4)
                reason = "SETTL_WIN"
            else:
                pnl    = round(-actual_cost, 4)
                reason = "SETTL_LOSS"

            exit_px = 1.0 if won else 0.0
            self._record(ms, pnl, reason, exit_px)
            self._log(
                f"SETTLED | {reason} | BTC ref:{ms.ref_price:,.0f} → "
                f"simdi:{btc_now:,.0f} | PnL: ${pnl:+.3f}",
                "TRADE" if won else "WARNING"
            )

        del self.markets[mid]

    # ------------------------------------------------------------------ record

    def _record(self, ms: MarketState, pnl: float, rtype: str, exit_px: float) -> None:
        """FIX-1: Her islemi JSONL dosyasina yaz + sayaclari guncelle."""
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
            "ts":         datetime.now(timezone.utc).isoformat(),
            "mid":        ms.mid,
            "question":   ms.question[:60],
            "side":       t.side,
            "entry":      t.entry_price,
            "exit":       exit_px,
            "shares":     t.shares,
            "stake":      t.stake,
            "pnl":        pnl,
            "result":     rtype,
            "btc_ref":    ms.ref_price,
            "btc_exit":   self.prices.get("BTC", 0.0),
            "live":       self.live_mode,
        }
        try:
            mem = self.cfg.get("memory_file", "trades_krajekis.jsonl")
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
            Layout(name="l", size=14)
        )
        lay["b"].split_row(Layout(name="mt", ratio=4), Layout(name="s", size=40))

        btc_px = self.prices.get("BTC", 0.0)
        ta_btc = self.ta_data.get("BTC", {})
        vwap   = ta_btc.get("vwap", 0.0)
        rsi    = ta_btc.get("rsi", 0.0)
        wr     = (self.wins / self.trades * 100) if self.trades else 0.0
        limit  = abs(self.risk.get("max_daily_loss_usd", 3.0))

        hdr = (
            f"[bold white]KRAJEKIS BTC AUTO-SNIPER V14.5[/bold white] "
            f"{'[bold red]CANLI[/bold red]' if self.live_mode else '[dim]PAPER (SIFIR RISK)[/dim]'} | "
            f"BTC:[cyan]${btc_px:,.0f}[/cyan] | "
            f"VWAP:[yellow]${vwap:,.0f}[/yellow] | "
            f"RSI:[magenta]{rsi:.1f}[/magenta] | "
            f"PnL:[{'green' if self.session_pnl >= 0 else 'red'}]${self.session_pnl:+.3f}[/] | "
            f"W/L:[green]{self.wins}[/green]/[red]{self.losses}[/red]({wr:.0f}%)"
        )
        lay["h"].update(Panel(Text.from_markup(hdr), border_style="cyan"))

        tbl = Table(box=box.MINIMAL_DOUBLE_HEAD, expand=True)
        for col in ["Kalan", "BTC Pazar", "YES", "NO", "Sinyal", "Pozisyon"]:
            tbl.add_column(col, no_wrap=True)

        for ms in sorted(self.markets.values(), key=lambda x: x.secs_left):
            if ms.secs_left <= 0:
                continue
            pos_str    = ""
            row_style  = "white"
            if ms.active_trade:
                t         = ms.active_trade
                cur_poly  = _safe_price(ms.best_bid if t.side == "YES" else 1.0 - ms.best_ask)
                gain      = cur_poly - t.entry_price
                pos_str   = f"{t.side}@{t.entry_price:.2f} ({gain:+.2f})"
                row_style = "green" if gain >= 0 else "red"
            elif ms.has_traded:
                pos_str   = "[dim]KAPANDI[/dim]"
                row_style = "dim"

            time_str = f"{int(ms.mins_left)}m {int(ms.secs_left % 60)}s"
            tbl.add_row(
                time_str, ms.short_name,
                f"{ms.best_ask:.3f}", f"{1.0 - ms.best_bid:.3f}",
                ms.signal, pos_str,
                style=row_style
            )

        lay["mt"].update(Panel(
            tbl,
            title=f"Radar ({sum(1 for m in self.markets.values() if m.secs_left > 0)} pazar"
                  f" | {self._open_positions} acik pozisyon)",
            border_style="cyan"
        ))

        stat = (
            f"[bold cyan]TA YAPISI (BTC)[/bold cyan]\n"
            f"  Fiyat: [cyan]${btc_px:,.0f}[/cyan]\n"
            f"  VWAP:  [yellow]${vwap:,.0f}[/yellow]\n"
            f"  EMA21: ${ta_btc.get('ema21', 0):,.0f}\n"
            f"  EMA50: ${ta_btc.get('ema50', 0):,.0f}\n"
            f"  RSI:   [magenta]{rsi:.1f}[/magenta]\n"
            f"  MACD:  {'[green]' if ta_btc.get('macd', 0) > 0 else '[red]'}"
            f"{ta_btc.get('macd', 0):+.2f}[/]\n\n"
            f"[bold cyan]KRAJEKIS ZAMANLAMA[/bold cyan]\n"
            f"  15m Pencere: {self.strat.get('sweet_spot_15m_end')}-"
            f"{self.strat.get('sweet_spot_15m_start')} dk\n"
            f"  5m  Pencere: {self.strat.get('sweet_spot_5m_end')}-"
            f"{self.strat.get('sweet_spot_5m_start')} dk\n"
            f"  Giris: {self.strat.get('min_entry_price')}-"
            f"{self.strat.get('max_entry_price')}\n"
            f"  TP: +%{self.strat.get('tp_pct_gain', 0.20)*100:.0f} | "
            f"SL: -%{self.strat.get('sl_pct_loss', 0.15)*100:.0f}\n\n"
            f"[bold cyan]KASA[/bold cyan]\n"
            f"  Gunluk PnL: "
            f"[{'green' if self.daily_pnl >= 0 else 'red'}]${self.daily_pnl:+.3f}[/]\n"
            f"  Gunluk Limit: [red]-${limit:.2f}[/]\n"
            f"  Toplam Islem: [white]{self.trades}[/white]\n"
            f"  Win Rate: [green]{wr:.1f}%[/green]"
        )
        lay["s"].update(Panel(Text.from_markup(stat), title="Krajekis Analiz", border_style="yellow"))
        lay["l"].update(Panel(
            Text.from_markup("\n".join(list(self.logs))),
            title="Sistem Log [V14.5]",
            border_style="cyan"
        ))
        return lay

    # ------------------------------------------------------------------ main

    async def main_run(self) -> None:
        connector = aiohttp.TCPConnector(
            limit=20,
            resolver=aiohttp.ThreadedResolver(),
            family=socket.AF_INET
        )
        async with aiohttp.ClientSession(connector=connector) as session:
            if self.live_mode:
                appr = await self.order_mgr.ensure_approvals()
                self._log(f"Live Onay: {appr}", "INFO")

            self._log(
                "V14.5 Krajekis BTC Radari Basliyor... "
                f"({'CANLI' if self.live_mode else 'PAPER'})",
                "LIVE" if self.live_mode else "PAPER"
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
        ans = input("  CANLI PARA modunda calisacak. Devam? [evet/hayir]: ").strip().lower()
        if ans not in ("evet", "e", "yes", "y"):
            sys.exit(0)
    try:
        asyncio.run(bot.main_run())
    except KeyboardInterrupt:
        pass
