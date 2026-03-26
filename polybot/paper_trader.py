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
    # unresolved lifecycle tracking
    resolution_retry_count: int = 0
    first_resolution_failure_ts: float = 0.0
    last_resolution_attempt_ts: float = 0.0


class PaperTrader:
    def __init__(self, config: dict, risk_manager=None):
        self.resolve_confirm_secs: int = config.get("resolve_confirm_secs", 130)
        self.shares_per_side: int = config.get("shares_per_side", 5)
        self.fee_rate: float = config.get("fee_rate", 0.072)
        self.fee_exponent: float = config.get("fee_exponent", 1.0)
        self._positions: list[PaperPosition] = []
        self._counter: int = 0
        self.risk_manager = risk_manager

    def open_position(
        self,
        up_ask: float,
        down_ask: float,
        shares: int,
        btc_open: float,
        window_ts: int,
        interval: str = "5m",
    ) -> PaperPosition:
        """Dual entry simüle et — her iki taraf ask'tan fill edildi kabul edilir."""
        self._counter += 1
        pair_sum = round(up_ask + down_ask, 4)
        net_edge = round(1.0 - pair_sum, 4)

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
        )
        self._positions.append(pos)

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

                    await log_module.log("trade_resolution_blocked", {
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
                    })
                    continue

                # Valid winner var — trade'i finalize et
                winning_side = truth.winner_binance
                result = f"win_{winning_side}"

                # PnL: fee-aware
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

                await log_module.log("trade_resolved", {
                    "trade_id": pos.trade_id,
                    "timestamp": time.time(),
                    "window": pos.window_ts,
                    "interval": pos.interval,
                    "up_ask": pos.up_ask,
                    "down_ask": pos.down_ask,
                    "pair_sum": pos.pair_sum,
                    "net_edge": pos.net_edge,
                    "shares": pos.shares,
                    "btc_open": pos.btc_open,
                    "btc_close": pos.btc_close,
                    "result": pos.result,
                    "fee_up": pos.fee_up,
                    "fee_down": pos.fee_down,
                    "fee_total": pos.fee_total,
                    "fee_rate": pos.fee_rate,
                    "fee_exponent": pos.fee_exponent,
                    "gross_pnl": pos.gross_pnl,
                    "net_pnl": pos.net_pnl,
                    "winner_source": pos.winner_source,
                    "winner_binance": pos.winner_binance,
                    "winner_chainlink": pos.winner_chainlink,
                    "resolution_truth_status": pos.resolution_truth_status,
                    "resolution_match": pos.resolution_match,
                    "chainlink_status": pos.chainlink_status,
                })
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
        resolved = [pos for pos in self._positions if pos.resolved]
        blocked = [pos for pos in self._positions if pos.resolution_blocked]
        max_retry = max((pos.resolution_retry_count for pos in blocked), default=0)
        if not resolved:
            return {
                "trades": 0,
                "total_gross_pnl": 0.0, "total_fee_paid": 0.0, "total_net_pnl": 0.0,
                "avg_net_edge": 0.0,
                "win_up": 0, "win_down": 0,
                "unresolved_count": len(blocked),
                "blocked_resolution_count": len(blocked),
                "max_resolution_retry_count": max_retry,
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
            "dual_fill_rate": "100%",  # paper modda her zaman %100
        }
