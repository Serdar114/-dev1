"""
Paper Trader — Dual Side Capture + Single Side Taker simulation.

Trade akışı (dual):
  1. DUAL_ENTRY sinyali → open_position() → her iki taraf ask'tan girilir
  2. Fee: fee_up + fee_down
  3. Resolve: gross_pnl = shares * net_edge; net_pnl = gross_pnl - fee_total

Trade akışı (single):
  1. SINGLE_ENTRY sinyali → open_single_position() → bir taraf ask'tan girilir
  2. Fee: fee_entry (one side only)
  3. Resolve:
       side wins  → gross_pnl = shares * (1.0 - entry_price)
       side loses → gross_pnl = shares * (-entry_price)
       net_pnl = gross_pnl - fee_total

fee_rate ve fee_exponent config'den okunur — hardcode yok.

Integrity invariants (enforced here):
  - max 1 open_position() OR open_single_position() per window_ts (_opened_window_ts guard)
  - max 1 trade_resolved per trade_id (_resolved_ids guard)
  - Single canonical writer for lifecycle events: log_module only.
    truth_logger is NOT used here for trade_opened/trade_resolved.
"""

import asyncio
import aiohttp
import time
from dataclasses import dataclass
import logger as log_module
from resolution_truth import resolve_truth
from fee_engine import compute_fee


@dataclass
class PaperPosition:
    trade_id: str
    up_ask: float
    down_ask: float
    pair_sum: float      # up_ask + down_ask (dual); 0.0 for single
    net_edge: float      # 1.0 - pair_sum (dual); 0.0 for single
    shares: int
    btc_open: float
    opened_at: float
    window_ts: int
    interval: str = "5m"
    # single-side fields (empty/0 for dual)
    side: str = ""           # "" = dual; "up" | "down" = single
    entry_price: float = 0.0 # ask of chosen side (single only)
    # fee fields (entry anında hesaplanır)
    fee_up: float = 0.0
    fee_down: float = 0.0
    fee_total: float = 0.0
    fee_rate: float = 0.0
    fee_exponent: float = 0.0
    # resolve sonrası
    resolved: bool = False
    resolution_blocked: bool = False
    btc_close: float = 0.0
    winning_side: str = ""
    result: str = ""
    gross_pnl: float = 0.0
    net_pnl: float = 0.0
    # resolution truth fields
    winner_source: str = ""
    winner_binance: str = ""
    winner_chainlink: str = ""
    resolution_truth_status: str = ""
    resolution_match: str = ""
    chainlink_status: str = ""
    # fee source visibility
    fee_source: str = ""
    fee_status: str = ""
    # execution lane
    execution_lane: str = ""
    # market context at entry
    market_slug: str = ""
    btc_mid_binance: float = 0.0
    up_bid: float = 0.0
    down_bid: float = 0.0
    spread_up_pct: float = 0.0
    spread_down_pct: float = 0.0
    secs_to_res: int = 0
    # unresolved lifecycle tracking
    resolution_retry_count: int = 0
    first_resolution_failure_ts: float = 0.0
    last_resolution_attempt_ts: float = 0.0


class PaperTrader:
    def __init__(self, config: dict, risk_manager=None):
        self.resolve_confirm_secs: int = config.get("resolve_confirm_secs", 130)
        self.shares_per_side: int = config.get("shares_per_side", 5)

        # --- fee_rate: config-explicit or fallback (labeled) ---
        fee_rate_configured = "fee_rate" in config
        self.fee_rate: float = config.get("fee_rate", 0.072)
        self.fee_source: str = "config" if fee_rate_configured else "fallback"
        self.fee_status: str = "configured" if fee_rate_configured else "fallback_used"

        # --- fee_exponent: config-explicit or fallback (labeled) ---
        fee_exp_configured = "fee_exponent" in config
        self.fee_exponent: float = config.get("fee_exponent", 1.0)
        self.fee_exponent_source: str = "config" if fee_exp_configured else "fallback"

        # --- tick_size: config-explicit or fallback (labeled) ---
        tick_size_configured = "tick_size" in config
        self.tick_size: float = config.get("tick_size", 0.01)
        self.tick_size_source: str = "config" if tick_size_configured else "fallback"

        # --- min_order_size: config-explicit or fallback (labeled) ---
        min_order_configured = "min_order_size" in config
        self.min_order_size: float = config.get("min_order_size", 1.0)
        self.min_order_size_source: str = "config" if min_order_configured else "fallback"

        # Market discovery fee attempt tracking
        self._market_fee_attempted: bool = False
        self._market_fee_ok: bool = False
        self._market_fee_status: str = "not_attempted"

        # Execution lane — paper mode always simulates taker fills at ask, 100% fill
        self.execution_lane: str = "taker_paper"

        self._positions: list[PaperPosition] = []
        self._counter: int = 0
        self.risk_manager = risk_manager

        # Integrity guards
        # max 1 open per window_ts (covers both dual and single)
        self._opened_window_ts: set[int] = set()
        # max 1 trade_resolved per trade_id
        self._resolved_ids: set[str] = set()

    async def try_update_fee_from_market(self, token_id: str) -> dict:
        """
        Market discovery'den fee rate çekmeyi dene.

        Epistemik kural:
          - verified_remote=True  → fee_source="market_discovery", fee_rate güncellenir
          - verified_remote=False → fee_source DEĞİŞMEZ (config/fallback kalır)
        """
        from market_discovery import get_market_fee_rate
        self._market_fee_attempted = True
        try:
            mkt = await get_market_fee_rate(token_id)
        except Exception as e:
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
            self.fee_rate = mkt["fee_rate"]
            self.fee_source = "market_discovery"
            self.fee_status = "market_verified"
            self._market_fee_ok = True
        else:
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
        """Dual entry — both sides at ask, taker fill.

        Integrity: max 1 call per window_ts.
        """
        if window_ts in self._opened_window_ts:
            log_module.log_sync("integrity_violation", {
                "violation": "duplicate_open_position",
                "window_ts": window_ts,
                "counter": self._counter,
            })
            raise RuntimeError(
                f"open_position called twice for window_ts={window_ts}. "
                "Check _signal_sent guard in main.py."
            )

        self._counter += 1
        pair_sum = round(up_ask + down_ask, 4)
        net_edge = round(1.0 - pair_sum, 4)
        ctx = market_context or {}

        fee_up = compute_fee(shares, up_ask, self.fee_rate, self.fee_exponent)
        fee_down = compute_fee(shares, down_ask, self.fee_rate, self.fee_exponent)
        fee_total = round(fee_up + fee_down, 4)

        pos = PaperPosition(
            trade_id=f"dual-{int(time.time())}-{self._counter}",
            up_ask=up_ask, down_ask=down_ask,
            pair_sum=pair_sum, net_edge=net_edge,
            shares=shares, btc_open=btc_open,
            opened_at=time.time(), window_ts=window_ts, interval=interval,
            fee_up=fee_up, fee_down=fee_down, fee_total=fee_total,
            fee_rate=self.fee_rate, fee_exponent=self.fee_exponent,
            fee_source=self.fee_source, fee_status=self.fee_status,
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
        self._opened_window_ts.add(window_ts)

        # Canonical trade_opened write is in main.py. truth_logger NOT used here.
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
        """Single-side entry — one side at ask, taker fill.

        PnL at resolve:
          side wins  → gross = shares * (1.0 - entry_price)
          side loses → gross = shares * (-entry_price)

        Integrity: same _opened_window_ts guard as open_position.
        """
        if window_ts in self._opened_window_ts:
            log_module.log_sync("integrity_violation", {
                "violation": "duplicate_open_single_position",
                "window_ts": window_ts,
                "side": side,
                "counter": self._counter,
            })
            raise RuntimeError(
                f"open_single_position called twice for window_ts={window_ts}. "
                "Check _signal_sent guard in main.py."
            )

        self._counter += 1
        ctx = market_context or {}

        fee_entry = compute_fee(shares, entry_price, self.fee_rate, self.fee_exponent)
        fee_up = fee_entry if side == "up" else 0.0
        fee_down = fee_entry if side == "down" else 0.0

        pos = PaperPosition(
            trade_id=f"single-{side}-{int(time.time())}-{self._counter}",
            up_ask=ctx.get("up_ask", entry_price if side == "up" else 0.0),
            down_ask=ctx.get("down_ask", entry_price if side == "down" else 0.0),
            pair_sum=0.0, net_edge=0.0,
            shares=shares, btc_open=btc_open,
            opened_at=time.time(), window_ts=window_ts, interval=interval,
            side=side, entry_price=entry_price,
            fee_up=fee_up, fee_down=fee_down, fee_total=fee_entry,
            fee_rate=self.fee_rate, fee_exponent=self.fee_exponent,
            fee_source=self.fee_source, fee_status=self.fee_status,
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
        self._opened_window_ts.add(window_ts)

        # Canonical trade_opened write is in main.py. truth_logger NOT used here.
        if self.risk_manager:
            self.risk_manager.on_trade_opened()

        return pos

    async def resolve_pending(self, wait_secs: int | None = None) -> list[PaperPosition]:
        """Resolve pending positions via resolution_truth layer.

        Integrity:
          - max 1 resolution per trade_id (_resolved_ids guard)
          - Single canonical writer: log_module only. truth_logger NOT used here.
        """
        pending = [pos for pos in self._positions if not pos.resolved]
        if not pending:
            return []

        wait = wait_secs if wait_secs is not None else self.resolve_confirm_secs
        if wait > 0:
            await log_module.log("resolve_waiting", {"secs": wait, "count": len(pending)})
            await asyncio.sleep(wait)

        resolved = []
        for pos in pending:
            # Guard: skip if already resolved in a previous resolve_pending() call
            if pos.trade_id in self._resolved_ids:
                await log_module.log("integrity_violation", {
                    "violation": "duplicate_resolve_attempt",
                    "trade_id": pos.trade_id,
                    "window_ts": pos.window_ts,
                })
                continue

            try:
                truth = await resolve_truth(
                    window_ts=pos.window_ts,
                    interval=pos.interval,
                    btc_open=pos.btc_open,
                )

                pos.winner_source = truth.winner_source
                pos.winner_binance = truth.winner_binance
                pos.winner_chainlink = truth.winner_chainlink
                pos.resolution_truth_status = truth.resolution_truth_status
                pos.resolution_match = truth.resolution_match
                pos.chainlink_status = truth.chainlink_status

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

                    secs_since_first = round(now - pos.first_resolution_failure_ts, 1)
                    # Single canonical writer: log_module only.
                    await log_module.log("trade_resolution_blocked", {
                        "trade_id": pos.trade_id,
                        "timestamp": now,
                        "window": pos.window_ts,
                        "interval": pos.interval,
                        "btc_open": pos.btc_open,
                        "side": pos.side,
                        "resolution_truth_status": truth.resolution_truth_status,
                        "winner_source": truth.winner_source,
                        "winner_binance": truth.winner_binance,
                        "chainlink_status": truth.chainlink_status,
                        "retry_count": pos.resolution_retry_count,
                        "first_failure_ts": pos.first_resolution_failure_ts,
                        "secs_since_first_failure": secs_since_first,
                        "note": "Truth layer could not determine winner. "
                                "Trade NOT finalized. No PnL assigned.",
                    })
                    continue

                # Valid winner — finalize
                winning_side = truth.winner_binance

                # PnL: dual vs single
                if pos.side:
                    # single-side: win = (1.0 - entry_price), lose = (-entry_price)
                    if winning_side == pos.side:
                        gross_pnl = round(pos.shares * (1.0 - pos.entry_price), 4)
                    else:
                        gross_pnl = round(pos.shares * (-pos.entry_price), 4)
                else:
                    # dual-side: always wins one leg at 1.0
                    gross_pnl = round(pos.shares * pos.net_edge, 4)

                net_pnl = round(gross_pnl - pos.fee_total, 4)

                pos.resolved = True
                pos.resolution_blocked = False  # clear any prior retry-block flag
                pos.btc_close = truth.btc_close_binance
                pos.winning_side = winning_side
                pos.result = f"win_{winning_side}"
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
                    "side": pos.side,
                    "entry_price": pos.entry_price,
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
                    "winning_side": pos.winning_side,
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
                # Single canonical writer: log_module only.
                await log_module.log("trade_resolved", resolved_data)
                self._resolved_ids.add(pos.trade_id)
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
        return sum(pos.net_pnl for pos in self._positions if pos.resolved)

    def unresolved_positions(self) -> list[PaperPosition]:
        return [pos for pos in self._positions if pos.resolution_blocked]

    def summary_stats(self) -> dict:
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
            "dual_fill_rate": "100%",
        }
