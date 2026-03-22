"""
logger/summary.py — Session / daily summary reporter.

Summary fields (per spec §11):
    windows_observed
    candidate_windows
    maker_candidate_count
    taker_candidate_count
    maker_fills
    taker_fills
    maker_fill_rate
    taker_execution_rate
    maker_avg_quote_bucket
    filtered_directional_accuracy
    fill_conditioned_WR
    avg_fill_price
    basis_mismatch_frequency
    kill_condition_status
    verdict: continue / tighten / kill

Per-window log fields include:
    quote_bucket
    break_even_wr_estimate
    win_if_correct
    loss_if_wrong
    chainlink_gap_seconds
    fast_feed_gap_seconds
    basis_mismatch_bps
    basis_mismatch (flag)
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Optional

from validator.kill_conditions import KillAction, KillCheckResult, SessionStats

logger = logging.getLogger(__name__)

# Bucket ordering for avg_quote_bucket computation
_BUCKET_VALUES = {"B1": 1, "B2": 2, "B3": 3}


@dataclass
class WindowLog:
    """
    Per-window log record written after each window is evaluated.
    All fields relevant to audit and kill condition tracking.
    """
    window_open_ts: int
    slug: str
    phase: str

    # Feed
    fast_price: Optional[float] = None
    chainlink_price: Optional[float] = None
    fast_feed_gap_seconds: Optional[float] = None
    chainlink_gap_seconds: Optional[float] = None
    fast_feed_stale: bool = False
    chainlink_feed_stale: bool = False
    basis_bps: Optional[float] = None
    basis_mismatch_bps: Optional[float] = None
    basis_mismatch: bool = False

    # Signal
    signal_direction: str = "NONE"
    signal_eligible: bool = False
    gate_summary: str = ""
    signal_rejection_reasons: str = ""   # Pipe-separated list of rejection reasons
    current_yes_mid: Optional[float] = None  # Polymarket YES probability at signal time

    # Maker lane
    maker_quote_bucket: str = "N/A"
    maker_intended_price: Optional[float] = None
    maker_filled: bool = False
    maker_fill_price: Optional[float] = None
    maker_fill_realism_grade: str = "N/A"   # OBSERVED_PATH | PROVISIONAL_NO_PATH | N/A
    maker_bankroll_fraction: Optional[float] = None
    maker_break_even_wr: Optional[float] = None
    maker_win_if_correct: Optional[float] = None
    maker_loss_if_wrong: Optional[float] = None
    maker_net_pnl: Optional[float] = None
    maker_outcome_correct: Optional[bool] = None

    # Taker lane
    taker_intended_price: Optional[float] = None
    taker_filled: bool = False
    taker_fill_price: Optional[float] = None
    taker_decision_ts: Optional[float] = None
    taker_execution_source: str = "UNAVAILABLE"
    taker_assumed_slippage_bps: float = 0.0
    taker_fee_per_share: Optional[float] = None
    taker_total_fee: Optional[float] = None
    taker_bankroll_fraction: Optional[float] = None
    taker_break_even_wr: Optional[float] = None
    taker_win_if_correct: Optional[float] = None
    taker_loss_if_wrong: Optional[float] = None
    taker_net_pnl: Optional[float] = None
    taker_outcome_correct: Optional[bool] = None

    # Settlement
    settlement_source: str = "UNAVAILABLE"   # CHAINLINK_PROXY | FAST_PROXY | UNAVAILABLE

    # Daily caps state snapshot
    caps_candidates_today: int = 0
    caps_entries_today: int = 0
    caps_position_open: bool = False

    # Discovery
    discovery_ok: bool = False
    discovery_latency_ms: Optional[float] = None

    # Open price capture delay
    open_price_capture_delay_seconds: Optional[float] = None

    # YES price input tracking (for signal + execution audit)
    yes_price_source: Optional[str] = None          # "clob_midpoint" | "clob_ask" | None
    yes_price_is_provisional: Optional[bool] = None # True if REST-polled, False if WebSocket
    signal_input_missing: Optional[str] = None      # e.g. "yes_mid_unavailable"
    maker_path_source: Optional[str] = None         # fill realism grade for this window
    maker_path_points_collected: int = 0            # number of intra-window prices collected
    runtime_mode_effective: Optional[str] = None    # "STRICT" | "PROVISIONAL"


class SummaryReporter:
    """
    Collects WindowLog records and produces session summaries.

    Output: structured JSON log lines + human-readable summary blocks.
    """

    def __init__(self, config: dict, phase: str) -> None:
        log_cfg = config.get("logging", {})
        self._log_dir = log_cfg.get("log_dir", "logs")
        self._summary_interval = log_cfg.get("summary_interval_windows", 50)
        self._phase = phase
        # Read initial bankroll from config — no hardcoded values.
        self._initial_bankroll = float(
            config.get("sizing", {}).get("initial_bankroll", 30.0)
        )

        os.makedirs(self._log_dir, exist_ok=True)
        ts = int(time.time())
        self._window_log_path = os.path.join(self._log_dir, f"windows_{ts}.jsonl")
        self._summary_log_path = os.path.join(self._log_dir, f"summary_{ts}.txt")

        self._windows: list[WindowLog] = []
        self._session_start = time.time()

        logger.info(
            "[summary] Phase=%s window_log=%s summary_log=%s",
            phase, self._window_log_path, self._summary_log_path
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record_window(self, wl: WindowLog) -> None:
        """Append a window log record and flush to JSONL file."""
        self._windows.append(wl)
        try:
            with open(self._window_log_path, "a") as f:
                f.write(json.dumps(asdict(wl)) + "\n")
        except OSError as exc:
            logger.error("[summary] Failed to write window log: %s", exc)

        if len(self._windows) % self._summary_interval == 0:
            self.print_interim_summary()

    def build_session_stats(self) -> SessionStats:
        """Build a SessionStats snapshot from all recorded windows."""
        stats = SessionStats(phase=self._phase)

        bucket_sum = 0
        bucket_count = 0
        prev_outcome_correct: Optional[bool] = None
        consecutive = 0

        for wl in self._windows:
            stats.windows_observed += 1
            stats.active_windows += 1

            # Feed health
            if wl.fast_feed_stale:
                stats.fast_stale_windows += 1
            if wl.chainlink_feed_stale:
                stats.chainlink_stale_windows += 1
            if wl.basis_mismatch:
                stats.basis_mismatch_windows += 1

            # Discovery
            stats.discovery_attempts += 1
            if not wl.discovery_ok:
                stats.discovery_failures += 1

            # Open price delay
            if wl.open_price_capture_delay_seconds is not None:
                stats.open_price_delay_sum += wl.open_price_capture_delay_seconds
                stats.open_price_delay_count += 1

            # Signal
            if wl.signal_direction != "NONE":
                stats.candidate_windows += 1

            # Raw direction (any window where we had a direction)
            if wl.signal_direction != "NONE" and wl.maker_outcome_correct is not None:
                stats.raw_direction_total += 1
                if wl.maker_outcome_correct:
                    stats.raw_direction_correct += 1

            # Filtered direction (signal was eligible)
            if wl.signal_eligible and wl.maker_outcome_correct is not None:
                stats.filtered_direction_total += 1
                if wl.maker_outcome_correct:
                    stats.filtered_direction_correct += 1

            # Maker lane
            if wl.maker_quote_bucket not in ("N/A", "INELIGIBLE"):
                stats.maker_candidate_count += 1
                bv = _BUCKET_VALUES.get(wl.maker_quote_bucket, 0)
                if bv:
                    bucket_sum += bv
                    bucket_count += 1

            if wl.maker_filled:
                stats.maker_fills += 1
                if wl.maker_fill_price is not None:
                    stats.avg_fill_price_sum += wl.maker_fill_price
                    stats.avg_fill_price_count += 1

            if wl.maker_outcome_correct is not None and wl.maker_filled:
                stats.fill_conditioned_total += 1
                if wl.maker_outcome_correct:
                    stats.fill_conditioned_wins += 1
                    consecutive = 0
                else:
                    consecutive += 1
                    stats.consecutive_losses = max(stats.consecutive_losses, consecutive)
                    # Track adverse P&L
                    if wl.maker_net_pnl is not None:
                        stats.adverse_fill_pnl += wl.maker_net_pnl

            # Taker lane
            if wl.taker_filled:
                stats.taker_fills += 1
            if wl.signal_direction != "NONE":
                stats.taker_candidate_count += 1

        stats.paper_bankroll = self._compute_bankroll(stats)
        return stats

    def print_interim_summary(self) -> None:
        stats = self.build_session_stats()
        self._write_summary_block(stats, interim=True)

    def print_final_summary(
        self,
        kill_result: Optional[KillCheckResult] = None,
    ) -> None:
        stats = self.build_session_stats()
        self._write_summary_block(stats, interim=False, kill_result=kill_result)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _compute_bankroll(self, stats: SessionStats) -> float:
        """Recompute paper bankroll from filled-trade P&L records.
        Uses initial_bankroll from config (no hardcoded value)."""
        total_pnl = sum(
            wl.maker_net_pnl
            for wl in self._windows
            if wl.maker_net_pnl is not None
        )
        return self._initial_bankroll + total_pnl

    def _write_summary_block(
        self,
        stats: SessionStats,
        interim: bool,
        kill_result: Optional[KillCheckResult] = None,
    ) -> None:
        elapsed = time.time() - self._session_start
        kind = "INTERIM" if interim else "FINAL"

        # Compute derived values
        maker_fill_rate = stats.maker_fill_rate()
        taker_exec_rate = (
            stats.taker_fills / stats.taker_candidate_count
            if stats.taker_candidate_count > 0 else None
        )
        filt_acc = stats.filtered_directional_accuracy()
        fc_wr = stats.fill_conditioned_wr()
        avg_price = stats.avg_fill_price()
        basis_freq = stats.basis_mismatch_frequency()
        cl_gap_freq = stats.chainlink_gap_frequency()
        fast_gap_freq = stats.fast_gap_frequency()

        # Average bucket numeric → label
        bucket_windows = [
            wl for wl in self._windows
            if wl.maker_quote_bucket not in ("N/A", "INELIGIBLE")
        ]
        if bucket_windows:
            avg_bucket_num = sum(
                _BUCKET_VALUES.get(wl.maker_quote_bucket, 0)
                for wl in bucket_windows
            ) / len(bucket_windows)
            avg_bucket_str = f"{avg_bucket_num:.2f}"
        else:
            avg_bucket_str = "N/A"

        # Kill condition status
        if kill_result is not None:
            kc_status = kill_result.verdict
            action_str = kill_result.recommended_action.value.upper()
        else:
            kc_status = "not_evaluated"
            action_str = "CONTINUE"

        # Verdict
        if kill_result is not None:
            verdict = kill_result.recommended_action.value
        else:
            verdict = "continue"

        lines = [
            "",
            f"{'='*62}",
            f"  RESEARCH BOT — {kind} SUMMARY",
            f"  Phase: {stats.phase}  |  Elapsed: {elapsed:.0f}s",
            f"  *** PAPER TRADING ONLY — see README for live risks ***",
            f"{'='*62}",
            f"  Windows observed         : {stats.windows_observed}",
            f"  Candidate windows        : {stats.candidate_windows}",
            f"  Maker candidate count    : {stats.maker_candidate_count}",
            f"  Taker candidate count    : {stats.taker_candidate_count}",
            f"  Maker fills              : {stats.maker_fills}",
            f"  Taker fills              : {stats.taker_fills}",
            f"  Maker fill rate          : {_pct(maker_fill_rate)}",
            f"  Taker execution rate     : {_pct(taker_exec_rate)}",
            f"  Maker avg quote bucket   : {avg_bucket_str}",
            f"{'--'*31}",
            f"  Filtered dir accuracy    : {_pct(filt_acc)} (n={stats.filtered_direction_total})",
            f"  Fill-conditioned WR      : {_pct(fc_wr)} (n={stats.fill_conditioned_total})",
            f"  Avg fill price           : {_val(avg_price, '.4f')}",
            f"{'--'*31}",
            f"  Basis mismatch freq      : {_pct(basis_freq)} (n={stats.active_windows})",
            f"  Chainlink gap freq       : {_pct(cl_gap_freq)}",
            f"  Fast feed gap freq       : {_pct(fast_gap_freq)}",
            f"  Paper bankroll           : ${stats.paper_bankroll:.2f}",
            f"  Consecutive losses       : {stats.consecutive_losses}",
            f"{'--'*31}",
            f"  Kill condition status    : {kc_status}",
            f"  Recommended action       : {action_str}",
            f"  VERDICT                  : {verdict.upper()}",
            f"{'='*62}",
            "",
        ]

        block = "\n".join(lines)
        logger.info(block)
        print(block)

        try:
            with open(self._summary_log_path, "a") as f:
                f.write(block + "\n")
        except OSError as exc:
            logger.error("[summary] Failed to write summary: %s", exc)


def _pct(v: Optional[float]) -> str:
    return f"{v*100:.1f}%" if v is not None else "N/A"


def _val(v: Optional[float], fmt: str = ".4f") -> str:
    return format(v, fmt) if v is not None else "N/A"
