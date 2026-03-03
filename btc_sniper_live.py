#!/usr/bin/env python3
"""
Polymarket BTC Sniper V11.2 (Late Convergence Strategy)
=========================================================
  V11.1 — PNL MATEMATIK DUZELTMESI:
  - EMRG_STOP, SETTL_WIN, SETTL_LOSS: gercek maliyet (shares * entry_price)
    kullanilacak, sabit stake ($5) degil.
  - Ornek: 5 hisse * 0.87 = $4.35 gercek maliyet, stake=$5 degil.
  - Break-even icin gereken win rate: ~%68 (Gemini'nin %90 iddiasi yanlistir).

  V11.0 — TAM STRATEJI DEGISIMI (V10.35 uzerine):

  ARASTIRMA BULGULARI:
  - Polymarket dinamik taker fee: p*(1-p)*k formulu
    → 0.50 fiyatinda %3.15 fee (eski giris bölgesi artik negatif EV!)
    → 0.90 fiyatinda %0.20 fee (convergence bolgesi hala karlı)
  - 500ms speed bump kaldirildi → latency arb tamamen oldu
  - Kazanan strateji: <110s kalan, BTC $20+ hareket, fiyat 0.83-0.93
    → Arastirmalarda %85-96 win rate

  V11.0 DEGISIKLIKLER:
  1. [STRATEJI] MR/OBI/Sniper/Momentum sinyalleri KALDIRILDI.
     Tek sinyal: CONV YES / CONV NO (Late Convergence)
     Giris kosulu: secs_left 25-110, BTC window delta >= $20,
     BTC son momentum yonuyle uyumlu, fiyat 0.83-0.93 arasi.
  2. [CIKIS] TIME_EXIT kaldirildi. Settlement'a kadar pozisyon tutulur.
     Tek cikis: EMRG_STOP (fiyat 0.68'e duserse sat — BTC yonu donmus)
  3. [MATEMATIK] 0.88 giris, %96 win rate: EV = 0.96*$0.44 - 0.04*$0.82
     = +$0.39 per trade. Eski sistemde: negatif EV.
  4. [CONFIG] min_entry 0.83, max_entry 0.93, emergency_stop 0.68
     convergence_btc_delta 20, convergence_secs_min 25, convergence_secs_max 110

  V10.34 FIX LIST (korunuyor):
  6. [CRITICAL] approve_token(): Alim basarili olunca HEMEN o token icin
     CONDITIONAL approval set edilir. Polygon'a ~2-3 dakika onay suresi
     verir; satista artik 15s bekleme / approval hatasi olmaz.
  7. place_sell: approval hatasi artik sessizce gecmiyor, ERR:APPROVAL:...
     olarak log'a yaziliyor — gercek sebep gorulebilir.

  V10.33 FIX LIST (korunuyor):
  3. Satisi "max deneme asildi" ile durdurmak kaldirildi.
     Her exit_retry_interval (15s) saniyede bir tekrar dener, pazar kapanana kadar.
  4. ensure_approvals(): Startup'ta COLLATERAL + CONDITIONAL approval kontrol/set.
  5. TP/SL cikislari hala anlik denenir (interval yok).

  V10.32 FIX LIST (korunuyor):
  6. update_allowances() ClobClient'ta mevcut degil hatasi giderildi.

  V10.31 FIX LIST (korunuyor):
  7. [CRITICAL] _safe_amounts(): TAM SAYI HISSE stratejisi.
  8. debug.log: encoding='utf-8' + try/except + tam tarih formati.

  V10.30 FIX LIST (korunuyor):
  9. [BUG FIX] shares < 5.0 gizli kill: Stake $4 ile fix edildi.
  10. fok_cooldown: 60s -> 20s.
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
    V10.31 FIX - TAM SAYI HISSE (INTEGER SHARES) STRATEJISI:
    Polymarket maker amount (price * shares USDC) kesinlikle 2 ondalikli olmali.

    Onceki yaklasim (4-decimal shares) cift _safe_amounts cagrisi sebebiyle
    shares kayiyordu: _analyze -> place_buy arasindaki geri donusum
    7.5472 * 0.53 = 3.999016 (2-decimal DEGIL) -> red.

    Cozum: Tam sayi hisse. n (integer) * 0.XX (2-decimal) = her zaman 2-decimal USDC.
    Matematiksel garanti: n*a, n integer & a 2-decimal => sonuc max 2-decimal.
    """
    price_r = round(max(0.01, min(0.99, float(price))), 2)
    raw_shares = stake / price_r
    shares_int = float(int(round(raw_shares, 4)))
    return price_r, shares_int


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
        self.exit_retries:      int   = 0
        self.last_buy_attempt:  float = 0.0
        self.last_exit_attempt: float = 0.0

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
        return self._client

    async def place_buy(self, token_id: str, price: float, shares: float) -> str:
        if not self.live_mode:
            self._paper_seq += 1
            return f"PAPER-{self._paper_seq:04d}"

        def _do():
            try:
                client = self._client_or_raise()
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
            last_err = None
            for attempt in range(3):
                try:
                    if attempt == 1:
                        # approve_token() alimda zaten cagirildi; bu son care fallback.
                        # Hata artik sessizce gecmiyor, log'da gorunur.
                        try:
                            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
                            client.update_balance_allowance(
                                params=BalanceAllowanceParams(
                                    asset_type=AssetType.CONDITIONAL,
                                    token_id=token_id,
                                )
                            )
                        except Exception as appr_e:
                            return f"ERR:APPROVAL:{appr_e}"
                        _time.sleep(15.0)  # Polygon: ~5s blok, 15s = 3 blok guvenli
                    elif attempt == 2:
                        _time.sleep(5.0)
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
                    last_err = e
                    err_str = str(e).lower()
                    if "not enough balance" in err_str or "allowance" in err_str:
                        continue
                    return f"ERR:{e}"
            return f"ERR:{last_err}"

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, _do)

    async def approve_token(self, token_id: str) -> str:
        """Alimdan hemen sonra o token icin CONDITIONAL approval set et.
        Polygon onayina satisa kadar ~2-3 dakika verir; satista bekleme gerekmez."""
        if not self.live_mode:
            return "PAPER"

        def _do():
            try:
                from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
                client = self._client_or_raise()
                resp = client.update_balance_allowance(
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

            # Native USDC (Polygon) - 0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359
            NATIVE_USDC = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
            wallet = self.cfg["credentials"].get("wallet_address", "")

            # 1. Doğrudan Polygon RPC'den Native USDC bakiyesini oku
            if wallet:
                try:
                    padded = wallet.lower().replace("0x", "").zfill(64)
                    data    = "0x70a08231" + padded  # balanceOf(address)
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

            # 2. Fallback: Polymarket CLOB API (Bridged USDC veya API bakiyesi)
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

    async def ensure_approvals(self) -> str:
        """Startup'ta USDC (COLLATERAL) ve conditional token (ERC1155) approval'larini ayarla."""
        if not self.live_mode:
            return "PAPER"

        def _do():
            import time as _time
            client = self._client_or_raise()
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
        self._consec_losses: int   = 0
        self._sl_cooldown_until: float = 0.0

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
        """Son N kayittaki BTC yonunu hesapla (~90s at 2s/cycle icin win=45)."""
        win    = int(self.strat.get("momentum_window", 45))
        thresh = float(self.strat.get("momentum_threshold", 0.05))
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
        """
        Late Convergence Signal (V11.0):
        Sadece pencere sonunda ($20+ BTC hareketi + 0.83-0.93 fiyat) girer.
        Arastirma: bu kosulda %85-96 win rate, fee ~%0.20.
        """
        if ms.secs_left <= 0:
            return "BEKLE"
        if ms.ref_btc_price == 0.0:
            return "BEKLENIYOR"

        # Convergence penceresi: sadece son 25-110 saniyede gir
        secs_min = float(self.strat.get("convergence_secs_min", 25.0))
        secs_max = float(self.strat.get("convergence_secs_max", 110.0))
        if not (secs_min < ms.secs_left < secs_max):
            return "BEKLE"

        # Spread kontrolu
        spread = ms.best_ask - ms.best_bid
        if spread > float(self.strat.get("max_spread", 0.05)):
            return "GENIS"

        # BTC hareketi: pencere basından bu yana ($20+ gerekli)
        btc_min      = float(self.strat.get("convergence_btc_delta", 20.0))
        window_delta = self.btc_price - ms.ref_btc_price

        # Son momentum — yon dogrulamasi icin (geri dönüs varsa girme)
        _, direction = self._btc_momentum()

        # Convergence fiyat bolgesi: 0.83-0.93
        min_e  = float(self.strat.get("min_entry_price", 0.83))
        max_e  = float(self.strat.get("max_entry_price", 0.93))
        yes_px = ms.best_ask
        no_px  = 1.0 - ms.best_bid

        # YES: BTC yukari gitti VE hala yukari gidiyor (ya da flat — gecici bekleme)
        if window_delta >= btc_min and direction != "DOWN":
            if min_e <= yes_px <= max_e:
                return "CONV YES"

        # NO: BTC asagi gitti VE hala asagi gidiyor
        if window_delta <= -btc_min and direction != "UP":
            if min_e <= no_px <= max_e:
                return "CONV NO"

        return "BEKLE"

    def _check_exit(self, ms: MarketState) -> Tuple[bool, str, float, float]:
        """
        V11.0 cikis mantigi: Normal TP/SL YOK — settlement'a kadar tut.
        Tek cikis: EMRG_STOP — fiyat cok duserse BTC yonu donmustür, sat.
        V11.2: Son 30 saniyede EMRG kontrol edilmez; market kapanirken
               best_bid kurur, sahte tetik + basa donen PolyApiException olur.
        """
        t = ms.active_trade
        if not t:
            return False, "", 0.0, 0.0

        # Son 30 saniye: settlement zaten geliyor, satmaya calisma
        if ms.secs_left <= 30:
            return False, "", 0.0, 0.0

        cur  = _safe_price(ms.best_bid if t.side == "YES" else 1.0 - ms.best_ask)

        # Acil durus: giris fiyatinin cok altina dustuyse BTC yonu dönmüstür
        emrg = float(self.strat.get("emergency_stop", 0.68))
        if cur <= emrg:
            gross       = t.shares * cur
            actual_cost = t.shares * t.entry_price
            fee         = actual_cost * float(self.strat["fee_slippage"])
            pnl         = round(gross - actual_cost - fee, 4)
            return True, "EMRG_STOP", pnl, cur

        return False, "", 0.0, 0.0

    async def _analyze(self, ms: MarketState) -> None:
        if self._daily_limit_hit:
            ms.signal = "GUNLUK LIMIT"
            return
        if datetime.now(timezone.utc).timestamp() < self._sl_cooldown_until:
            ms.signal = "SL SOGUMA"
            return
        if ms.has_traded and not ms.active_trade:
            ms.signal = "TEK KURSUN"
            return

        if ms.active_trade:
            t      = ms.active_trade
            now_ts = datetime.now(timezone.utc).timestamp()

            # V11.0: Sadece EMRG_STOP — settlement'a kadar tut
            ok, reason, pnl, exit_px = self._check_exit(ms)
            if ok:
                oid = await self.order_mgr.place_sell(t.token_id, exit_px, t.shares)
                if not oid or oid.startswith("ERR:"):
                    ms.exit_retries += 1
                    self._log(f"EMRG SATIS HATASI (deneme {ms.exit_retries}): {oid}", "ERROR")
                else:
                    self._record(ms, pnl, reason, exit_px)
                    ms.has_traded, ms.exit_retries = True, 0
                    self._log(
                        f"EMRG_STOP | {t.side} "
                        f"{t.entry_price:.3f}->{exit_px:.3f} | PnL:${pnl:+.3f}",
                        "WARNING"
                    )
            return

        ms.signal = self._signal(ms)

        # V11.0: Sadece CONV sinyalini kabul et
        if "CONV" not in ms.signal:
            return

        side     = "YES" if "YES" in ms.signal else "NO"
        token_id = ms.yes_id if side == "YES" else ms.no_id
        entry    = ms.best_ask if side == "YES" else 1.0 - ms.best_bid

        min_e = float(self.strat.get("min_entry_price", 0.83))
        max_e = float(self.strat.get("max_entry_price", 0.93))
        if entry < min_e or entry > max_e or not token_id:
            return

        # time_exit_secs yok — convergence_secs_min zaten _signal() icinde kontrol edildi

        if sum(1 for m in self.markets.values() if m.active_trade) >= int(self.risk.get("max_open_positions", 2)):
            ms.signal = "POS DOLU"
            return

        now_ts = datetime.now(timezone.utc).timestamp()
        fok_cd = float(self.strat.get("fok_cooldown", 20))
        if now_ts - ms.last_buy_attempt < fok_cd:
            return

        stake = float(self.risk["stake_usd"])

        _, shares = _safe_amounts(entry, stake)
        if shares < 5.0:
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
            f"{side}@{entry:.3f} | ${stake:.2f}->{shares:.0f}hisse",
            "LIVE" if self.live_mode else "PAPER"
        )
        # Alimdan hemen sonra bu token icin sell approval set et.
        # Polygon onayina satisa kadar ~2-3 dakika vakit verir.
        appr = await self.order_mgr.approve_token(token_id)
        self._log(f"Token approval: {appr[:80]}", "INFO")

    async def _settle(self, mid: str) -> None:
        ms = self.markets.get(mid)
        if not ms:
            return
        if ms.active_trade:
            t   = ms.active_trade
            win = (self.btc_price > ms.ref_btc_price) if t.side == "YES" else (self.btc_price <= ms.ref_btc_price)
            actual_cost = t.shares * t.entry_price
            if win:
                fee = actual_cost * float(self.strat["fee_slippage"])
                pnl = round((t.shares * 1.0) - actual_cost - fee, 4)
                self._record(ms, pnl, "SETTL_WIN", 1.0)
                self._log(f"SETTL_WIN | {t.side} | PnL:${pnl:+.3f}", "TRADE")
            else:
                pnl = round(-actual_cost, 4)
                self._record(ms, pnl, "SETTL_LOSS", 0.0)
                self._log(f"SETTL_LOSS | {t.side} | -${actual_cost:.3f}", "WARNING")
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
        if pnl <= 0:
            self._consec_losses += 1
            if self._consec_losses >= 3:
                self._sl_cooldown_until = datetime.now(timezone.utc).timestamp() + 300
                self._log(f"{self._consec_losses} ardisik kayip — 5 dk bekleniyor", "WARNING")
        else:
            self._consec_losses = 0
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
        btc_min  = float(self.strat.get("convergence_btc_delta", 20.0))

        hdr = (
            f"[bold white]BTC SNIPER V11.1 (Late Convergence)[/bold white] "
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
        secs_min = float(self.strat.get("convergence_secs_min", 25.0))
        secs_max = float(self.strat.get("convergence_secs_max", 110.0))
        min_e    = float(self.strat.get("min_entry_price", 0.83))
        max_e    = float(self.strat.get("max_entry_price", 0.93))
        for col in ["Kalan", "Pazar", "Ref BTC", "dBTC", "AskY", "AskN", "Bölge", "Sinyal", "Pozisyon"]:
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
                pos_str   = f"{'UP' if gain >= 0 else 'DN'} {t.side}@{t.entry_price:.2f} {gain:+.2f}"
                row_style = "green" if gain >= 0 else "red"
            elif ms.has_traded:
                pos_str   = "[dim]TAMAMLANDI[/dim]"
                row_style = "dim"
            elif datetime.now(timezone.utc).timestamp() - ms.last_buy_attempt < float(self.strat.get("fok_cooldown", 20)):
                pos_str   = "[yellow]COOLDOWN[/yellow]"
                row_style = "yellow"

            # Convergence penceresi göstergesi
            in_window = secs_min < ms.secs_left < secs_max
            t_color   = "bold green" if in_window else "white"
            t_str     = f"[{t_color}]{int(ms.secs_left)}s[/]"

            # YES/NO giris fiyatlari ve bölge uyumu
            yes_px   = ms.best_ask
            no_px    = 1.0 - ms.best_bid
            yes_ok   = min_e <= yes_px <= max_e
            no_ok    = min_e <= no_px  <= max_e
            zone_str = (
                f"[green]Y✓[/] " if yes_ok else f"[dim]Y--[/] "
            ) + (
                f"[green]N✓[/]" if no_ok else f"[dim]N--[/]"
            )

            tbl.add_row(
                t_str, ms.short_name,
                f"${ms.ref_btc_price:,.0f}" if ms.ref_btc_price > 0 else "[dim]BEKL.[/dim]",
                f"[{'green' if delta >= 0 else 'red'}]{delta:+,.0f}[/]",
                f"[{'green' if yes_ok else 'white'}]{yes_px:.3f}[/]",
                f"[{'green' if no_ok else 'white'}]{no_px:.3f}[/]",
                zone_str, ms.signal, pos_str,
                style=row_style
            )

        lay["mt"].update(Panel(
            tbl,
            title=f"Aktif Pazarlar ({sum(1 for ms in self.markets.values() if ms.secs_left > 0)})",
            border_style="red" if self.live_mode else "dim"
        ))

        emrg = float(self.strat.get("emergency_stop", 0.68))
        stat = (
            f"[bold cyan]KASA[/bold cyan]\n  Stake: [green]${stake:.2f}[/green]\n"
            f"  Limit: [red]-${self.risk['max_daily_loss_usd']:.2f}[/red]\n\n"
            f"[bold cyan]CONVERGENCE[/bold cyan]\n"
            f"  Bölge: {min_e:.2f}-{max_e:.2f}¢\n"
            f"  Pencere: {int(secs_min)}-{int(secs_max)}s\n"
            f"  BTC Min: ${btc_min:.0f}\n"
            f"  EMRG Stop: {emrg:.2f}¢\n\n"
            f"[bold cyan]BTC MOM[/bold cyan]\n"
            f"  [{'green' if v_dir == 'UP' else 'red' if v_dir == 'DOWN' else 'white'}]"
            f"{velocity:+.2f} $/cycle → {v_dir}[/]\n\n"
            f"[bold cyan]OTURUM[/bold cyan]\n"
            f"  PnL: [{'green' if self.session_pnl >= 0 else 'red'}]${self.session_pnl:+.3f}[/]\n"
            f"  Gunluk: [{'green' if self.daily_pnl >= 0 else 'red'}]${self.daily_pnl:+.3f}[/]"
        )
        lay["s"].update(Panel(Text.from_markup(stat), title="Durum", border_style="yellow"))
        lay["l"].update(Panel(
            Text.from_markup("\n".join(list(self.logs))),
            title="Log [V11.1]",
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

            if self.live_mode:
                appr_result = await self.order_mgr.ensure_approvals()
                self._log(f"Approval kontrol: {appr_result}", "INFO")

            self._log(
                "V11.1 BASLADI | Late Convergence | 0.83-0.93 | 25-110s | BTC $20+",
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
