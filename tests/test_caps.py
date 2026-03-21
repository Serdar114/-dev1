"""
tests/test_caps.py

Covers fix #7: daily caps enforced in code.
- max_candidates_per_day = 50
- max_entries_per_day = 5
- one_position_at_a_time = True
"""
import pytest
from risk.caps import DailyCaps


@pytest.fixture
def caps(base_config):
    return DailyCaps(base_config["daily_caps"])


@pytest.fixture
def tight_caps():
    """Caps with very low limits for easy testing."""
    return DailyCaps({
        "max_candidates_per_day": 3,
        "max_entries_per_day": 2,
        "one_position_at_a_time": True,
    })


class TestCandidateCap:
    def test_can_observe_initially(self, caps):
        ok, reason = caps.can_observe_candidate()
        assert ok is True

    def test_blocked_after_max_candidates(self, tight_caps):
        for _ in range(3):
            tight_caps.record_candidate()
        ok, reason = tight_caps.can_observe_candidate()
        assert ok is False
        assert "daily_candidate_cap_reached" in reason

    def test_not_blocked_at_max_minus_one(self, tight_caps):
        for _ in range(2):
            tight_caps.record_candidate()
        ok, _ = tight_caps.can_observe_candidate()
        assert ok is True

    def test_candidate_count_increments(self, caps):
        caps.record_candidate()
        caps.record_candidate()
        assert caps.snapshot().candidates_today == 2

    def test_fifty_candidate_default_limit(self, caps):
        for _ in range(50):
            caps.record_candidate()
        ok, reason = caps.can_observe_candidate()
        assert ok is False
        assert "50/50" in reason


class TestEntryCap:
    def test_can_enter_initially(self, caps):
        ok, reason = caps.can_enter()
        assert ok is True

    def test_blocked_after_max_entries(self, tight_caps):
        # Record 2 entries (and exits to allow next)
        tight_caps.record_entry()
        tight_caps.record_exit()
        tight_caps.record_entry()
        tight_caps.record_exit()
        ok, reason = tight_caps.can_enter()
        assert ok is False
        assert "daily_entry_cap_reached" in reason

    def test_five_entry_default_limit(self, caps):
        for _ in range(5):
            caps.record_entry()
            caps.record_exit()
        ok, reason = caps.can_enter()
        assert ok is False
        assert "5/5" in reason

    def test_entry_count_increments(self, caps):
        caps.record_entry()
        caps.record_exit()
        caps.record_entry()
        caps.record_exit()
        assert caps.snapshot().entries_today == 2


class TestOnePositionAtATime:
    def test_blocked_when_position_open(self, caps):
        caps.record_entry()   # open position
        ok, reason = caps.can_enter()
        assert ok is False
        assert "one_position_at_a_time" in reason

    def test_allowed_after_exit(self, caps):
        caps.record_entry()
        caps.record_exit()
        ok, reason = caps.can_enter()
        assert ok is True

    def test_position_open_flag_set(self, caps):
        assert caps.snapshot().position_open is False
        caps.record_entry()
        assert caps.snapshot().position_open is True

    def test_position_open_flag_cleared_on_exit(self, caps):
        caps.record_entry()
        caps.record_exit()
        assert caps.snapshot().position_open is False

    def test_disabled_one_at_a_time_allows_overlap(self):
        caps = DailyCaps({
            "max_candidates_per_day": 50,
            "max_entries_per_day": 5,
            "one_position_at_a_time": False,
        })
        caps.record_entry()   # position open
        ok, reason = caps.can_enter()
        # one_at_a_time disabled — entry cap not yet hit, so should be ok
        assert ok is True


class TestDayRollover:
    def test_reset_on_new_day(self, caps):
        import datetime
        caps.record_candidate()
        caps.record_candidate()
        caps.record_entry()
        caps._today = datetime.date(2020, 1, 1)   # force old date
        caps.reset_if_new_day()
        assert caps.snapshot().candidates_today == 0
        assert caps.snapshot().entries_today == 0

    def test_position_open_preserved_across_reset(self, caps):
        """An open position at midnight must carry over to the next day."""
        import datetime
        caps.record_entry()
        caps._today = datetime.date(2020, 1, 1)
        caps.reset_if_new_day()
        assert caps.snapshot().position_open is True


class TestCapsSnapshot:
    def test_snapshot_fields(self, base_config):
        caps = DailyCaps(base_config["daily_caps"])
        s = caps.snapshot()
        assert s.max_candidates == 50
        assert s.max_entries == 5
        assert s.one_at_a_time is True
        assert s.candidates_today == 0
        assert s.entries_today == 0
        assert s.position_open is False
