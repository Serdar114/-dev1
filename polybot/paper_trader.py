"""
Paper Trader — Dual Side Capture simülasyonu.

Trade akışı:
  1. DUAL_ENTRY sinyali → open_position() → her iki taraf ask'tan girilir
  2. Fee hesaplanır: fee_up + fee_down (fee_engine.compute_fee)
  3. Pencere kapanır → resolve_pending()
  4. Kazanan taraf belirlenir (logging için — gross PnL her iki durumda aynı):
       UP wins: btc_close >= btc_open
       DOWN wins: btc_close < btc_open
       Tie: UP kazanır (sadece valid close ile)
  5. PnL:
       gross_pnl = shares * (1.0 - up_ask - down_ask)
       net_pnl   = gross_pnl - fee_total
  6. JSONL: fee_up, fee_down, fee_total, gross_pnl, net_pnl alanları dahil

fee_rate ve fee_exponent config'den okunur — hardcode yok.
"""

import asyncio
import aiohttp
import time
from dataclasses import dataclass
import logger as log_module
import truth_logger
from resolution_truth import resolve_truth
from fee_engine import compute_fee


@dataclass
class PaperPosition:
    trade_id: str
    up_ask: float
    down_ask: float
    pair_sum: float      # up_ask + down_ask
    net_edge: float      # 1.0 - pair_sum (gross edge per share, fee-blind)
    shares: int          # shares_per_side (her taraf için)
    btc_open: float
    opened_at: float
    window_ts: int
    interval: str = "5m"
    # fee fields (entry anında hesaplanır)
    fee_up: float = 0.0         # compute_fee(shares, up_ask, ...)
    fee_down: float = 0.0       # compute_fee(shares, down_ask, ...)
    fee_total: float = 0.0      # fee_up + fee_down
    fee_rate: float = 0.0       # config'den okunan fee rate
    fee_exponent: float = 0.0   # config'den okunan fee exponent
    # resolve sonrası
    resolved: bool = False
    resolution_blocked: bool = False  # True = truth layer resolve edemedi
    btc_close: float = 0.0
    winning_side: str = ""   # "up" | "down" | ""
    result: str = ""         # "win_up" | "win_down" | "unresolved"
    gross_pnl: float = 0.0  # shares * net_edge (fee-blind)
    net_pnl: float = 0.0    # gross_pnl - fee_total (fee-aware)
    # resolution truth fields
    winner_source: str = ""              # "binance" | "none"
    winner_binance: str = ""             # "up" | "down" | "unknown"
    winner_chainlink: str = ""           # "up" | "down" | "unknown"
    resolution_truth_status: str = ""    # "binance_only" | "dual_verified" | "dual_mismatch" | "unresolved_fetch_error"
    resolution_match: str = ""           # "match" | "mismatch" | "unknown"
    chainlink_status: str = ""           # "fetched" | "placeholder" | "error"
    # fee source visibility
    fee_source: str = ""           # "market_discovery" | "config" | "fallback" | "unknown"
    fee_status: str = ""           # "market_verified" | "configured" | "fallback_used" | "unproven_market_fee"
    # execution lane
    execution_lane: str = ""       # "taker_paper" | "maker_paper" | "unknown"
    # market context at entry (enrichment)
    # NOTE: up_ask/down_ask are required constructor args above — always present.
    #       up_bid/down_bid come from orderbook snapshot at entry via market_context.
    market_slug: str = ""
    btc_mid_binance: float = 0.0   # Binance mid at entry time (0.0 = unavailable)
    up_bid: float = 0.0            # best bid for UP token at entry
    down_bid: float = 0.0          # best bid for DOWN token at entry
    spread_up_pct: float = 0.0     # UP token spread as percentage: (ask-bid)/mid*100
    spread_down_pct: float = 0.0   # DOWN token spread as percentage: (ask-bid)/mid*100
    secs_to_res: int = 0           # seconds to resolution at entry
    # single-side fields (empty/0 for dual)
    side: str = ""                 # "" = dual, "up" | "down" = single-side
    entry_price: float = 0.0      # ask price of chosen side (single-side only)
    # unresolved lifecycle tracking
    resolution_retry_count: int = 0
    first_resolution_failure_ts: float = 0.0
    last_resolution_attempt_ts: float = 0.0


class PaperTrader:
    def __init__(self, config: dict, risk_manager=None):
        self.resolve_confirm_secs: int = config.get("resolve_confirm_secs", 130)
        self.shares_per_side: int = config.get("shares_per_side", 5)

        # Fee source detection — config'de açıkça var mı, yoksa fallback mı?
        fee_rate_configured = "fee_rate" in config

        self.fee_rate: float = config.get("fee_rate", 0.072)
        self.fee_exponent: float = config.get("fee_exponent", 1.0)

        if fee_rate_configured:
            self.fee_source: str = "config"
            self.fee_status: str = "configured"
        else:
            self.fee_source: str = "fallback"
            self.fee_status: str = "fallback_used"

        # Market discovery fee attempt tracking
        self._market_fee_attempted: bool = False
        self._market_fee_ok: bool = False
        self._market_fee_status: str = "not_attempted"

        # Execution lane — paper mode always simulates taker fills at ask, 100% fill
        self.execution_lane: str = "taker_paper"

        self._positions: list[PaperPosition] = []
        self._counter: int = 0
        self.risk_manager = risk_manager

    async def try_update_fee_from_market(self, token_id: str) -> dict:
        """
        Market discovery'den fee rate çekmeyi dene.

        Epistemik kural:
          - verified_remote=True  → fee_source="market_discovery", fee_rate güncellenir
          - verified_remote=False → fee_source DEĞİŞMEZ (config/fallback kalır)
          - Her iki durumda da deneme kaydedilir: market_fee_attempted, market_fee_status

        Döndürür: {"attempted", "verified_remote", "market_fee_rate", "market_fee_status", "note"}
        """
        from market_discovery import get_market_fee_rate
        self._market_fee_attempted = True
        try:
            mkt = await get_market_fee_rate(token_id)
        except Exception as e:
            # get_market_fee_rate kendi içinde exception yakalar ama
            # import veya beklenmedik hata olabilir
            self._market_fee_status = "import_or_unexpected_error"
            await log_module.log("fee_market_discovery_failed", {
                "token_id": token_id,
                "error": str(e),
                "effective_fee_source": self.fee_source,
                "market_fee_status": self._market_fee_status,
            })
            return {
                "attempted": True, "verified_remote": False,
                "market_fee_rate": None,
                "market_fee_status": self._market_fee_status,
                "note": f"Unexpected error: {e}. Effective fee_source unchanged.",
            }

        verified = mkt["verified_remote"]
        self._market_fee_status = mkt["status"]

        if verified:
            # CLOB gerçekten cevap verdi ve fee_rate alanı vardı → güvenilir
            self.fee_rate = mkt["fee_rate"]
            self.fee_source = "market_discovery"
            self.fee_status = "market_verified"
            self._market_fee_ok = True
        else:
            # CLOB cevap vermedi veya fee_rate alanı yoktu → effective source değişmez
            # fee_source config/fallback olarak kalır — market_discovery iddia edilmez
            self._market_fee_ok = False

        await log_module.log("fee_market_discovery_attempt", {
            "token_id": token_id,
            "verified_remote": verified,
            "market_fee_rate": mkt["fee_rate"],
            "market_fee_status": mkt["status"],
            "effective_fee_source": self.fee_source,
            "effective_fee_status": self.fee_status,
            "effective_fee_rate": self.fee_rate,
            "note": mkt["note"],
        })

        return {
            "attempted": True,
            "verified_remote": verified,
            "market_fee_rate": mkt["fee_rate"] if verified else None,
            "market_fee_status": mkt["status"],
            "note": mkt["note"],
        }

    def open_position(
        self,
        up_ask: float,
        down_ask: float,
        shares: int,
        btc_open: float,
        window_ts: int,
        interval: str = "5m",
        market_context: dict | None = None,
    ) -> PaperPosition:
        """Dual entry simüle et — her iki taraf ask'tan fill edildi kabul edilir."""
        self._counter += 1
        pair_sum = round(up_ask + down_ask, 4)
        net_edge = round(1.0 - pair_sum, 4)
        ctx = market_context or {}

        # Fee hesapla — entry anında, config'den okunan parametrelerle
        fee_up = compute_fee(shares, up_ask, self.fee_rate, self.fee_exponent)
        fee_down = compute_fee(shares, down_ask, self.fee_rate, self.fee_exponent)
        fee_total = round(fee_up + fee_down, 4)

        pos = PaperPosition(
            trade_id=f"dual-{int(time.time())}-{self._counter}",
            up_ask=up_ask,
            down_ask=down_ask,
            pair_sum=pair_sum,
            net_edge=net_edge,
            shares=shares,
            btc_open=btc_open,
            opened_at=time.time(),
            window_ts=window_ts,
            interval=interval,
            fee_up=fee_up,
            fee_down=fee_down,
            fee_total=fee_total,
            fee_rate=self.fee_rate,
            fee_exponent=self.fee_exponent,
            fee_source=self.fee_source,
            fee_status=self.fee_status,
            execution_lane=self.execution_lane,
            market_slug=ctx.get("market_slug", ""),
            btc_mid_binance=ctx.get("btc_mid_binance", 0.0),
            up_bid=ctx.get("up_bid", 0.0),
            down_bid=ctx.get("down_bid", 0.0),
            spread_up_pct=ctx.get("spread_up_pct", 0.0),
            spread_down_pct=ctx.get("spread_down_pct", 0.0),
            secs_to_res=ctx.get("secs_to_res", 0),
        )
        self._positions.append(pos)

        # Truth observation — trade_opened (with market context)
        truth_logger.observe_sync("trade_opened", {
            "trade_id": pos.trade_id,
            "market_slug": pos.market_slug,
            "interval": pos.interval,
            "window_ts": pos.window_ts,
            "execution_lane": pos.execution_lane,
            "pair_sum": pos.pair_sum,
            "up_bid": pos.up_bid,
            "up_ask": pos.up_ask,
            "down_bid": pos.down_bid,
            "down_ask": pos.down_ask,
            "spread_up_pct": pos.spread_up_pct,
            "spread_down_pct": pos.spread_down_pct,
            "net_edge": pos.net_edge,
            "shares": pos.shares,
            "btc_open": pos.btc_open,
            "btc_mid_binance": pos.btc_mid_binance,
            "secs_to_res": pos.secs_to_res,
            "fee_total": pos.fee_total,
            "fee_rate": pos.fee_rate,
            "fee_source": pos.fee_source,
            "fee_status": pos.fee_status,
        })

        if self.risk_manager:
            self.risk_manager.on_trade_opened()

        return pos

    def open_single_position(
        self,
        side: str,
        entry_price: float,
        shares: int,
        btc_open: float,
        window_ts: int,
        interval: str = "5m",
        market_context: dict | None = None,
    ) -> PaperPosition:
        """Single-side entry — one side at ask, taker fill."""
        self._counter += 1
        ctx = market_context or {}

        fee_entry = compute_fee(shares, entry_price, self.fee_rate, self.fee_exponent)

        pos = PaperPosition(
            trade_id=f"single-{side}-{int(time.time())}-{self._counter}",
            up_ask=ctx.get("up_ask", entry_price if side == "up" else 0.0),
            down_ask=ctx.get("down_ask", entry_price if side == "down" else 0.0),
            pair_sum=0.0,
            net_edge=0.0,
            shares=shares,
            btc_open=btc_open,
            opened_at=time.time(),
            window_ts=window_ts,
            interval=interval,
            fee_up=fee_entry if side == "up" else 0.0,
            fee_down=fee_entry if side == "down" else 0.0,
            fee_total=fee_entry,
            fee_rate=self.fee_rate,
            fee_exponent=self.fee_exponent,
            fee_source=self.fee_source,
            fee_status=self.fee_status,
            execution_lane=self.execution_lane,
            side=side,
            entry_price=entry_price,
            market_slug=ctx.get("market_slug", ""),
            btc_mid_binance=ctx.get("btc_mid_binance", 0.0),
            up_bid=ctx.get("up_bid", 0.0),
            down_bid=ctx.get("down_bid", 0.0),
            spread_up_pct=ctx.get("spread_up_pct", 0.0),
            spread_down_pct=ctx.get("spread_down_pct", 0.0),
            secs_to_res=ctx.get("secs_to_res", 0),
        )
        self._positions.append(pos)

        truth_logger.observe_sync("trade_opened", {
            "trade_id": pos.trade_id,
            "strategy": "single_side_taker",
            "side": side,
            "entry_price": entry_price,
            "market_slug": pos.market_slug,
            "interval": pos.interval,
            "window_ts": pos.window_ts,
            "execution_lane": pos.execution_lane,
            "shares": pos.shares,
            "btc_open": pos.btc_open,
            "btc_mid_binance": pos.btc_mid_binance,
            "secs_to_res": pos.secs_to_res,
            "fee_total": pos.fee_total,
            "fee_rate": pos.fee_rate,
            "fee_source": pos.fee_source,
            "fee_status": pos.fee_status,
        })

        if self.risk_manager:
            self.risk_manager.on_trade_opened()

        return pos

    async def resolve_pending(self, wait_secs: int | None = None) -> list[PaperPosition]:
        """Bekleyen pozisyonları resolve et — resolution_truth layer üzerinden."""
        pending = [pos for pos in self._positions if not pos.resolved]
        if not pending:
            return []

        wait = wait_secs if wait_secs is not None else self.resolve_confirm_secs
        if wait > 0:
            await log_module.log("resolve_waiting", {"secs": wait, "count": len(pending)})
            await asyncio.sleep(wait)

        resolved = []
        for pos in pending:
            try:
                # Resolution truth layer — Binance + Chainlink (placeholder)
                truth = await resolve_truth(
                    window_ts=pos.window_ts,
                    interval=pos.interval,
                    btc_open=pos.btc_open,
                )

                # Truth fields — her durumda doldur
                pos.winner_source = truth.winner_source
                pos.winner_binance = truth.winner_binance
                pos.winner_chainlink = truth.winner_chainlink
                pos.resolution_truth_status = truth.resolution_truth_status
                pos.resolution_match = truth.resolution_match
                pos.chainlink_status = truth.chainlink_status

                # Fetch başarısız → trade'i final olarak kapatma
                if truth.winner_source == "none" or truth.winner_binance == "unknown":
                    now = time.time()
                    pos.resolution_blocked = True
                    pos.result = "unresolved"
                    pos.pnl = 0.0
                    pos.btc_close = 0.0
                    pos.winning_side = ""
                    pos.resolution_retry_count += 1
                    pos.last_resolution_attempt_ts = now
                    if pos.first_resolution_failure_ts == 0.0:
                        pos.first_resolution_failure_ts = now
                    # resolved=False kalır — trade hâlâ açık sayılır

                    secs_since_first = round(now - pos.first_resolution_failure_ts, 1)

                    blocked_data = {
                        "trade_id": pos.trade_id,
                        "timestamp": now,
                        "window": pos.window_ts,
                        "interval": pos.interval,
                        "btc_open": pos.btc_open,
                        "resolution_truth_status": truth.resolution_truth_status,
                        "winner_source": truth.winner_source,
                        "winner_binance": truth.winner_binance,
                        "chainlink_status": truth.chainlink_status,
                        "retry_count": pos.resolution_retry_count,
                        "first_failure_ts": pos.first_resolution_failure_ts,
                        "secs_since_first_failure": secs_since_first,
                        "note": "Truth layer could not determine winner. "
                                "Trade NOT finalized. No PnL assigned.",
                    }
                    await log_module.log("trade_resolution_blocked", blocked_data)
                    await truth_logger.observe("trade_resolution_blocked", {
                        **blocked_data,
                        "execution_lane": pos.execution_lane,
                        "fee_source": pos.fee_source,
                        "fee_status": pos.fee_status,
                        "pair_sum": pos.pair_sum,
                    })
                    continue

                # Valid winner var — trade'i finalize et
                winning_side = truth.winner_binance
                result = f"win_{winning_side}"

                # PnL: fee-aware — branch on dual vs single
                if pos.side:
                    # Single-side PnL
                    if winning_side == pos.side:
                        # Win: payout=shares, cost=shares*entry_price
                        gross_pnl = round(pos.shares * (1.0 - pos.entry_price), 4)
                    else:
                        # Lose: payout=0, cost=shares*entry_price
                        gross_pnl = round(-(pos.shares * pos.entry_price), 4)
                    net_pnl = round(gross_pnl - pos.fee_total, 4)
                else:
                    # Dual-side PnL (unchanged)
                    gross_pnl = round(pos.shares * pos.net_edge, 4)
                    net_pnl = round(gross_pnl - pos.fee_total, 4)

                pos.resolved = True
                pos.btc_close = truth.btc_close_binance
                pos.winning_side = winning_side
                pos.result = result
                pos.gross_pnl = gross_pnl
                pos.net_pnl = net_pnl

                if self.risk_manager:
                    self.risk_manager.on_trade_result(net_pnl)

                resolved_data = {
                    "trade_id": pos.trade_id,
                    "market_slug": pos.market_slug,
                    "timestamp": time.time(),
                    "window": pos.window_ts,
                    "interval": pos.interval,
                    "up_bid": pos.up_bid,
                    "up_ask": pos.up_ask,
                    "down_bid": pos.down_bid,
                    "down_ask": pos.down_ask,
                    "spread_up": pos.spread_up_pct,
                    "spread_down": pos.spread_down_pct,
                    "pair_sum": pos.pair_sum,
                    "net_edge": pos.net_edge,
                    "shares": pos.shares,
                    "btc_open": pos.btc_open,
                    "btc_mid_binance": pos.btc_mid_binance,
                    "secs_to_res": pos.secs_to_res,
                    "btc_close": pos.btc_close,
                    "result": pos.result,
                    "fee_up": pos.fee_up,
                    "fee_down": pos.fee_down,
                    "fee_total": pos.fee_total,
                    "fee_rate": pos.fee_rate,
                    "fee_exponent": pos.fee_exponent,
                    "fee_source": pos.fee_source,
                    "fee_status": pos.fee_status,
                    "execution_lane": pos.execution_lane,
                    "gross_pnl": pos.gross_pnl,
                    "net_pnl": pos.net_pnl,
                    "winner_source": pos.winner_source,
                    "winner_binance": pos.winner_binance,
                    "winner_chainlink": pos.winner_chainlink,
                    "resolution_truth_status": pos.resolution_truth_status,
                    "resolution_match": pos.resolution_match,
                    "chainlink_status": pos.chainlink_status,
                }
                await log_module.log("trade_resolved", resolved_data)
                await truth_logger.observe("trade_resolved", resolved_data)
                resolved.append(pos)

            except Exception as e:
                await log_module.log("resolve_error", {
                    "trade_id": pos.trade_id, "error": str(e),
                })

        return resolved

    def open_positions(self) -> list[PaperPosition]:
        return [pos for pos in self._positions if not pos.resolved]

    def all_positions(self) -> list[PaperPosition]:
        return list(self._positions)

    def total_pnl(self) -> float:
        """Net PnL (fee-aware) — sadece resolved pozisyonlar."""
        return sum(pos.net_pnl for pos in self._positions if pos.resolved)

    def unresolved_positions(self) -> list[PaperPosition]:
        """Resolution blocked olan pozisyonlar."""
        return [pos for pos in self._positions if pos.resolution_blocked]

    def summary_stats(self) -> dict:
        """10 pencere sonunda rapor için istatistikler."""
        all_pos = self._positions
        resolved = [pos for pos in all_pos if pos.resolved]
        blocked = [pos for pos in all_pos if pos.resolution_blocked]
        max_retry = max((pos.resolution_retry_count for pos in blocked), default=0)
        fallback_count = sum(1 for pos in all_pos if pos.fee_source == "fallback")
        if not resolved:
            return {
                "trades": 0,
                "total_gross_pnl": 0.0, "total_fee_paid": 0.0, "total_net_pnl": 0.0,
                "avg_net_edge": 0.0,
                "win_up": 0, "win_down": 0,
                "unresolved_count": len(blocked),
                "blocked_resolution_count": len(blocked),
                "max_resolution_retry_count": max_retry,
                "fee_source": self.fee_source,
                "fee_status": self.fee_status,
                "fallback_fee_usage_count": fallback_count,
                "execution_lane": self.execution_lane,
                "market_fee_attempted": self._market_fee_attempted,
                "market_fee_ok": self._market_fee_ok,
                "market_fee_status": self._market_fee_status,
            }
        total_gross = sum(pos.gross_pnl for pos in resolved)
        total_fee = sum(pos.fee_total for pos in resolved)
        total_net = sum(pos.net_pnl for pos in resolved)
        avg_edge = sum(pos.net_edge for pos in resolved) / len(resolved)
        win_up = sum(1 for pos in resolved if pos.result == "win_up")
        win_down = sum(1 for pos in resolved if pos.result == "win_down")
        return {
            "trades": len(resolved),
            "total_gross_pnl": round(total_gross, 4),
            "total_fee_paid": round(total_fee, 4),
            "total_net_pnl": round(total_net, 4),
            "avg_net_edge": round(avg_edge, 4),
            "avg_fee_per_trade": round(total_fee / len(resolved), 4),
            "avg_net_pnl_per_trade": round(total_net / len(resolved), 4),
            "win_up": win_up,
            "win_down": win_down,
            "unresolved_count": len(blocked),
            "blocked_resolution_count": len(blocked),
            "max_resolution_retry_count": max_retry,
            "fee_source": self.fee_source,
            "fee_status": self.fee_status,
            "fallback_fee_usage_count": fallback_count,
            "execution_lane": self.execution_lane,
            "market_fee_attempted": self._market_fee_attempted,
            "market_fee_ok": self._market_fee_ok,
            "dual_fill_rate": "100%",  # paper modda her zaman %100
        }
