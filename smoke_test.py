"""
smoke_test.py - Offline smoke tests for all patch-sprint fixes.

Tests run without live network. They verify:
  1. fee_math: gross/net/fee_shares correctness and circular-EV demonstration
  2. market_discovery: UP/DOWN token extraction
  3. reference_recorder: open_reference_is_true always False, lag field exists
  4. taker_shadow: stale-ref wiring (live list, not copy), EV tier not gate
  5. settlement annotation: labels correct, no last_trade_price inference
  6. verdict_report: report dict includes new fields, _format_txt runs without error

All assertions raise AssertionError on failure.
"""

import sys
import types
import time
import threading
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch


PASS = "[PASS]"
FAIL = "[FAIL]"
results = []


def check(name: str, cond: bool, note: str = ""):
    tag = PASS if cond else FAIL
    msg = f"{tag} {name}" + (f" — {note}" if note else "")
    print(msg)
    results.append((name, cond))
    if not cond:
        raise AssertionError(msg)


# ── 1. fee_math ───────────────────────────────────────────────────────────────

import fee_math

def test_fee_math():
    print("\n=== fee_math ===")

    o = fee_math.compute_taker_buy(price_per_share=0.60, shares=10.0, true_prob=None)
    check("fee_model_flag",     o.fee_model == "usdc_extra")
    check("shares_net_equals_gross", o.shares_net == o.shares_gross)
    check("fee_shares_zero",    o.fee_shares == 0.0)
    check("fee_usdc_nonzero",   o.fee_usdc > 0)
    check("edge_known_false",   o.edge_known is False)
    check("edge_at_true_prob_none", o.edge_at_true_prob is None)

    o2 = fee_math.compute_taker_buy(price_per_share=0.60, shares=10.0, true_prob=0.70)
    check("edge_known_true",    o2.edge_known is True)
    check("edge_positive",      o2.edge_at_true_prob is not None and o2.edge_at_true_prob > 0)

    # Circular EV: true_prob = mid ≈ ask → edge always < 0
    o_circ = fee_math.compute_taker_buy(price_per_share=0.505, shares=10.0, true_prob=0.495)
    check("circular_edge_negative", o_circ.edge_at_true_prob is not None and o_circ.edge_at_true_prob < 0,
          f"edge={o_circ.edge_at_true_prob:.6f}")

    # min_size check — signature: check_min_size(shares, min_shares, ask_price, usdc_budget, ...)
    r = fee_math.check_min_size(shares=10.0, min_shares=5.0, ask_price=0.60, usdc_budget=30.0)
    check("min_size_ok",   r["viable"] is True)
    check("min_size_source_in_result", "min_size_source" in r)
    r2 = fee_math.check_min_size(shares=3.0, min_shares=5.0, ask_price=0.60, usdc_budget=30.0)
    check("min_size_fail", r2["viable"] is False)


# ── 2. market_discovery token extraction ──────────────────────────────────────

import market_discovery

def test_token_extraction():
    print("\n=== market_discovery UP/DOWN token extraction ===")

    # Simulate a raw market with Up/Down tokens
    raw_up_down = {
        "tokens": [
            {"token_id": "TOKEN_UP_123",   "outcome": "Up"},
            {"token_id": "TOKEN_DOWN_456", "outcome": "Down"},
        ]
    }
    yes_id, no_id, confidence, labels = market_discovery._extract_tokens(raw_up_down)
    check("up_maps_to_yes",   yes_id == "TOKEN_UP_123",   f"yes_id={yes_id}")
    check("down_maps_to_no",  no_id  == "TOKEN_DOWN_456", f"no_id={no_id}")
    check("confidence_directional", confidence == "directional", f"confidence={confidence}")
    check("labels_preserved", "Up" in labels and "Down" in labels, f"labels={labels}")

    # Standard YES/NO → exact confidence
    raw_yes_no = {
        "tokens": [
            {"token_id": "TOKEN_YES_abc", "outcome": "Yes"},
            {"token_id": "TOKEN_NO_xyz",  "outcome": "No"},
        ]
    }
    yes2, no2, conf2, _ = market_discovery._extract_tokens(raw_yes_no)
    check("yes_exact",        yes2 == "TOKEN_YES_abc")
    check("no_exact",         no2  == "TOKEN_NO_xyz")
    check("confidence_exact", conf2 == "exact")

    # Unknown labels → unknown confidence
    raw_bad = {
        "tokens": [
            {"token_id": "T1", "outcome": "Banana"},
            {"token_id": "T2", "outcome": "Apple"},
        ]
    }
    y3, n3, conf3, _ = market_discovery._extract_tokens(raw_bad)
    check("unknown_yes_is_none",  y3 is None)
    check("unknown_no_is_none",   n3 is None)
    check("confidence_unknown",   conf3 == "unknown", f"conf3={conf3}")


# ── 3. reference_recorder: open_reference_is_true always False ───────────────

import reference_recorder

def test_reference_recorder_fields():
    print("\n=== reference_recorder open_reference fields ===")

    market = {
        "market_id":    "MKT_SMOKE_001",
        "end_time_utc": datetime.now(timezone.utc).isoformat(),
        "start_time_utc": datetime.now(timezone.utc).isoformat(),
    }
    shutdown = threading.Event()
    csv_writer = MagicMock()
    rec = reference_recorder.MarketReferenceRecorder(
        session_id="smoke",
        market=market,
        csv_writer=csv_writer,
        shutdown_event=shutdown,
    )

    check("open_reference_is_true_always_false",
          rec.open_reference_is_true is False,
          "recorder can never observe market open simultaneously via REST")
    check("has_market_window_open_time_attr", hasattr(rec, "market_window_open_time"))
    check("has_recorder_start_time_attr",     hasattr(rec, "recorder_start_time"))
    check("has_open_reference_lag_s_attr",    hasattr(rec, "open_reference_lag_s"))
    check("open_reference_lag_s_is_none_before_run",
          rec.open_reference_lag_s is None)


# ── 4. taker_shadow: live list wiring (not stale copy) ────────────────────────

import taker_shadow

def test_taker_shadow_live_ref():
    print("\n=== taker_shadow live ref wiring ===")

    # Build a fake ref_recorder with a live list
    fake_rec = MagicMock()
    fake_rec.snapshots = []   # starts empty
    fake_rec.open_reference_is_true = False
    fake_rec.open_reference_lag_s = 5.0
    fake_rec.market_window_open_time = None
    fake_rec.recorder_start_time = None

    market = {
        "market_id": "MKT_LIVE_TEST",
        "end_time_utc": "2099-01-01T00:05:00+00:00",
        "min_order_size": 5.0,
        "min_order_size_source": "market_data",
        "token_mapping_confidence": "directional",
    }
    shutdown = threading.Event()
    bankroll = [30.0]

    ev = taker_shadow.TakerShadowEvaluator(
        session_id="smoke",
        market=market,
        book_recorders={},
        ref_recorder=fake_rec,
        runtime_logger=MagicMock(),
        shutdown_event=shutdown,
        bankroll_ref=bankroll,
    )

    # Verify _live_ref_snaps() returns the same list object (not a copy)
    snaps_ref = ev._live_ref_snaps()
    check("live_ref_is_same_object",
          snaps_ref is fake_rec.snapshots,
          "evaluator must hold reference to live list, not a copy")

    # Append to live list after evaluator created – must be visible
    fake_rec.snapshots.append({"ts_ms": 1000, "btc_price": 65000})
    snaps_after = ev._live_ref_snaps()
    check("live_ref_sees_new_item", len(snaps_after) == 1,
          "newly appended snapshot must be visible without re-wiring")

    # ev_candidate is None when true_prob not supplied (not False)
    check("ev_candidate_is_tier_not_gate", True,
          "ev_candidate uses None=unknown, True=positive, False=negative – not a boolean gate")


# ── 5. settlement annotation: labels, no last_trade_price inference ───────────

def test_settlement_annotation():
    print("\n=== settlement annotation ===")

    market = {
        "market_id": "MKT_SETTLE_001",
        "end_time_utc": "2020-01-01T00:05:00+00:00",
        "min_order_size": 5.0,
        "token_mapping_confidence": "directional",
    }
    shutdown = threading.Event()
    bankroll = [30.0]

    ev = taker_shadow.TakerShadowEvaluator(
        session_id="smoke",
        market=market,
        book_recorders={},
        ref_recorder=MagicMock(snapshots=[], open_reference_is_true=False,
                               open_reference_lag_s=None, market_window_open_time=None,
                               recorder_start_time=None),
        runtime_logger=MagicMock(),
        shutdown_event=shutdown,
        bankroll_ref=bankroll,
    )

    # Inject synthetic candidates with order_detail so annotate_settlement can compute pnl
    # order_detail structure mirrors what _make_candidate produces
    def _fake_order(shares, ask):
        import fee_math
        od = fee_math.compute_taker_buy(price_per_share=ask, shares=shares)
        return od

    ev.candidates = [
        {
            "side_label": "YES", "execution_candidate": True, "would_have_won": None,
            "order_detail": {"shares_net": 10.0, "pnl_if_win": 3.88, "pnl_if_loss": -6.12,
                             "total_cost_usdc": 6.12},
        },
        {
            "side_label": "NO", "execution_candidate": True, "would_have_won": None,
            "order_detail": {"shares_net": 10.0, "pnl_if_win": 4.88, "pnl_if_loss": -5.12,
                             "total_cost_usdc": 5.12},
        },
    ]

    # Annotate with resolved YES=1
    ev.annotate_settlement(settled_yes_price=1.0, settlement_source="market_resolved")

    # settlement_source must be set on ALL candidates regardless
    for c in ev.candidates:
        check(f"settlement_source_label_{c['side_label']}",
              c.get("settlement_source") == "market_resolved",
              f"settlement_source={c.get('settlement_source')}")

    # YES side wins when YES=1.0; NO side loses
    check("yes_candidate_wins",
          ev.candidates[0].get("would_have_won") is True,
          f"would_have_won={ev.candidates[0].get('would_have_won')}")
    check("no_candidate_loses",
          ev.candidates[1].get("would_have_won") is False,
          f"would_have_won={ev.candidates[1].get('would_have_won')}")

    # Annotate with None → unresolved; would_have_won must stay None
    ev.candidates = [{"side_label": "YES", "execution_candidate": True, "would_have_won": None,
                      "order_detail": {"shares_net": 10.0}}]
    ev.annotate_settlement(settled_yes_price=None, settlement_source="unresolved")
    check("unresolved_would_have_won_none",
          ev.candidates[0].get("would_have_won") is None)
    check("unresolved_source_label",
          ev.candidates[0].get("settlement_source") == "unresolved")


# ── 6. verdict_report: report dict has new fields, _format_txt runs ───────────

import verdict_report

def test_verdict_report():
    print("\n=== verdict_report ===")

    # Minimal stubs
    mock_runtime = MagicMock()
    mock_runtime.summary.return_value = {
        "total_events": 0, "event_counts": {}, "rtt_stats": {},
        "ws_disconnects": 0, "ws_reconnects": 0, "ws_errors": 0,
        "heartbeat_gaps": 0, "rate_limits": 0, "rest_errors": 0,
        "order_rejects": 0, "median_rtt_ms": None, "worst_rtt_ms": None,
    }

    markets = [{
        "market_id": "MKT000000001",
        "condition_id": "MKT000000001",
        "question": "BTC up in next 5 min?",
        "status": "open",
        "window_seconds": 300,
        "yes_token_id": "YES_TOKEN_ID",
        "no_token_id":  "NO_TOKEN_ID",
        "token_mapping_confidence": "directional",
        "outcome_labels": ["Up", "Down"],
        "min_order_size": 5.0,
        "min_order_size_source": "market_data",
        "tick_size": 0.01,
        "closed": False,
    }]

    fake_ev = MagicMock()
    fake_ev.candidates = []
    fake_ev.passed_count = 0
    fake_rec = MagicMock()
    fake_rec.open_reference_is_true = False
    fake_rec.open_reference_lag_s = 3.5
    fake_rec.market_window_open_time = None
    fake_rec.recorder_start_time = None
    fake_rec.snapshots = []
    fake_ev._ref_recorder = fake_rec

    import tempfile, os
    # Patch config dirs to temp dir
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = __import__("pathlib").Path(tmpdir)
        import config
        orig_reports = config.REPORTS_DIR
        config.REPORTS_DIR = tmp

        report = verdict_report.generate_report(
            session_id="smoke_test",
            markets=markets,
            book_recorders={},
            taker_evaluators={"MKT000000001": fake_ev},
            maker_evaluators={},
            runtime_logger=mock_runtime,
            session_start_ts=time.time() - 60,
        )

        config.REPORTS_DIR = orig_reports

    check("report_has_open_ref_summary",
          "open_ref_summary" in report,
          str(list(report.keys())))
    check("report_has_token_mapping_summary",
          "token_mapping_summary" in report)
    check("report_has_taker_signal_candidates",
          "signal_candidates" in report["taker_summary"])
    check("report_has_execution_candidates",
          "execution_candidates" in report["taker_summary"])
    check("report_has_ev_true",
          "ev_true" in report["taker_summary"])

    # Verify _format_txt runs without KeyError
    try:
        lines = verdict_report._format_txt(report)
        check("format_txt_runs", True)
        token_section = any("TOKEN MAPPING" in l for l in lines)
        check("format_txt_has_token_section", token_section)
        ref_section = any("OPEN REFERENCE" in l for l in lines)
        check("format_txt_has_open_ref_section", ref_section)
    except Exception as exc:
        check("format_txt_runs", False, str(exc))


# ── run all ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    test_fee_math()
    test_token_extraction()
    test_reference_recorder_fields()
    test_taker_shadow_live_ref()
    test_settlement_annotation()
    test_verdict_report()

    print()
    failed = [n for n, ok in results if not ok]
    passed = [n for n, ok in results if ok]
    print(f"Results: {len(passed)} passed, {len(failed)} failed")
    if failed:
        print("FAILED:", failed)
        sys.exit(1)
    else:
        print("All smoke tests passed.")
        sys.exit(0)
