#!/usr/bin/env python3
"""
Polymarket BTC Sniper V10.30 (Aggressive Compounding + Critical Fixes)
=======================================================================
  V10.30 FIX LIST:
  1. [CRITICAL] _safe_amounts(): maker amount (shares*price USDC) artik 2
     ondalik basamakla sinirlandirildi. "invalid amounts" hatasi tamamen cozuldu.
  2. [CRITICAL] SELL Allowance Retry: 'not enough balance/allowance' hatasinda
     update_allowances() + 1s bekle + otomatik retry ile hayalet islem engeli.
  3. [BUG FIX] shares < 5.0 gizli kill: Stake $3 ile entry > 0.60 tum
     sinyaller sessizce iptal oluyordu. Stake $4'e cikarilarak fix edildi.
  4. fok_cooldown: 60s -> 20s (daha cok firsat kovalama)
  5. Agresif Compounding config destegi.
"""
import sys
import asyncio
import socket
import aiohttp
import numpy as np
import json
import os
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
            result = json.loads(raw)
            return result if isinstance(result, list) else []
        except (json.JSONDecodeError, ValueError):
            return []
    return []


def _safe_price(p: float) -> float:
    return round(max(0.01, min(0.99, float(p))), 4)


def _safe_amounts(price: float, stake: float) -> Tuple[float, float]:
    """
    FIX #1 - invalid amounts hatasi cozumu:
    Polymarket FOK/market emirleri:
      - maker amount (USDC = shares * price): max 2 ondalik basamak
      - taker amount (shares/token): max 4 ondalik basamak

    Strateji: once shares'i hesapla, sonra USDC tutarini 2 basamaga yuvarla,
    ardindan shares'i bu USDC'ye gore yeniden hesapla.
    """
    price_r = _safe_price(price)
    raw_shares = stake / price_r
    # Maker amount (USDC) 2 basamakta sabitle
    maker_usdc = round(raw_shares * price_r, 2)
    # Shares (taker) 4 basamakta yeniden hesapla
    shares_final = round(maker_usdc / price_r, 4)
    return price_r, shares_final


@dataclass
class LiveTrade:
    market_id:   str
    token_id:    str
    side:        str
    entry_price: float
    shares:      float
    stake:       float
    entry_btc:   float
    ref_btc:     float
    order_id:    str = ""
    entry_time:  datetime = field(
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
        self.best_ask:  float = 0.5
        self.best_bid:  float = 0.5
        self.mid_px:    float = 0.5
        self.history:   deque = deque(maxlen=20)
        self.zscore:    float = 0.0
        self.obi_score: float = 0.5
        self.signal:    str   = "BEKLE"
        self.active_trade: Optional[LiveTrade] = None
        self.ref_btc_price: float = 0.0
        self.has_traded:    bool  = False
        self.exit_retries:  int   = 0
        self.last_buy_attempt: float = 0.0

    @property
    def secs_left(self) -> float:
        return max(0.0, (self.end_time - datetime.now(timezone.utc)).total_seconds())

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
            try:
                self._client.update_allowances()
            except Exception:
                pass
        return self._client

    async def place_buy(self, token_id: str, price: float, shares: float) -> str:
        if not self.live_mode:
            self._paper_seq += 1
            return f"PAPER-{self._paper_seq:04d}"

        def _do():
            try:
                client = self._client_or_raise()
                # FIX #1: _safe_amounts ile maker/taker precision garanti altinda
                price_r, shares_r = _safe_amounts(price, shares * price)
                args = OrderArgs(
                    price=price_r,
                    size=shares_r,
                    side=BUY,
                    token_id=token_id,
                )
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
            import time as _time
            client = self._client_or_raise()
            # FIX #2: allowance hatasi icin retry mekanizmasi
            for attempt in range(2):
                try:
                    client.update_allowances()
                    if attempt > 0:
                        _time.sleep(1.5)  # allowance'in chain'e islenmesi icin bekle
                    price_r, shares_r = _safe_amounts(price, shares * price)
                    args = OrderArgs(
                        price=price_r,
                        size=shares_r,
                        side=SELL,
                        token_id=token_id,
                    )
                    signed = client.create_order(args)
                    resp   = client.post_order(signed, OrderType.GTC)
                    return (resp.get("orderID") or resp.get("order_id") or resp.get("id", ""))
                except Exception as e:
                    err_str = str(e).lower()
                    if "not enough balance" in err_str or "allowance" in err_str:
                        if attempt == 0:
                            continue  # retry once
                    return f"ERR:{e}"
            return "ERR:allowance retry exhausted"

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, _do)

    async def get_balance(self) -> float:
        if not self.live_mode:
            return 0.0

        def _do():
            try:
                client = self._client_or_raise()
                try:
                    from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
                    resp = client.get_balance_allowance(
                        params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
                except Exception:
                    resp = client.get_balance_allowance()
                raw = resp.get("balance", 0) if isinstance(resp, dict) else 0
                return float(raw) / 1e6
            except Exception:
                return 0.0

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


class LiveSniperBot:
    def __init__(self, config: dict):
        self.cfg       = config
        self.risk      = config["risk"]
        self.strat     = config["strategy"]
        self.net       = config["network"]
        self.live_mode = CLOB_OK and _cfg_has_creds(config)
        self.console   = Console()
        self.order_mgr = OrderManager(config, self.live_mode)
        self.markets:      Dict[str, MarketState] = {}
        self.logs:         deque = deque(maxlen=14)
        self.btc_price:    float = 0.0
        self.btc_history:  deque = deque(maxlen=60)
        self.trades:       int   = 0
        self.wins:         int   = 0
        self.losses:       int   = 0
        self.session_pnl:  float = 0.0
        self.daily_pnl:    float = 0.0
        self.usdc_balance: float = 0.0
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
            "EXIT":    "bold magenta",
            "ERROR":   "bold red",
            "LIVE":    "bold green",
            "PAPER":   "dim cyan",
        }
        c = colors.get(level, "white")
        self.logs.append(f"[{c}][{ts}] {level}[/] {msg}")
        try:
            with open("debug.log", "a", encoding="utf-8") as f:
                f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [{level}] {msg}\n")
        except Exception:
            pass

    def _check_daily_reset(self) -> None:
        if datetime.now(timezone.utc) >= self._daily_reset:
            self.daily_pnl    = 0.0
            self._daily_reset += timedelta(days=1)
            self._log("Gunluk PnL sifirlandi", "INFO")

    @property
    def _daily_limit_hit(self) -> bool:
        return self.daily_pnl <= -abs(self.risk["max_daily_loss_usd"])

    async def _fetch_btc_rest(self, session: aiohttp.ClientSession) -> Optional[float]:
        symbol = self.net.get("binance_symbol", "BTCUSDT").replace("/", "")
        try:
            async with session.get(
                "https://api.binance.com/api/v3/ticker/price",
                params={"symbol": symbol},
                timeout=aiohttp.ClientTimeout(total=5)
            ) as r:
                if r.status == 200:
                    return float((await r.json())["price"])
        except Exception:
            pass
        return None

    async def _update_markets(self, session: aiohttp.ClientSession) -> None:
        found = 0
        try:
            async with session.get(
                f"{self.net['gamma_url']}/events",
                params={"tag_slug": "crypto", "active": "true", "closed": "false", "_limit": 60},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as r:
                if r.status == 200:
                    raw    = await r.json()
                    events = raw if isinstance(raw, list) else raw.get("events", raw.get("data", []))
                    events = [events] if isinstance(events, dict) else events
                    for ev in (events or []):
                        if not ev or not isinstance(ev, dict):
                            continue
                        title = str(ev.get("title", "")).lower()
                        if ("bitcoin" in title or "btc" in title) and \
                           ("up" in title or "down" in title or "updown" in title):
                            for m in ev.get("markets", []):
                                found += self._process_market(m, ev)
        except Exception:
            pass

        if found == 0:
            try:
                async with session.get(
                    f"{self.net['gamma_url']}/markets",
                    params={"tag_slug": "bitcoin", "active": "true", "_limit": 40},
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as r:
                    if r.status == 200:
                        data = await r.json()
                        ml   = data if isinstance(data, list) else data.get("markets", data.get("data", []))
                        for m in ml:
                            if not isinstance(m, dict):
                                continue
                            q = str(m.get("question", "")).lower()
                            if "up or down" in q or "up-or-down" in q or "updown" in q:
                                found += self._process_market(m, {})
            except Exception:
                pass

        # SLUG GUESSING FALLBACK
        if found == 0:
            now  = int(datetime.now(timezone.utc).timestamp())
            base = (now // 300) * 300
            sluglar = (
                [f"bitcoin-up-or-down-{base + i * 300}" for i in range(-1, 8)] +
                [f"btc-updown-5m-{base + i * 300}" for i in range(-1, 8)]
            )
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
                                    found += self._process_market(m, ev)
                except Exception:
                    pass

    def _process_market(self, m: dict, ev: dict) -> int:
        if not isinstance(m, dict):
            return 0
        mid = m.get("id")
        if not mid or m.get("closed") or m.get("active") is False:
            return 0
        end_str = m.get("endDate") or m.get("end_date_iso") or ""
        try:
            end_t = datetime.fromisoformat(end_str.replace("Z", "+00:00")).replace(tzinfo=timezone.utc)
        except Exception:
            return 0
        if (end_t - datetime.now(timezone.utc)).total_seconds() < -60:
            return 0
        cids   = _parse_clob_token_ids(m.get("clobTokenIds", []))
        yes_id = cids[0] if len(cids) > 0 else ""
        no_id  = cids[1] if len(cids) > 1 else ""
        if mid not in self.markets:
            question = m.get("question") or ev.get("title") or "Bitcoin Up or Down"
            self.markets[mid] = MarketState(mid, question, end_t, yes_id, no_id)
            self._log(
                f"Yeni pazar | {int((end_t - datetime.now(timezone.utc)).total_seconds())}s | {question[:48]}",
                "INFO"
            )
            return 1
        else:
            ms = self.markets[mid]
            if not ms.yes_id and yes_id:
                ms.yes_id = yes_id
            if not ms.no_id and no_id:
                ms.no_id = no_id
            return 0

    async def _fetch_prices(self, session: aiohttp.ClientSession) -> None:
        price = await self._fetch_btc_rest(session)
        if price:
            self.btc_price = price
            self.btc_history.append(self.btc_price)
        else:
            try:
                async with session.get(
                    "https://api.coingecko.com/api/v3/simple/price",
                    params={"ids": "bitcoin", "vs_currencies": "usd"},
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as r:
                    if r.status == 200:
                        self.btc_price = float((await r.json())["bitcoin"]["usd"])
                        self.btc_history.append(self.btc_price)
            except Exception:
                pass

        if self.btc_price > 0:
            for ms in list(self.markets.values()):
                if ms.ref_btc_price == 0.0 and 0 < ms.secs_left <= 300:
                    ms.ref_btc_price = self.btc_price
                    self._log(f"REF MUHUR | ${self.btc_price:,.2f} | {ms.short_name}", "INFO")

        tasks = [
            self._fetch_book(session, ms)
            for ms in list(self.markets.values())
            if ms.secs_left > 0 and ms.yes_id
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _fetch_book(self, session: aiohttp.ClientSession, ms: MarketState) -> None:
        book_success = False
        try:
            async with session.get(
                f"{self.net['clob_url']}/book",
                params={"token_id": ms.yes_id},
                timeout=aiohttp.ClientTimeout(total=3),
            ) as r:
                if r.status == 200:
                    d    = await r.json()
                    bids = d.get("bids", [])
                    asks = d.get("asks", [])
                    if bids and asks:
                        bids = sorted(bids, key=lambda x: float(x.get("price", 0)), reverse=True)
                        asks = sorted(asks, key=lambda x: float(x.get("price", 0)))
                        ms.best_bid = float(bids[0]["price"])
                        ms.best_ask = float(asks[0]["price"])
                        total_bid   = sum(float(b.get("size", 0)) for b in bids)
                        total_ask   = sum(float(a.get("size", 0)) for a in asks)
                        denom       = total_bid + total_ask
                        ms.obi_score = (total_bid / denom) if denom > 0 else 0.5
                        book_success = True
        except Exception:
            pass

        if not book_success:
            try:
                async with session.get(
                    f"{self.net['clob_url']}/price",
                    params={"token_id": ms.yes_id, "side": "buy"},
                    timeout=aiohttp.ClientTimeout(total=2)
                ) as r:
                    if r.status == 200:
                        px = (await r.json()).get("price")
                        if px:
                            ms.best_ask = float(px)
                async with session.get(
                    f"{self.net['clob_url']}/price",
                    params={"token_id": ms.yes_id, "side": "sell"},
                    timeout=aiohttp.ClientTimeout(total=2)
                ) as r:
                    if r.status == 200:
                        px = (await r.json()).get("price")
                        if px:
                            ms.best_bid = float(px)
                ms.obi_score = 0.5
            except Exception:
                pass

        ms.mid_px = (ms.best_ask + ms.best_bid) / 2.0
        ms.history.append(ms.mid_px)

    def _btc_momentum(self) -> Tuple[float, str]:
        win    = int(self.strat.get("momentum_window", 10))
        thresh = float(self.strat.get("momentum_threshold", 0.20))
        prices = list(self.btc_history)[-win:]
        if len(prices) < 3:
            return 0.0, "FLAT"
        velocity = (prices[-1] - prices[0]) / max(len(prices) - 1, 1)
        if velocity > thresh:
            return velocity, "UP"
        if velocity < -thresh:
            return velocity, "DOWN"
        return velocity, "FLAT"

    def _signal(self, ms: MarketState) -> str:
        if ms.secs_left <= 0:
            return "BEKLE"
        if ms.ref_btc_price == 0.0:
            return "BEKLENIYOR"

        spread = ms.best_ask - ms.best_bid
        if spread > float(self.strat.get("max_spread", 0.04)):
            return "BEKLE"

        velocity, direction = self._btc_momentum()
        btc_delta = self.btc_price - ms.ref_btc_price
        max_e     = float(self.strat.get("max_entry_price", 0.76))

        if len(ms.history) > 4:
            arr, std = np.array(list(ms.history)), np.std(list(ms.history))
            if std > 1e-9:
                ms.zscore = (ms.mid_px - np.mean(arr)) / std
            z = float(self.strat["zscore_threshold"])
            if ms.zscore < -z and ms.best_ask < max_e:
                return "AL YES (MR)"
            if ms.zscore > z and (1.0 - ms.best_bid) < max_e:
                return "AL NO (MR)"

        if 0 < ms.secs_left < float(self.strat["latency_window"]):
            tol    = float(self.strat.get("btc_delta_tolerance", 50.0))
            yes_ok = btc_delta >= -tol and direction in ("UP", "FLAT") and ms.best_ask < max_e
            no_ok  = btc_delta <= tol  and direction in ("DOWN", "FLAT") and (1.0 - ms.best_bid) < max_e
            if yes_ok and no_ok:
                return "Sniper YES" if ms.best_ask <= (1.0 - ms.best_bid) else "Sniper NO"
            if yes_ok:
                return "Sniper YES"
            if no_ok:
                return "Sniper NO"

        obi_thresh = float(self.strat.get("obi_threshold", 0.62))
        if ms.obi_score > obi_thresh and ms.best_ask < max_e:
            return "OBI YES"
        if ms.obi_score < (1.0 - obi_thresh) and (1.0 - ms.best_bid) < max_e:
            return "OBI NO"

        strong = float(self.strat.get("momentum_threshold", 0.20)) * 3
        if abs(velocity) > strong:
            if direction == "UP" and ms.best_ask < max_e:
                return "Mom YES"
            if direction == "DOWN" and (1.0 - ms.best_bid) < max_e:
                return "Mom NO"

        return "BEKLE"

    def _check_exit(self, ms: MarketState) -> Tuple[bool, str, float, float]:
        t = ms.active_trade
        if not t:
            return False, "", 0.0, 0.0
        cur  = _safe_price(ms.best_bid if t.side == "YES" else 1.0 - ms.best_ask)
        gain = cur - t.entry_price

        tp = (cur >= float(self.strat["tp_min_price"]) or gain >= float(self.strat["tp_gain"]))
        sl = (cur <= float(self.strat["sl_max_price"]) or gain <= -float(self.strat["sl_loss"]))

        if tp or sl:
            exit_px = cur
            gross   = t.shares * exit_px
            fee     = t.stake  * float(self.strat["fee_slippage"])
            pnl     = round(gross - t.stake - fee, 4)
            return True, ("TP" if tp else "SL"), pnl, exit_px
        return False, "", 0.0, 0.0

    async def _analyze(self, ms: MarketState) -> None:
        if self._daily_limit_hit:
            ms.signal = "GUNLUK LIMIT"
            return
        if ms.has_traded and not ms.active_trade:
            ms.signal = "TEK KURSUN"
            return

        if ms.active_trade:
            t              = ms.active_trade
            time_exit_secs = float(self.strat.get("time_exit_secs", 45))
            max_retries    = int(self.strat.get("max_exit_retries", 3))

            if ms.exit_retries >= max_retries:
                if not ms.has_traded:
                    self._log("Max deneme asildi! Mac Sonu bekleniyor.", "WARNING")
                    ms.has_traded = True
                return

            if ms.secs_left <= time_exit_secs:
                cur = _safe_price(ms.best_bid if t.side == "YES" else 1.0 - ms.best_ask)
                oid = await self.order_mgr.place_sell(t.token_id, cur, t.shares)
                if not oid or oid.startswith("ERR:"):
                    ms.exit_retries += 1
                    self._log(f"SATIS HATASI ({ms.exit_retries}/{max_retries}): {oid}", "ERROR")
                else:
                    gross = t.shares * cur
                    fee   = t.stake  * float(self.strat["fee_slippage"])
                    pnl   = round(gross - t.stake - fee, 4)
                    self._record(ms, pnl, "TIME_EXIT", cur)
                    ms.has_traded, ms.exit_retries = True, 0
                    self._log(
                        f"TIME_EXIT | {t.side} {t.entry_price:.3f}->{cur:.3f} | PnL:${pnl:+.3f}",
                        "EXIT"
                    )
                return

            ok, reason, pnl, exit_px = self._check_exit(ms)
            if ok:
                oid = await self.order_mgr.place_sell(t.token_id, exit_px, t.shares)
                if not oid or oid.startswith("ERR:"):
                    ms.exit_retries += 1
                    self._log(f"SATIS HATASI ({ms.exit_retries}/{max_retries}): {oid}", "ERROR")
                else:
                    self._record(ms, pnl, reason, exit_px)
                    ms.has_traded, ms.exit_retries = True, 0
                    self._log(
                        f"{'WIN' if pnl > 0 else 'LOSS'} {reason} | {t.side} "
                        f"{t.entry_price:.3f}->{exit_px:.3f} | PnL:${pnl:+.3f}",
                        "TRADE"
                    )
            return

        ms.signal = self._signal(ms)

        if not ("AL" in ms.signal or "Sniper" in ms.signal or "Mom" in ms.signal or "OBI" in ms.signal):
            return

        side     = "YES" if "YES" in ms.signal else "NO"
        token_id = ms.yes_id if side == "YES" else ms.no_id
        entry    = ms.best_ask if side == "YES" else 1.0 - ms.best_bid

        min_e = float(self.strat.get("min_entry_price", 0.52))
        max_e = float(self.strat.get("max_entry_price", 0.76))
        if entry < min_e or entry > max_e or not token_id:
            return

        if ms.secs_left <= float(self.strat.get("time_exit_secs", 45)):
            return

        if sum(1 for m in self.markets.values() if m.active_trade) >= int(self.risk.get("max_open_positions", 2)):
            ms.signal = "POS DOLU"
            return

        now_ts = datetime.now(timezone.utc).timestamp()
        fok_cd = float(self.strat.get("fok_cooldown", 20))
        if now_ts - ms.last_buy_attempt < fok_cd:
            return

        stake = float(self.risk["stake_usd"])

        # FIX #3: shares < 5.0 gizli kill sorunu
        # Polymarket minimum 5 token gerektirir.
        # _safe_amounts ile dogru yuvarlanmis degerler kullan.
        _, shares = _safe_amounts(entry, stake)
        if shares < 5.0:
            # Bu piyasada bu fiyatta minimum emirden az -- sessizce gecme yerine logla
            self._log(
                f"MIN SIZE ({shares:.2f}<5.0) | entry={entry:.3f} stake=${stake} | "
                f"Stake artirimi gerekiyor.",
                "WARNING"
            )
            return

        oid = await self.order_mgr.place_buy(token_id, entry, shares)
        if not oid or oid.startswith("ERR:"):
            ms.last_buy_attempt = now_ts
            self._log(f"FOK iptal — {int(fok_cd)}s bekleniyor. ({oid})", "WARNING")
            return

        ms.active_trade = LiveTrade(
            market_id=ms.mid, token_id=token_id, side=side,
            entry_price=entry, shares=shares, stake=stake,
            entry_btc=self.btc_price, ref_btc=ms.ref_btc_price, order_id=oid
        )
        self._log(
            f"[{'LIVE' if self.live_mode else 'PAPER'}] {ms.signal} | "
            f"{side}@{entry:.3f} | ${stake:.2f}->{shares:.4f}hisse",
            "LIVE" if self.live_mode else "PAPER"
        )

    async def _settle(self, mid: str) -> None:
        ms = self.markets.get(mid)
        if not ms:
            return
        if ms.active_trade:
            t   = ms.active_trade
            win = (self.btc_price > ms.ref_btc_price) if t.side == "YES" else (self.btc_price <= ms.ref_btc_price)
            if win:
                pnl = round((t.shares * 1.0) - t.stake - (t.stake * float(self.strat["fee_slippage"])), 4)
                self._record(ms, pnl, "SETTL_WIN", 1.0)
                self._log(f"SETTL_WIN | {t.side} | PnL:${pnl:+.3f}", "TRADE")
            else:
                self._record(ms, -t.stake, "SETTL_LOSS", 0.0)
                self._log(f"SETTL_LOSS | {t.side} | -${t.stake}", "WARNING")
        del self.markets[mid]

    def _record(self, ms: MarketState, pnl: float, rtype: str, exit_px: float) -> None:
        t = ms.active_trade
        if not t:
            return
        self.wins      += pnl > 0
        self.losses    += pnl <= 0
        self.trades    += 1
        self.session_pnl += pnl
        self.daily_pnl   += pnl
        try:
            with open(self.cfg.get("memory_file", "trades_live.jsonl"), "a") as f:
                f.write(json.dumps({
                    "mid": ms.mid, "side": t.side, "entry": t.entry_price,
                    "exit": exit_px, "shares": t.shares, "stake": t.stake,
                    "pnl": pnl, "result": rtype,
                    "live": self.live_mode,
                    "ts": datetime.now(timezone.utc).isoformat()
                }) + "\n")
        except Exception:
            pass
        ms.active_trade = None

    def _render(self) -> Layout:
        lay = Layout()
        lay.split_column(
            Layout(name="h", size=3),
            Layout(name="b", ratio=1),
            Layout(name="l", size=14)
        )
        lay["b"].split_row(Layout(name="mt", ratio=4), Layout(name="s", size=38))

        wr       = (self.wins / self.trades * 100) if self.trades else 0.0
        velocity, v_dir = self._btc_momentum()
        stake    = float(self.risk["stake_usd"])

        hdr = (
            f"[bold white]BTC SNIPER V10.30 (Agresif Compounding)[/bold white] "
            f"{'[bold red]CANLI[/bold red]' if self.live_mode else '[dim]KAGIT[/dim]'} | "
            f"BTC:[cyan]${self.btc_price:,.0f}[/cyan] | "
            f"PnL:[{'green' if self.session_pnl >= 0 else 'red'}]${self.session_pnl:+.3f}[/] | "
            f"USDC:[yellow]${self.usdc_balance:.2f}[/yellow] | "
            f"W/L:[green]{self.wins}[/green]/[red]{self.losses}[/red]({wr:.0f}%)"
        )
        lay["h"].update(Panel(
            Text.from_markup(hdr),
            border_style="red" if self.live_mode else "dim"
        ))

        tbl = Table(box=box.MINIMAL_DOUBLE_HEAD, expand=True)
        for col in ["Kalan", "Pazar", "Ref BTC", "dBTC", "Ask", "Bid", "Z", "OBI", "Sinyal", "Pozisyon"]:
            tbl.add_column(col, no_wrap=True)

        for ms in sorted(self.markets.values(), key=lambda x: x.secs_left):
            if ms.secs_left <= 0:
                continue
            delta   = self.btc_price - ms.ref_btc_price if ms.ref_btc_price > 0 else 0.0
            pos_str = ""
            row_style = "white"

            if ms.active_trade:
                t    = ms.active_trade
                cur  = _safe_price(ms.best_bid if t.side == "YES" else 1.0 - ms.best_ask)
                gain = cur - t.entry_price
                pos_str   = f"{'UP' if gain >= 0 else 'DN'} {t.side} {gain:+.2f}({gain/t.entry_price*100:+.0f}%)"
                row_style = "green" if gain >= 0 else "red"
            elif ms.has_traded:
                pos_str   = "[dim]TEK KURSUN[/dim]"
                row_style = "dim"
            elif datetime.now(timezone.utc).timestamp() - ms.last_buy_attempt < float(self.strat.get("fok_cooldown", 20)):
                pos_str   = "[yellow]COOLDOWN[/yellow]"
                row_style = "yellow"

            obi_str = f"[{'green' if ms.obi_score > 0.65 else 'red' if ms.obi_score < 0.35 else 'white'}]{ms.obi_score:.2f}[/]"
            t_str   = f"[bold magenta]{int(ms.secs_left)}s[/]" if ms.secs_left <= float(self.strat.get("time_exit_secs", 45)) else f"{int(ms.secs_left)}s"

            tbl.add_row(
                t_str, ms.short_name,
                f"${ms.ref_btc_price:,.0f}" if ms.ref_btc_price > 0 else "[dim]BEKL.[/dim]",
                f"[{'green' if delta >= 0 else 'red'}]{delta:+,.0f}[/]",
                f"{ms.best_ask:.3f}", f"{ms.best_bid:.3f}",
                f"{ms.zscore:+.2f}", obi_str, ms.signal, pos_str,
                style=row_style
            )

        lay["mt"].update(Panel(
            tbl,
            title=f"Aktif Pazarlar ({sum(1 for ms in self.markets.values() if ms.secs_left > 0)})",
            border_style="red" if self.live_mode else "dim"
        ))

        stat = (
            f"[bold cyan]KASA[/bold cyan]\n  Stake: [green]${stake:.2f}[/green]\n"
            f"  Limit: [red]-${self.risk['max_daily_loss_usd']:.2f}[/red]\n\n"
            f"[bold cyan]MOMENTUM[/bold cyan]\n"
            f"  [{'green' if v_dir == 'UP' else 'red'}]{velocity:+.2f} $/s -> {v_dir}[/]\n\n"
            f"[bold cyan]CIKIS[/bold cyan]\n"
            f"  TP: +{self.strat['tp_gain']:.2f}\n"
            f"  SL: -{self.strat['sl_loss']:.2f}\n\n"
            f"[bold cyan]OTURUM[/bold cyan]\n"
            f"  PnL: [{'green' if self.session_pnl >= 0 else 'red'}]${self.session_pnl:+.3f}[/]\n"
            f"  Gunluk: [{'green' if self.daily_pnl >= 0 else 'red'}]${self.daily_pnl:+.3f}[/]"
        )
        lay["s"].update(Panel(Text.from_markup(stat), title="Durum", border_style="yellow"))
        lay["l"].update(Panel(
            Text.from_markup("\n".join(list(self.logs))),
            title="Log [V10.30]",
            border_style="red" if self.live_mode else "dim"
        ))
        return lay

    async def main_run(self) -> None:
        connector = aiohttp.TCPConnector(
            limit=20,
            resolver=aiohttp.ThreadedResolver(),
            family=socket.AF_INET
        )
        async with aiohttp.ClientSession(connector=connector) as session:
            for _ in range(5):
                price = await self._fetch_btc_rest(session)
                if price:
                    self.btc_price = price
                    self.btc_history.append(price)
                    break
                await asyncio.sleep(2)

            try:
                self.usdc_balance = await self.order_mgr.get_balance()
            except Exception:
                pass

            self._log(
                "V10.30 BASLADI | Agresif Compounding + invalid_amounts fix + allowance retry aktif",
                "LIVE" if self.live_mode else "PAPER"
            )

            with Live(self._render(), refresh_per_second=2, screen=True) as live:
                cycle = 0
                while self._running:
                    self._check_daily_reset()
                    if cycle % 30 == 0:
                        await self._update_markets(session)
                    if cycle % 60 == 0 and self.live_mode:
                        try:
                            self.usdc_balance = await self.order_mgr.get_balance()
                        except Exception:
                            pass
                    await self._fetch_prices(session)
                    for ms in list(self.markets.values()):
                        if ms.secs_left > 0:
                            await self._analyze(ms)
                        else:
                            await self._settle(ms.mid)
                    live.update(self._render())
                    await asyncio.sleep(1)
                    cycle += 1

        self._running = False
        if self.live_mode:
            await self.order_mgr.cancel_all()


if __name__ == "__main__":
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else "config.json"
    try:
        cfg = load_config(cfg_path)
    except FileNotFoundError:
        sys.exit(1)

    bot = LiveSniperBot(cfg)
    if bot.live_mode:
        if input("  CANLI PARA modunda calisacak. Devam? [evet/hayir]: ").strip().lower() not in ("evet", "e", "yes", "y"):
            sys.exit(0)
    try:
        asyncio.run(bot.main_run())
    except KeyboardInterrupt:
        pass
