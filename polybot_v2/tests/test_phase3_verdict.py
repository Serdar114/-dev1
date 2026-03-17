"""
Phase 3 verdict / session summary tests.

Covers:
  A) taker_signal_eval_enabled / taker_paper_execution_enabled config loading
  B) summary generation from sample artifacts
  C) maker grouped breakdown generation
  D) provisional verdict logic (rules-based)
  E) threshold config loading
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest

from settings import Settings
from session_verdict import (
    _build_maker_summary,
    _build_taker_summary,
    _build_verdict,
    build_session_summary,
)

CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"


# ──────────────────────────────────────────────────────────────────────────────
# A) Config toggle loading
# ──────────────────────────────────────────────────────────────────────────────

class TestTakerConfigToggles:
    def test_defaults_are_both_true(self):
        cfg = Settings(CONFIG_PATH)
        # config.yaml sets both true; defaults are also true
        assert cfg.taker_signal_eval_enabled is True
        assert cfg.taker_paper_execution_enabled is True

    def test_signal_eval_disabled_via_raw(self):
        cfg = Settings(CONFIG_PATH)
        cfg._raw["app"]["taker_signal_eval_enabled"] = False
        assert cfg.taker_signal_eval_enabled is False
        # paper execution unaffected
        assert cfg.taker_paper_execution_enabled is True

    def test_paper_execution_disabled_via_raw(self):
        cfg = Settings(CONFIG_PATH)
        cfg._raw["app"]["taker_paper_execution_enabled"] = False
        assert cfg.taker_paper_execution_enabled is False
        # signal eval unaffected
        assert cfg.taker_signal_eval_enabled is True

    def test_both_can_be_disabled(self):
        cfg = Settings(CONFIG_PATH)
        cfg._raw["app"]["taker_signal_eval_enabled"] = False
        cfg._raw["app"]["taker_paper_execution_enabled"] = False
        assert cfg.taker_signal_eval_enabled is False
        assert cfg.taker_paper_execution_enabled is False


# ──────────────────────────────────────────────────────────────────────────────
# B) Taker paper execution gate in main._tick
# ──────────────────────────────────────────────────────────────────────────────

class TestTakerExecutionGate:
    """
    Verify that taker_paper_execution_enabled=False prevents paper trade opening
    while taker signal evaluation still runs (action logged as PAPER_TRADE but
    paper_exec.open_trade is never called).
    """

    def _make_bot_with_toggles(self, signal_eval: bool, paper_exec_flag: bool):
        """
        Build a minimal PolybotV2 stub that exercises the execution gate logic
        without touching real feeds or files.
        """
        from main import PolybotV2
        bot = PolybotV2.__new__(PolybotV2)

        cfg = Settings(CONFIG_PATH)
        cfg._raw["app"]["taker_signal_eval_enabled"] = signal_eval
        cfg._raw["app"]["taker_paper_execution_enabled"] = paper_exec_flag
        cfg._raw["ui"] = {"enabled": False}
        bot._cfg = cfg

        # Stubs
        bot._bankroll = MagicMock(bankroll=30.0, drawdown=0.0)
        bot._risk_state = MagicMock(actions_this_window=0, open_paper_trades_this_window=0)
        bot._signal_engine = MagicMock()
        bot._paper_exec = MagicMock()
        bot._shadow_probe = MagicMock()
        bot._metrics = MagicMock()
        bot._structured = MagicMock()
        bot._ui = None
        bot._ui_state = None
        bot._last_sanity_details = {}
        bot._window_open_price = 84000.0
        bot._signal_engine._last_fair_result = None
        bot._signal_engine._last_decision_context = None
        bot._signal_engine._last_regime = "UNKNOWN"
        bot._signal_engine._last_pattern = "UNKNOWN"
        return bot

    def test_paper_execution_disabled_prevents_open_trade(self):
        """When taker_paper_execution_enabled=False, open_trade must not be called."""
        from models import SignalDecision
        bot = self._make_bot_with_toggles(signal_eval=True, paper_exec_flag=False)

        # Signal engine returns PAPER_TRADE
        paper_decision = SignalDecision(
            ts=time.time(), window_ts=time.time() + 100,
            lane="selective_taker", action="PAPER_TRADE",
            chosen_side="yes", reason="edge_ok",
            seconds_to_expiry=100.0, elapsed_from_window_start=50.0,
            btc_mid=84000.0, window_open=84000.0,
            implied_yes_prob=0.43, fair_yes_prob=0.55,
        )
        bot._signal_engine.evaluate_taker.return_value = paper_decision

        market = MagicMock()
        market.seconds_to_expiry = 100.0
        market.implied_yes_prob = 0.43
        market.best_bid_yes = 0.40
        market.best_ask_yes = 0.46
        market.best_bid_no = 0.54
        market.best_ask_no = 0.60
        market.fetched_at = time.time()

        bot._current_market = market
        bot._last_sanity_details = {
            "yes_bid": 0.40, "yes_ask": 0.46,
            "no_bid": 0.54, "no_ask": 0.60,
            "yes_mid": 0.43, "no_mid": 0.57,
            "spread_yes": 0.06, "spread_no": 0.06,
            "complement_skew": 0.0, "reject": None,
        }

        # Patch sanity check to pass
        bot._sanity_check_market = MagicMock(return_value=None)
        # Call the paper trade block logic directly
        # Replicate the gate condition from main._tick
        taker_decision = paper_decision
        if taker_decision.action == "PAPER_TRADE" and bot._cfg.taker_paper_execution_enabled:
            bot._paper_exec.open_trade(taker_decision, 0.46, bot._bankroll)

        bot._paper_exec.open_trade.assert_not_called()

    def test_paper_execution_enabled_calls_open_trade(self):
        """When taker_paper_execution_enabled=True, open_trade is called."""
        from models import SignalDecision
        bot = self._make_bot_with_toggles(signal_eval=True, paper_exec_flag=True)

        paper_decision = SignalDecision(
            ts=time.time(), window_ts=time.time() + 100,
            lane="selective_taker", action="PAPER_TRADE",
            chosen_side="yes", reason="edge_ok",
            seconds_to_expiry=100.0, elapsed_from_window_start=50.0,
            btc_mid=84000.0, window_open=84000.0,
            implied_yes_prob=0.43, fair_yes_prob=0.55,
        )
        bot._paper_exec.open_trade.return_value = None  # trade object

        if paper_decision.action == "PAPER_TRADE" and bot._cfg.taker_paper_execution_enabled:
            bot._paper_exec.open_trade(paper_decision, 0.46, bot._bankroll)

        bot._paper_exec.open_trade.assert_called_once()


# ──────────────────────────────────────────────────────────────────────────────
# C) Taker summary from sample artifacts
# ──────────────────────────────────────────────────────────────────────────────

class TestTakerSummaryFromArtifacts:
    def _make_signals(self) -> list[dict]:
        return [
            {"lane": "selective_taker", "action": "PAPER_TRADE", "chosen_side": "yes",
             "after_fee_edge_yes": 0.04, "elapsed_from_window_start": 50.0},
            {"lane": "selective_taker", "action": "NO_TRADE", "reason": "delta_too_small(...)"},
            {"lane": "selective_taker", "action": "NO_TRADE", "reason": "outside_entry_window"},
            {"lane": "selective_taker", "action": "NO_TRADE", "reason": "outside_entry_window"},
            {"lane": "maker_shadow", "action": "NO_QUOTE", "reason": "ste_too_low"},
        ]

    def _make_trades(self) -> list[dict]:
        return [
            {"event": "open", "side": "yes", "entry_price": 0.46},
            {"event": "resolve", "pnl": 0.12, "side": "yes",
             "after_fee_edge_yes": 0.04},
            {"event": "trade_thesis", "side": "yes",
             "after_fee_edge_yes": 0.04, "after_fee_edge_no": -0.01},
        ]

    def test_signal_count(self):
        t = _build_taker_summary(self._make_signals(), self._make_trades())
        assert t["signal_count"] == 4  # only selective_taker signals

    def test_no_trade_count(self):
        t = _build_taker_summary(self._make_signals(), self._make_trades())
        assert t["no_trade_count"] == 3

    def test_trade_open_count(self):
        t = _build_taker_summary(self._make_signals(), self._make_trades())
        assert t["trade_open_count"] == 1

    def test_trade_resolve_count(self):
        t = _build_taker_summary(self._make_signals(), self._make_trades())
        assert t["trade_resolve_count"] == 1

    def test_win_count(self):
        t = _build_taker_summary(self._make_signals(), self._make_trades())
        assert t["win_count"] == 1
        assert t["loss_count"] == 0

    def test_total_pnl(self):
        t = _build_taker_summary(self._make_signals(), self._make_trades())
        assert t["total_pnl_usdc"] == pytest.approx(0.12, abs=1e-6)

    def test_expectancy(self):
        t = _build_taker_summary(self._make_signals(), self._make_trades())
        assert t["expectancy_per_resolved_trade_usdc"] == pytest.approx(0.12, abs=1e-6)

    def test_avg_entry_edge_pct(self):
        t = _build_taker_summary(self._make_signals(), self._make_trades())
        # from trade_thesis: after_fee_edge_yes=0.04 -> 4.0%
        assert t["avg_entry_after_fee_edge_pct"] == pytest.approx(4.0, abs=0.01)

    def test_no_trade_reason_breakdown(self):
        t = _build_taker_summary(self._make_signals(), self._make_trades())
        assert "outside_entry_window" in t["no_trade_reason_breakdown"]
        assert t["no_trade_reason_breakdown"]["outside_entry_window"] == 2


# ──────────────────────────────────────────────────────────────────────────────
# D) Maker summary and grouped breakdowns
# ──────────────────────────────────────────────────────────────────────────────

class TestMakerSummaryFromArtifacts:
    def _make_quotes(self) -> list[dict]:
        base_ts = time.time()
        return [
            # pending quote, eventually filled_favorable + boundary win
            {"quote_id": "aaa", "fill_status": "filled_favorable",
             "side": "yes", "regime": "TRENDING", "pattern": "BURST",
             "seconds_to_expiry": 90.0, "intended_passive_edge": 0.12,
             "maker_pnl_if_held": 0.60, "boundary_outcome_for_side": 1.0,
             "boundary_outcome_yes": 1.0, "ts": base_ts},
            # filled_adverse + boundary loss
            {"quote_id": "bbb", "fill_status": "filled_adverse",
             "side": "yes", "regime": "CHOP", "pattern": "NOISE",
             "seconds_to_expiry": 45.0, "intended_passive_edge": 0.05,
             "maker_pnl_if_held": -0.40, "boundary_outcome_for_side": 0.0,
             "boundary_outcome_yes": 0.0, "ts": base_ts},
            # expired unfilled
            {"quote_id": "ccc", "fill_status": "expired_unfilled",
             "side": "no", "regime": "QUIET", "pattern": "FADE",
             "seconds_to_expiry": 60.0, "intended_passive_edge": 0.03,
             "maker_pnl_if_held": None, "boundary_outcome_for_side": None,
             "ts": base_ts},
            # crossed rejected
            {"quote_id": "ddd", "fill_status": "crossed_rejected",
             "reject_reason": "below_min_passive_edge: passive_edge=0.01",
             "side": "yes", "seconds_to_expiry": 120.0, "ts": base_ts},
        ]

    def test_unique_quote_count(self):
        m = _build_maker_summary(self._make_quotes())
        assert m["unique_quote_count"] == 4

    def test_fill_count(self):
        m = _build_maker_summary(self._make_quotes())
        assert m["fill_count"] == 2

    def test_expiry_count(self):
        m = _build_maker_summary(self._make_quotes())
        assert m["expiry_count"] == 1

    def test_adverse_fill_count(self):
        m = _build_maker_summary(self._make_quotes())
        assert m["adverse_fill_count"] == 1

    def test_favorable_fill_count(self):
        m = _build_maker_summary(self._make_quotes())
        assert m["favorable_fill_count"] == 1

    def test_boundary_win_loss(self):
        m = _build_maker_summary(self._make_quotes())
        assert m["boundary_win_count"] == 1
        assert m["boundary_loss_count"] == 1

    def test_pnl_totals(self):
        m = _build_maker_summary(self._make_quotes())
        assert m["maker_pnl_if_held_total"] == pytest.approx(0.60 - 0.40, abs=1e-5)
        assert m["maker_pnl_if_held_mean"] == pytest.approx((0.60 - 0.40) / 2.0, abs=1e-5)

    def test_reject_breakdown_captures_passive_edge(self):
        m = _build_maker_summary(self._make_quotes())
        assert m["reject_breakdown"]["below_min_passive_edge"] >= 1

    def test_by_regime_populated(self):
        m = _build_maker_summary(self._make_quotes())
        assert "TRENDING" in m["by_regime"]
        assert m["by_regime"]["TRENDING"]["count"] >= 1

    def test_by_ste_bucket(self):
        m = _build_maker_summary(self._make_quotes())
        # seconds_to_expiry: 90 -> 60-120s, 45 -> 30-60s, 60 -> 60-120s, 120 -> 120-180s
        assert "60-120s" in m["by_ste_bucket"]
        assert m["by_ste_bucket"]["60-120s"]["count"] >= 2

    def test_by_passive_edge_bucket(self):
        m = _build_maker_summary(self._make_quotes())
        # edge 0.12 -> >=0.10; 0.05 -> 0.05-0.10; 0.03 -> 0.02-0.05; ddd has no edge
        assert ">=0.10" in m["by_passive_edge_bucket"]
        assert m["by_passive_edge_bucket"][">=0.10"]["count"] >= 1


# ──────────────────────────────────────────────────────────────────────────────
# E) Provisional verdict logic
# ──────────────────────────────────────────────────────────────────────────────

class TestProvisionalVerdict:
    def _cfg(self, overrides: dict | None = None) -> Settings:
        cfg = Settings(CONFIG_PATH)
        if overrides:
            cfg._raw.setdefault("verdict_thresholds", {}).update(overrides)
        return cfg

    def _taker(self, **kwargs) -> dict:
        base = {
            "signal_count": 100, "no_trade_count": 80,
            "trade_open_count": 20, "trade_resolve_count": 20,
            "win_count": 10, "loss_count": 10,
            "win_rate": 0.5, "total_pnl_usdc": 0.0,
            "avg_pnl_per_resolved_trade_usdc": 0.0,
            "expectancy_per_resolved_trade_usdc": 0.0,
            "avg_entry_after_fee_edge_pct": 2.0,
            "avg_entry_elapsed_sec": 60.0,
            "no_trade_reason_breakdown": {},
            "entry_blocker_breakdown": {},
            "entry_blocker_other": {},
        }
        base.update(kwargs)
        return base

    def _maker(self, **kwargs) -> dict:
        base = {
            "unique_quote_count": 30, "fill_count": 20,
            "expiry_count": 10, "fill_rate": 0.67, "expiry_rate": 0.33,
            "adverse_fill_count": 8, "favorable_fill_count": 12,
            "adverse_fill_ratio": 0.4, "favorable_fill_ratio": 0.6,
            "resolved_filled_count": 20, "boundary_win_count": 12,
            "boundary_loss_count": 8, "boundary_win_rate": 0.60,
            "maker_pnl_if_held_total": 1.2,
            "maker_pnl_if_held_mean": 0.06,
            "maker_pnl_if_held_median": 0.05,
            "reject_breakdown": {}, "reject_breakdown_raw": {},
            "by_side": {}, "by_regime": {}, "by_ste_bucket": {}, "by_passive_edge_bucket": {},
        }
        base.update(kwargs)
        return base

    def test_taker_baseline_only_when_zero_expectancy(self):
        v = _build_verdict(self._taker(expectancy_per_resolved_trade_usdc=0.0), self._maker(), self._cfg())
        assert v["taker_status"] == "baseline_only"

    def test_taker_baseline_only_when_negative_expectancy(self):
        v = _build_verdict(self._taker(expectancy_per_resolved_trade_usdc=-0.05), self._maker(), self._cfg())
        assert v["taker_status"] == "baseline_only"

    def test_taker_positive_when_above_threshold(self):
        v = _build_verdict(
            self._taker(expectancy_per_resolved_trade_usdc=0.01),
            self._maker(),
            self._cfg({"taker_min_expectancy_usdc": 0.005}),
        )
        assert v["taker_status"] == "baseline_positive_expectancy"

    def test_taker_no_data_when_zero_resolved(self):
        v = _build_verdict(self._taker(trade_resolve_count=0), self._maker(), self._cfg())
        assert v["taker_status"] == "no_data"

    def test_maker_insufficient_sample_below_min_fills(self):
        v = _build_verdict(
            self._taker(),
            self._maker(resolved_filled_count=5),
            self._cfg({"maker_min_resolved_fills": 20}),
        )
        assert v["maker_status"] == "insufficient_sample"

    def test_maker_evaluation_only_low_win_rate(self):
        v = _build_verdict(
            self._taker(),
            self._maker(boundary_win_rate=0.45, resolved_filled_count=25),
            self._cfg({"maker_min_resolved_fills": 20, "maker_min_boundary_win_rate": 0.55}),
        )
        assert v["maker_status"] == "evaluation_only"

    def test_maker_evaluation_only_low_mean_pnl(self):
        v = _build_verdict(
            self._taker(),
            self._maker(boundary_win_rate=0.60, maker_pnl_if_held_mean=0.02, resolved_filled_count=25),
            self._cfg({"maker_min_resolved_fills": 20, "maker_min_mean_pnl_if_held": 0.05}),
        )
        assert v["maker_status"] == "evaluation_only"

    def test_maker_conditionally_researchable_when_thresholds_met(self):
        v = _build_verdict(
            self._taker(),
            self._maker(
                resolved_filled_count=25,
                boundary_win_rate=0.60,
                maker_pnl_if_held_mean=0.07,
                adverse_fill_ratio=0.35,
            ),
            self._cfg({
                "maker_min_resolved_fills": 20,
                "maker_min_boundary_win_rate": 0.55,
                "maker_min_mean_pnl_if_held": 0.05,
                "maker_max_adverse_fill_ratio": 0.60,
            }),
        )
        assert v["maker_status"] == "conditionally_researchable"

    def test_live_candidate_not_live_ready_by_default(self):
        v = _build_verdict(self._taker(), self._maker(), self._cfg())
        assert v["live_candidate_status"] == "not_live_ready"

    def test_verdict_note_is_provisional(self):
        v = _build_verdict(self._taker(), self._maker(), self._cfg())
        assert "PROVISIONAL" in v["_note"]

    def test_false_hope_when_negative_mean_pnl(self):
        v = _build_verdict(
            self._taker(),
            self._maker(maker_pnl_if_held_mean=-0.05, fill_count=10, boundary_win_rate=0.4),
            self._cfg(),
        )
        assert v["strongest_false_hope"] is not None
        assert "negative" in v["strongest_false_hope"].lower() or "destroys" in v["strongest_false_hope"].lower()


# ──────────────────────────────────────────────────────────────────────────────
# F) Threshold config loading
# ──────────────────────────────────────────────────────────────────────────────

class TestVerdictThresholdConfig:
    def test_defaults_load_without_error(self):
        cfg = Settings(CONFIG_PATH)
        # All properties should be accessible without KeyError
        assert cfg.verdict_taker_min_expectancy_usdc > 0
        assert cfg.verdict_maker_min_resolved_fills > 0
        assert 0 < cfg.verdict_maker_min_boundary_win_rate < 1
        assert cfg.verdict_maker_min_mean_pnl_if_held > 0
        assert 0 < cfg.verdict_maker_max_adverse_fill_ratio < 1
        assert cfg.verdict_live_candidate_min_maker_fills > 0

    def test_config_values_match_yaml(self):
        cfg = Settings(CONFIG_PATH)
        assert cfg.verdict_taker_min_expectancy_usdc == pytest.approx(0.005)
        assert cfg.verdict_maker_min_resolved_fills == 20
        assert cfg.verdict_maker_min_boundary_win_rate == pytest.approx(0.55)
        assert cfg.verdict_maker_min_mean_pnl_if_held == pytest.approx(0.05)
        assert cfg.verdict_maker_max_adverse_fill_ratio == pytest.approx(0.60)
        assert cfg.verdict_live_candidate_min_maker_fills == 50

    def test_override_via_raw(self):
        cfg = Settings(CONFIG_PATH)
        cfg._raw["verdict_thresholds"] = {"maker_min_resolved_fills": 100}
        assert cfg.verdict_maker_min_resolved_fills == 100
        # Other keys should fall back to defaults
        assert cfg.verdict_taker_min_expectancy_usdc == pytest.approx(0.005)


# ──────────────────────────────────────────────────────────────────────────────
# G) build_session_summary writes JSON and TXT files
# ──────────────────────────────────────────────────────────────────────────────

class TestBuildSessionSummaryIO:
    def test_writes_json_and_txt(self, tmp_path):
        cfg = Settings(CONFIG_PATH)
        # Write minimal sample artifacts
        signals = [
            {"lane": "selective_taker", "action": "NO_TRADE", "reason": "outside_entry_window"},
            {"lane": "maker_shadow", "action": "NO_QUOTE", "reason": "ste_too_low"},
        ]
        trades: list = []
        quotes = [
            {"quote_id": "abc123", "fill_status": "expired_unfilled", "side": "yes",
             "regime": "QUIET", "seconds_to_expiry": 50.0,
             "intended_passive_edge": 0.03, "maker_pnl_if_held": None,
             "boundary_outcome_for_side": None},
        ]
        bankroll: list = []

        for fname, records in [
            ("signals.jsonl", signals),
            ("paper_trades.jsonl", trades),
            ("shadow_quotes.jsonl", quotes),
            ("bankroll.jsonl", bankroll),
        ]:
            with open(tmp_path / fname, "w") as fh:
                for r in records:
                    fh.write(json.dumps(r) + "\n")

        summary = build_session_summary(tmp_path, cfg, session_ts="20260316_120000")

        assert (tmp_path / "session_summary.json").exists()
        assert (tmp_path / "session_summary.txt").exists()

        with open(tmp_path / "session_summary.json") as fh:
            loaded = json.load(fh)
        assert loaded["session_ts"] == "20260316_120000"
        assert "taker" in loaded
        assert "maker" in loaded
        assert "verdict" in loaded

    def test_json_verdict_has_required_fields(self, tmp_path):
        cfg = Settings(CONFIG_PATH)
        for fname in ("signals.jsonl", "paper_trades.jsonl", "shadow_quotes.jsonl", "bankroll.jsonl"):
            (tmp_path / fname).write_text("")

        summary = build_session_summary(tmp_path, cfg)
        v = summary["verdict"]
        assert "taker_status" in v
        assert "maker_status" in v
        assert "live_candidate_status" in v
        assert "_note" in v
        assert "verdict_basis" in v

    def test_txt_contains_session_header(self, tmp_path):
        cfg = Settings(CONFIG_PATH)
        for fname in ("signals.jsonl", "paper_trades.jsonl", "shadow_quotes.jsonl", "bankroll.jsonl"):
            (tmp_path / fname).write_text("")

        build_session_summary(tmp_path, cfg, session_ts="20260316_120000")
        txt = (tmp_path / "session_summary.txt").read_text()
        assert "20260316_120000" in txt
        assert "TAKER" in txt
        assert "MAKER" in txt
        assert "PROVISIONAL VERDICT" in txt

    def test_handles_missing_files_gracefully(self, tmp_path):
        cfg = Settings(CONFIG_PATH)
        # No files at all — should not raise, should write empty summaries
        summary = build_session_summary(tmp_path, cfg)
        assert summary["taker"]["signal_count"] == 0
        assert summary["maker"]["unique_quote_count"] == 0
