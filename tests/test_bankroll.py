"""
tests/test_bankroll.py

Covers fix #1: initial bankroll is 30.0 from config; no hardcoded 1000.0.
"""
import pytest
from risk.sizing import PositionSizer
from logger.summary import SummaryReporter, WindowLog


class TestPositionSizerBankroll:
    def test_initial_bankroll_from_config(self, base_config):
        sizer = PositionSizer(base_config["sizing"]["initial_bankroll"])
        assert sizer.bankroll == 30.0

    def test_initial_bankroll_is_not_hardcoded_1000(self, base_config):
        sizer = PositionSizer(base_config["sizing"]["initial_bankroll"])
        assert sizer.bankroll != 1000.0

    def test_bankroll_decreases_on_loss(self, base_config):
        sizer = PositionSizer(30.0)
        sizer.record_trade(-4.0)
        assert sizer.bankroll == pytest.approx(26.0)

    def test_bankroll_increases_on_win(self, base_config):
        sizer = PositionSizer(30.0)
        sizer.record_trade(5.0)
        assert sizer.bankroll == pytest.approx(35.0)

    def test_bankroll_fraction_uses_current_bankroll(self):
        sizer = PositionSizer(30.0)
        result = sizer.size(0.87)
        # cost = 0.87 * 5 = 4.35; fraction = 4.35 / 30.0
        expected_fraction = (0.87 * 5) / 30.0
        assert result.bankroll_fraction == pytest.approx(expected_fraction)

    def test_bankroll_fraction_reflects_updated_bankroll(self):
        sizer = PositionSizer(30.0)
        sizer.record_trade(-10.0)   # bankroll now 20.0
        result = sizer.size(0.87)
        expected_fraction = (0.87 * 5) / 20.0
        assert result.bankroll_fraction == pytest.approx(expected_fraction)


class TestSummaryReporterBankroll:
    def test_initial_bankroll_read_from_config(self, base_config):
        reporter = SummaryReporter(base_config, "0c")
        assert reporter._initial_bankroll == 30.0

    def test_compute_bankroll_starts_at_30(self, base_config):
        reporter = SummaryReporter(base_config, "0c")
        stats = reporter.build_session_stats()
        assert stats.paper_bankroll == pytest.approx(30.0)

    def test_compute_bankroll_not_hardcoded_1000(self, base_config):
        reporter = SummaryReporter(base_config, "0c")
        stats = reporter.build_session_stats()
        assert stats.paper_bankroll != 1000.0

    def test_compute_bankroll_reflects_pnl(self, base_config):
        reporter = SummaryReporter(base_config, "0c")
        wl = WindowLog(window_open_ts=1, slug="s", phase="0c")
        wl.maker_filled = True
        wl.maker_net_pnl = -4.35   # loss
        reporter.record_window(wl)
        stats = reporter.build_session_stats()
        assert stats.paper_bankroll == pytest.approx(30.0 - 4.35)

    def test_bankroll_floor_kill_condition_uses_30_baseline(self):
        import yaml, os
        cfg_path = os.path.join(os.path.dirname(__file__), "..", "config", "kill_conditions.yaml")
        with open(cfg_path) as f:
            kc = yaml.safe_load(f)
        threshold = kc["kill_conditions"]["paper_bankroll_floor"]["threshold"]
        # Should be ~70% of 30.0 = 21.0, NOT 700.0
        assert threshold == pytest.approx(21.0)
        assert threshold != 700.0
