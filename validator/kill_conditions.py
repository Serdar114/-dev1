"""
validator/kill_conditions.py — Kill conditions framework.

ALL kill condition logic lives here.  No kill checks appear anywhere else.

Design
------
Kill conditions are loaded from config/kill_conditions.yaml.
The validator receives a SessionStats snapshot and evaluates each
enabled condition.  It returns a KillCheckResult with:
    - List of triggered conditions
    - Recommended action: kill | tighten | warn | continue
    - Human-readable verdict string

Each condition is evaluated only when min_sample_size is reached (where
applicable) to prevent premature kills from small-sample noise.

Adding a new condition
----------------------
1. Add the condition to config/kill_conditions.yaml.
2. Add a corresponding _check_<name>() method here.
3. Register it in _ALL_CHECKS at the bottom of this file.
No other file needs to change.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class KillAction(str, Enum):
    CONTINUE = "continue"
    WARN = "warn"
    TIGHTEN = "tighten"
    KILL = "kill"

    def severity(self) -> int:
        """Higher = more severe."""
        return {"continue": 0, "warn": 1, "tighten": 2, "kill": 3}[self.value]


@dataclass
class ConditionResult:
    name: str
    triggered: bool
    action: KillAction
    threshold: float
    measured_value: Optional[float]
    description: str
    sample_size: int
    min_sample_size: int


@dataclass
class KillCheckResult:
    triggered_conditions: list[ConditionResult] = field(default_factory=list)
    recommended_action: KillAction = KillAction.CONTINUE
    verdict: str = "continue"
    detail_lines: list[str] = field(default_factory=list)


@dataclass
class SessionStats:
    """
    Snapshot of all metrics needed for kill condition evaluation.
    Passed to KillConditionValidator.check() after each window.
    """
    # Counts
    windows_observed: int = 0
    candidate_windows: int = 0
    maker_candidate_count: int = 0
    taker_candidate_count: int = 0
    maker_fills: int = 0            # raw: any filled=True (includes single-point grade)
    maker_evaluable_fills: int = 0  # conservative: filled=True AND fill_evaluable=True
    taker_fills: int = 0

    # Accuracy / win rate
    raw_direction_correct: int = 0       # windows where raw direction was right
    raw_direction_total: int = 0
    filtered_direction_correct: int = 0  # direction correct, after all gates
    filtered_direction_total: int = 0
    fill_conditioned_wins: int = 0       # wins on filled maker trades
    fill_conditioned_total: int = 0      # total filled maker trades

    # Fill quality
    adverse_fill_pnl: float = 0.0        # cumulative P&L from adverse fills
    avg_fill_price_sum: float = 0.0
    avg_fill_price_count: int = 0
    paper_bankroll: float = 1000.0

    # Feed health
    basis_mismatch_windows: int = 0      # windows with basis_mismatch flag
    chainlink_stale_windows: int = 0     # windows where Chainlink was stale
    fast_stale_windows: int = 0          # windows where fast feed was stale
    active_windows: int = 0              # windows where feed was expected

    # Discovery
    discovery_attempts: int = 0
    discovery_failures: int = 0

    # Open price capture
    open_price_delay_sum: float = 0.0
    open_price_delay_count: int = 0

    # Consecutive losses
    consecutive_losses: int = 0

    # Phase
    phase: str = "0c"

    # Derived helpers
    def raw_directional_accuracy(self) -> Optional[float]:
        if self.raw_direction_total == 0:
            return None
        return self.raw_direction_correct / self.raw_direction_total

    def filtered_directional_accuracy(self) -> Optional[float]:
        if self.filtered_direction_total == 0:
            return None
        return self.filtered_direction_correct / self.filtered_direction_total

    def fill_conditioned_wr(self) -> Optional[float]:
        if self.fill_conditioned_total == 0:
            return None
        return self.fill_conditioned_wins / self.fill_conditioned_total

    def maker_fill_rate(self) -> Optional[float]:
        if self.maker_candidate_count == 0:
            return None
        return self.maker_fills / self.maker_candidate_count

    def maker_evaluable_fill_rate(self) -> Optional[float]:
        """Fill rate counting only evaluable (multi-point) fills. Use for viability."""
        if self.maker_candidate_count == 0:
            return None
        return self.maker_evaluable_fills / self.maker_candidate_count

    def avg_fill_price(self) -> Optional[float]:
        if self.avg_fill_price_count == 0:
            return None
        return self.avg_fill_price_sum / self.avg_fill_price_count

    def basis_mismatch_frequency(self) -> Optional[float]:
        if self.active_windows == 0:
            return None
        return self.basis_mismatch_windows / self.active_windows

    def chainlink_gap_frequency(self) -> Optional[float]:
        if self.active_windows == 0:
            return None
        return self.chainlink_stale_windows / self.active_windows

    def fast_gap_frequency(self) -> Optional[float]:
        if self.active_windows == 0:
            return None
        return self.fast_stale_windows / self.active_windows

    def discovery_failure_rate(self) -> Optional[float]:
        if self.discovery_attempts == 0:
            return None
        return self.discovery_failures / self.discovery_attempts

    def avg_open_price_delay(self) -> Optional[float]:
        if self.open_price_delay_count == 0:
            return None
        return self.open_price_delay_sum / self.open_price_delay_count

    def adverse_fill_fraction(self) -> Optional[float]:
        if self.paper_bankroll <= 0:
            return None
        return self.adverse_fill_pnl / self.paper_bankroll


class KillConditionValidator:
    """
    Evaluates all kill conditions against a SessionStats snapshot.

    Usage
    -----
        validator = KillConditionValidator(kill_config)
        result = validator.check(stats)
        if result.recommended_action == KillAction.KILL:
            raise SessionKillError(result.verdict)
    """

    def __init__(self, kill_config: dict) -> None:
        self._cfg = kill_config.get("kill_conditions", {})

    def check(self, stats: SessionStats) -> KillCheckResult:
        result = KillCheckResult()

        for check_fn in self._all_checks():
            cr = check_fn(stats)
            if cr is None:
                continue
            result.detail_lines.append(
                f"  [{cr.action.value.upper():7s}] {cr.name}: "
                f"measured={cr.measured_value:.4f} threshold={cr.threshold:.4f} "
                f"triggered={cr.triggered} n={cr.sample_size}"
                if cr.measured_value is not None else
                f"  [SKIP   ] {cr.name}: insufficient sample (n={cr.sample_size} "
                f"< min={cr.min_sample_size})"
            )
            if cr.triggered:
                result.triggered_conditions.append(cr)

        if result.triggered_conditions:
            worst = max(result.triggered_conditions, key=lambda c: c.action.severity())
            result.recommended_action = worst.action
            triggered_names = ", ".join(c.name for c in result.triggered_conditions)
            result.verdict = f"{worst.action.value}: {triggered_names}"
        else:
            result.recommended_action = KillAction.CONTINUE
            result.verdict = "continue"

        logger.info(
            "[kill_check] action=%s verdict=%s triggered=%d",
            result.recommended_action.value,
            result.verdict,
            len(result.triggered_conditions),
        )
        return result

    # ------------------------------------------------------------------
    # Individual condition checks
    # Each returns ConditionResult or None (if condition is disabled).
    # ------------------------------------------------------------------

    def _check_raw_directional_accuracy(self, stats: SessionStats) -> Optional[ConditionResult]:
        cfg = self._cfg.get("raw_directional_accuracy", {})
        if not cfg.get("enabled", False):
            return None
        min_n = cfg.get("min_sample_size", 20)
        threshold = float(cfg["threshold"])
        val = stats.raw_directional_accuracy()
        n = stats.raw_direction_total
        triggered = val is not None and n >= min_n and val < threshold
        return ConditionResult(
            name="raw_directional_accuracy",
            triggered=triggered,
            action=KillAction(cfg.get("action", "warn")),
            threshold=threshold,
            measured_value=val,
            description=cfg.get("description", ""),
            sample_size=n,
            min_sample_size=min_n,
        )

    def _check_filtered_directional_accuracy(self, stats: SessionStats) -> Optional[ConditionResult]:
        cfg = self._cfg.get("filtered_directional_accuracy", {})
        if not cfg.get("enabled", False):
            return None
        min_n = cfg.get("min_sample_size", 15)
        threshold = float(cfg["threshold"])
        val = stats.filtered_directional_accuracy()
        n = stats.filtered_direction_total
        triggered = val is not None and n >= min_n and val < threshold
        return ConditionResult(
            name="filtered_directional_accuracy",
            triggered=triggered,
            action=KillAction(cfg.get("action", "kill")),
            threshold=threshold,
            measured_value=val,
            description=cfg.get("description", ""),
            sample_size=n,
            min_sample_size=min_n,
        )

    def _check_fill_conditioned_wr(self, stats: SessionStats) -> Optional[ConditionResult]:
        cfg = self._cfg.get("fill_conditioned_win_rate", {})
        if not cfg.get("enabled", False):
            return None
        min_n = cfg.get("min_sample_size", 10)
        threshold = float(cfg["threshold"])
        val = stats.fill_conditioned_wr()
        n = stats.fill_conditioned_total
        triggered = val is not None and n >= min_n and val < threshold
        return ConditionResult(
            name="fill_conditioned_win_rate",
            triggered=triggered,
            action=KillAction(cfg.get("action", "kill")),
            threshold=threshold,
            measured_value=val,
            description=cfg.get("description", ""),
            sample_size=n,
            min_sample_size=min_n,
        )

    def _check_fill_rate(self, stats: SessionStats) -> Optional[ConditionResult]:
        cfg = self._cfg.get("fill_rate", {})
        if not cfg.get("enabled", False):
            return None
        min_n = cfg.get("min_sample_size", 20)
        threshold = float(cfg["threshold"])
        val = stats.maker_fill_rate()
        n = stats.maker_candidate_count
        triggered = val is not None and n >= min_n and val < threshold
        return ConditionResult(
            name="fill_rate",
            triggered=triggered,
            action=KillAction(cfg.get("action", "warn")),
            threshold=threshold,
            measured_value=val,
            description=cfg.get("description", ""),
            sample_size=n,
            min_sample_size=min_n,
        )

    def _check_adverse_fill(self, stats: SessionStats) -> Optional[ConditionResult]:
        cfg = self._cfg.get("adverse_fill_underperformance", {})
        if not cfg.get("enabled", False):
            return None
        min_n = cfg.get("min_sample_size", 10)
        threshold = float(cfg["threshold"])
        val = stats.adverse_fill_fraction()
        n = stats.fill_conditioned_total
        triggered = val is not None and n >= min_n and val < threshold
        return ConditionResult(
            name="adverse_fill_underperformance",
            triggered=triggered,
            action=KillAction(cfg.get("action", "kill")),
            threshold=threshold,
            measured_value=val,
            description=cfg.get("description", ""),
            sample_size=n,
            min_sample_size=min_n,
        )

    def _check_avg_fill_price(self, stats: SessionStats) -> Optional[ConditionResult]:
        cfg = self._cfg.get("avg_fill_price_ceiling", {})
        if not cfg.get("enabled", False):
            return None
        min_n = cfg.get("min_sample_size", 10)
        threshold = float(cfg["threshold"])
        val = stats.avg_fill_price()
        n = stats.avg_fill_price_count
        triggered = val is not None and n >= min_n and val > threshold
        return ConditionResult(
            name="avg_fill_price_ceiling",
            triggered=triggered,
            action=KillAction(cfg.get("action", "tighten")),
            threshold=threshold,
            measured_value=val,
            description=cfg.get("description", ""),
            sample_size=n,
            min_sample_size=min_n,
        )

    def _check_basis_mismatch(self, stats: SessionStats) -> Optional[ConditionResult]:
        cfg = self._cfg.get("basis_mismatch_ceiling", {})
        if not cfg.get("enabled", False):
            return None
        min_n = cfg.get("min_sample_size", 10)
        threshold = float(cfg["threshold"])
        val = stats.basis_mismatch_frequency()
        n = stats.active_windows
        triggered = val is not None and n >= min_n and val > threshold
        return ConditionResult(
            name="basis_mismatch_ceiling",
            triggered=triggered,
            action=KillAction(cfg.get("action", "tighten")),
            threshold=threshold,
            measured_value=val,
            description=cfg.get("description", ""),
            sample_size=n,
            min_sample_size=min_n,
        )

    def _check_chainlink_gap(self, stats: SessionStats) -> Optional[ConditionResult]:
        cfg = self._cfg.get("chainlink_gap_frequency_ceiling", {})
        if not cfg.get("enabled", False):
            return None
        min_n = cfg.get("min_sample_size", 10)
        threshold = float(cfg["threshold"])
        val = stats.chainlink_gap_frequency()
        n = stats.active_windows
        triggered = val is not None and n >= min_n and val > threshold
        return ConditionResult(
            name="chainlink_gap_frequency_ceiling",
            triggered=triggered,
            action=KillAction(cfg.get("action", "kill")),
            threshold=threshold,
            measured_value=val,
            description=cfg.get("description", ""),
            sample_size=n,
            min_sample_size=min_n,
        )

    def _check_fast_gap(self, stats: SessionStats) -> Optional[ConditionResult]:
        cfg = self._cfg.get("fast_feed_gap_frequency_ceiling", {})
        if not cfg.get("enabled", False):
            return None
        min_n = cfg.get("min_sample_size", 10)
        threshold = float(cfg["threshold"])
        val = stats.fast_gap_frequency()
        n = stats.active_windows
        triggered = val is not None and n >= min_n and val > threshold
        return ConditionResult(
            name="fast_feed_gap_frequency_ceiling",
            triggered=triggered,
            action=KillAction(cfg.get("action", "kill")),
            threshold=threshold,
            measured_value=val,
            description=cfg.get("description", ""),
            sample_size=n,
            min_sample_size=min_n,
        )

    def _check_discovery_failure_rate(self, stats: SessionStats) -> Optional[ConditionResult]:
        cfg = self._cfg.get("discovery_failure_rate_ceiling", {})
        if not cfg.get("enabled", False):
            return None
        min_n = cfg.get("min_sample_size", 10)
        threshold = float(cfg["threshold"])
        val = stats.discovery_failure_rate()
        n = stats.discovery_attempts
        triggered = val is not None and n >= min_n and val > threshold
        return ConditionResult(
            name="discovery_failure_rate_ceiling",
            triggered=triggered,
            action=KillAction(cfg.get("action", "kill")),
            threshold=threshold,
            measured_value=val,
            description=cfg.get("description", ""),
            sample_size=n,
            min_sample_size=min_n,
        )

    def _check_open_price_delay(self, stats: SessionStats) -> Optional[ConditionResult]:
        cfg = self._cfg.get("open_price_capture_delay_ceiling", {})
        if not cfg.get("enabled", False):
            return None
        min_n = cfg.get("min_sample_size", 10)
        threshold = float(cfg["threshold"])
        val = stats.avg_open_price_delay()
        n = stats.open_price_delay_count
        triggered = val is not None and n >= min_n and val > threshold
        return ConditionResult(
            name="open_price_capture_delay_ceiling",
            triggered=triggered,
            action=KillAction(cfg.get("action", "tighten")),
            threshold=threshold,
            measured_value=val,
            description=cfg.get("description", ""),
            sample_size=n,
            min_sample_size=min_n,
        )

    def _check_consecutive_losses(self, stats: SessionStats) -> Optional[ConditionResult]:
        cfg = self._cfg.get("consecutive_loss_limit", {})
        if not cfg.get("enabled", False):
            return None
        threshold = float(cfg["threshold"])
        val = float(stats.consecutive_losses)
        triggered = val >= threshold
        return ConditionResult(
            name="consecutive_loss_limit",
            triggered=triggered,
            action=KillAction(cfg.get("action", "kill")),
            threshold=threshold,
            measured_value=val,
            description=cfg.get("description", ""),
            sample_size=stats.fill_conditioned_total,
            min_sample_size=1,
        )

    def _check_bankroll_floor(self, stats: SessionStats) -> Optional[ConditionResult]:
        cfg = self._cfg.get("paper_bankroll_floor", {})
        if not cfg.get("enabled", False):
            return None
        threshold = float(cfg["threshold"])
        val = stats.paper_bankroll
        triggered = val < threshold
        return ConditionResult(
            name="paper_bankroll_floor",
            triggered=triggered,
            action=KillAction(cfg.get("action", "kill")),
            threshold=threshold,
            measured_value=val,
            description=cfg.get("description", ""),
            sample_size=stats.fill_conditioned_total,
            min_sample_size=1,
        )

    def _all_checks(self) -> list[Callable[[SessionStats], Optional[ConditionResult]]]:
        return [
            self._check_raw_directional_accuracy,
            self._check_filtered_directional_accuracy,
            self._check_fill_conditioned_wr,
            self._check_fill_rate,
            self._check_adverse_fill,
            self._check_avg_fill_price,
            self._check_basis_mismatch,
            self._check_chainlink_gap,
            self._check_fast_gap,
            self._check_discovery_failure_rate,
            self._check_open_price_delay,
            self._check_consecutive_losses,
            self._check_bankroll_floor,
        ]
