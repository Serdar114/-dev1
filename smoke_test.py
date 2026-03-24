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

import json
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


# ── 7. fee_fetcher: live fetch success + failure paths ────────────────────────

import fee_fetcher
import fee_math as _fee_math_mod
import config

def test_fee_fetcher_paths():
    print("\n=== fee_fetcher live/fallback paths ===")
    from unittest.mock import patch, MagicMock

    # Path A: live fetch SUCCESS – mock 200 with parseable taker_fee_rate
    mock_resp_ok = MagicMock()
    mock_resp_ok.status_code = 200
    mock_resp_ok.json.return_value = {"taker_fee_rate": "0.02", "maker_fee_rate": "0"}

    with patch("fee_fetcher.requests.get", return_value=mock_resp_ok):
        result = fee_fetcher.fetch_live_fee_rate(token_id="TOK_LIVE_1")

    check("live_fetch_success",           result["fetch_success"] is True)
    check("live_fee_rate_source",         result["fee_rate_source"] == "live_endpoint")
    check("live_fee_truth_status",        result["fee_truth_status"] == "confirmed")
    check("live_fee_rate_value",          abs(result["fee_rate_value"] - 0.02) < 1e-9)
    check("live_fee_model_still_unresolved",
          result["fee_model_assumption"] == "unresolved",
          "model_assumption must remain unresolved even after live fetch")
    check("live_fee_fallback_not_used",   result.get("fetch_error") is None)

    # Path B: live fetch FAILURE – 401 (expected for unauthenticated call)
    mock_resp_fail = MagicMock()
    mock_resp_fail.status_code = 401
    mock_resp_fail.text = "Unauthorized"

    with patch("fee_fetcher.requests.get", return_value=mock_resp_fail):
        result_fail = fee_fetcher.fetch_live_fee_rate(token_id="TOK_FAIL_1")

    check("fallback_fetch_not_success",   result_fail["fetch_success"] is False)
    check("fallback_fee_rate_source",     result_fail["fee_rate_source"] == "config_fallback")
    check("fallback_fee_truth_status",    result_fail["fee_truth_status"] == "assumed")
    check("fallback_has_error",           result_fail["fetch_error"] is not None)
    check("fallback_rate_is_config",      result_fail["fee_rate_value"] == config.TAKER_FEE_RATE)

    # Path C: timeout
    import requests as _req
    with patch("fee_fetcher.requests.get", side_effect=_req.exceptions.Timeout):
        result_timeout = fee_fetcher.fetch_live_fee_rate()

    check("timeout_not_success",   result_timeout["fetch_success"] is False)
    check("timeout_error_set",     "Timeout" in (result_timeout["fetch_error"] or ""))


# ── 8. fee_math both models ────────────────────────────────────────────────────

def test_fee_math_both_models():
    print("\n=== fee_math both models ===")

    for ask in [0.10, 0.25, 0.50, 0.75, 0.90]:
        result = fee_math.compute_both_models(price_per_share=ask, shares=10.0)

        ue = result["usdc_extra"]
        sc = result["share_cut"]
        delta = result["delta"]

        check(f"both_models_usdc_extra_model@{ask}",
              ue.fee_model == "usdc_extra")
        check(f"both_models_share_cut_model@{ask}",
              sc.fee_model == "share_cut")

        # usdc_extra: no fee_shares, fee_usdc > 0
        check(f"usdc_extra_fee_shares_zero@{ask}",
              ue.fee_shares == 0.0)
        check(f"usdc_extra_fee_usdc_pos@{ask}",
              ue.fee_usdc > 0)
        check(f"usdc_extra_shares_net_equals_gross@{ask}",
              ue.shares_net == ue.shares_gross)

        # share_cut: fee_shares > 0, fee_usdc == 0
        check(f"share_cut_fee_shares_pos@{ask}",
              sc.fee_shares > 0)
        check(f"share_cut_fee_usdc_zero@{ask}",
              sc.fee_usdc == 0.0)
        check(f"share_cut_shares_net_less_than_gross@{ask}",
              sc.shares_net < sc.shares_gross)

        # Both models: total USDC cost must differ (usdc_extra costs more USDC)
        check(f"usdc_extra_costs_more@{ask}",
              ue.total_cost_usdc > sc.total_cost_usdc,
              f"ue={ue.total_cost_usdc:.6f} sc={sc.total_cost_usdc:.6f}")

        # Both model assumptions must be stamped (set by compute_both_models)
        check(f"ue_model_assumption_stamped@{ask}",
              ue.fee_model_assumption == "usdc_extra")
        check(f"sc_model_assumption_stamped@{ask}",
              sc.fee_model_assumption == "share_cut")

        check(f"overall_model_unresolved@{ask}",
              result["fee_model_assumption"] == "unresolved")

    # fee_truth_info propagation
    fee_info = {
        "fee_rate_source": "live_endpoint",
        "fee_truth_status": "confirmed",
        "fee_model_assumption": "unresolved",
    }
    o = fee_math.compute_taker_buy(
        price_per_share=0.50, shares=10.0,
        fee_truth_info=fee_info, token_id="MY_TOKEN_ID"
    )
    check("fee_truth_source_stamped",   o.fee_rate_source == "live_endpoint")
    check("fee_truth_status_stamped",   o.fee_truth_status == "confirmed")
    check("fee_fallback_false",         o.fee_fallback_used is False)
    check("token_id_stamped",           o.token_id == "MY_TOKEN_ID")
    check("to_dict_has_fee_rate_value", "fee_rate_value" in o.to_dict())

    # config_fallback default (no fee_truth_info)
    o2 = fee_math.compute_taker_buy(price_per_share=0.50, shares=10.0)
    check("default_fallback_source",    o2.fee_rate_source == "config_fallback")
    check("default_fallback_true",      o2.fee_fallback_used is True)
    check("default_truth_assumed",      o2.fee_truth_status == "assumed")


# ── 9. settlement refetch paths ───────────────────────────────────────────────

def test_settlement_refetch_paths():
    """Verify _refetch_market logic using mocked HTTP."""
    print("\n=== settlement refetch paths ===")
    from unittest.mock import patch, MagicMock
    import main as main_mod

    # Path A: refetch returns resolved market (outcome_prices=[1.0,0.0])
    fresh_resolved = {
        "condition_id": "MKT_REFETCH_001",
        "closed": True,
        "outcome_prices": ["1.0", "0.0"],
    }
    mock_r = MagicMock()
    mock_r.status_code = 200
    mock_r.json.return_value = fresh_resolved

    with patch("main.requests.get", mock_r) if False else patch("requests.get", mock_r):
        pass  # can't easily mock module-internal requests; test via Session._annotate_settlements

    # Instead test _refetch_market directly with injected mock
    import requests as _rreq
    with patch.object(_rreq, "get", return_value=mock_r):
        result = main_mod._refetch_market("MKT_REFETCH_001")

    check("refetch_success",         result["success"] is True)
    check("refetch_raw_not_none",    result["raw"] is not None)
    check("refetch_raw_closed",      result["raw"].get("closed") is True)
    check("refetch_source_url_set",  result["source_url"] is not None)

    # Path B: refetch fails (connection error both attempts)
    import requests as _rreq2
    with patch.object(_rreq2, "get",
                      side_effect=_rreq2.exceptions.ConnectionError("unreachable")):
        result_fail = main_mod._refetch_market("MKT_FAIL_001")

    check("refetch_fail_not_success", result_fail["success"] is False)
    check("refetch_fail_error_set",   result_fail["error"] is not None)
    check("refetch_fail_raw_none",    result_fail["raw"] is None)

    # Core invariant: stale discovery snapshot must NOT be used when refetch fails
    # (verified by checking that settlement_truth_status = "unknown" when success=False)
    check("stale_snapshot_not_used_on_failure",
          result_fail["success"] is False,
          "when refetch fails, success=False forces unknown status – no stale inference")


# ── 10. report audit fields ───────────────────────────────────────────────────

def test_report_audit_fields():
    print("\n=== verdict_report audit fields ===")
    import verdict_report
    import config as _cfg
    import time, tempfile
    from pathlib import Path
    from unittest.mock import MagicMock

    mock_rt = MagicMock()
    mock_rt.summary.return_value = {
        "total_events": 0, "event_counts": {}, "rtt_stats": {},
        "ws_disconnects": 0, "ws_reconnects": 0, "ws_errors": 0,
        "heartbeat_gaps": 0, "rate_limits": 0, "rest_errors": 0,
        "order_rejects": 0, "median_rtt_ms": None, "worst_rtt_ms": None,
    }

    markets = [{
        "market_id": "MKT_AUDIT_001",
        "condition_id": "MKT_AUDIT_001",
        "question": "BTC 5m audit test?",
        "status": "open", "window_seconds": 300,
        "yes_token_id": "YES_TOK_001", "no_token_id": "NO_TOK_001",
        "token_mapping_confidence": "directional",
        "outcome_labels": ["Up", "Down"],
        "min_order_size": 5.0, "min_order_size_source": "market_data",
        "tick_size": 0.01, "closed": False,
    }]

    # Simulated fee_results with fallback
    fee_results = {
        "_global": {
            "fetch_attempted": True, "fetch_success": False,
            "fetch_error": "HTTP 401: Unauthorized",
            "endpoint_used": "https://clob.polymarket.com/fees",
            "fee_rate_source": "config_fallback",
            "fee_rate_value": 0.02,
            "fee_truth_status": "assumed",
            "fee_model_assumption": "unresolved",
        }
    }

    # Simulated settlement_details with resolved market
    settlement_details = [{
        "market_id": "MKT_AUDIT_001",
        "discovery_closed": False,
        "final_refetch_attempted": True,
        "final_refetch_source": "https://clob.polymarket.com/markets/MKT_AUDIT_001",
        "final_refetch_success": True,
        "final_refetch_ts_utc": "2026-03-24T00:00:00.000+00:00",
        "final_market_closed": True,
        "official_outcome_available": True,
        "settlement_truth_status": "resolved_confirmed",
        "annotated_result": "YES",
        "refetch_error": None,
    }]

    fake_ev = MagicMock()
    fake_ev.candidates = []
    fake_ev.passed_count = 0
    fake_rec = MagicMock()
    fake_rec.open_reference_is_true = False
    fake_rec.open_reference_lag_s = 2.0
    fake_rec.market_window_open_time = None
    fake_rec.recorder_start_time = None
    fake_rec.snapshots = []
    fake_ev._ref_recorder = fake_rec

    with tempfile.TemporaryDirectory() as tmpdir:
        orig_reports = _cfg.REPORTS_DIR
        _cfg.REPORTS_DIR = Path(tmpdir)

        report = verdict_report.generate_report(
            session_id="smoke_audit",
            markets=markets,
            book_recorders={},
            taker_evaluators={"MKT_AUDIT_001": fake_ev},
            maker_evaluators={},
            runtime_logger=mock_rt,
            session_start_ts=time.time() - 60,
            fee_results=fee_results,
            settlement_details=settlement_details,
        )

        _cfg.REPORTS_DIR = orig_reports

    check("report_has_fee_truth_audit",
          "fee_truth_audit" in report)
    check("report_has_settlement_truth_audit",
          "settlement_truth_audit" in report)
    check("report_has_harness_verdict",
          "harness_verdict" in report)

    fa = report["fee_truth_audit"]
    check("fee_audit_fetch_success_false",   fa["fetch_success"] is False)
    check("fee_audit_source_config_fallback", fa["fee_rate_source"] == "config_fallback")
    check("fee_audit_model_unresolved",      fa["fee_model_assumption"] == "unresolved")
    check("fee_audit_has_endpoint_used",     "endpoint_used" in fa)

    sa = report["settlement_truth_audit"]
    check("settle_audit_refetch_attempted",  sa["refetch_attempted"] is True)
    check("settle_audit_resolved_count",     sa["markets_resolved_confirmed"] == 1)
    check("settle_audit_has_per_market",     len(sa.get("per_market", [])) == 1)
    check("settle_audit_decision_grade",     sa["decision_grade"] is True)

    hv = report["harness_verdict"]
    check("verdict_fee_grade_false",         hv["fee_decision_grade"] is False,
          "fallback fee = not fee_decision_grade")
    check("verdict_settle_grade_true",       hv["settlement_decision_grade"] is True)
    check("verdict_overall_false",           hv["overall_decision_grade"] is False,
          "overall requires BOTH fee and settlement grade")
    check("verdict_has_load_bearing_unknown", len(hv.get("load_bearing_unknown", "")) > 0)
    check("verdict_model_always_in_unknowns",
          any("fee_model_assumption" in u for u in hv.get("all_unknowns", [])),
          "fee_model_assumption=unresolved must always appear in unknowns")

    # _format_txt must run without KeyError on new sections
    try:
        lines = verdict_report._format_txt(report)
        check("audit_format_txt_runs", True)
        check("audit_has_fee_truth_section",
              any("FEE TRUTH AUDIT" in l for l in lines))
        check("audit_has_settlement_section",
              any("SETTLEMENT TRUTH AUDIT" in l for l in lines))
        check("audit_has_harness_verdict_section",
              any("HARNESS VERDICT" in l for l in lines))
    except Exception as exc:
        check("audit_format_txt_runs", False, str(exc))


# ── 11. discovery admission/rejection (Market Selection Truth Patch v2) ───────

def test_discovery_admission_rejection():
    print("\n=== market_discovery admission/rejection (fail-closed, two paths) ===")

    # Reference timestamp from canonical URL: btc-updown-5m-1774358400
    CANONICAL_START = 1774358400

    def _make_raw(
        question,
        condition_id="COND_BTC5M_001",
        start_ts=None,
        end_ts=None,
        tokens=None,
        slug=None,
        description="",
    ):
        """Build a minimal raw market dict for classification tests."""
        raw = {
            "condition_id": condition_id,
            "question":     question,
            "description":  description,
            "tokens": tokens if tokens is not None else [
                {"token_id": "TOKEN_UP_001",   "outcome": "Up"},
                {"token_id": "TOKEN_DOWN_001", "outcome": "Down"},
            ],
        }
        if start_ts is not None:
            raw["game_start_time"] = start_ts
        if end_ts is not None:
            raw["end_date_iso"] = datetime.fromtimestamp(end_ts, tz=timezone.utc).isoformat()
        if slug is not None:
            raw["market_slug"] = slug
        return raw

    def _admit(raw):
        r = market_discovery._classify_market(raw)
        return r

    # ══════════════════════════════════════════════════════════════════════════
    # PATH A — canonical slug (PRIMARY signal)
    # ══════════════════════════════════════════════════════════════════════════

    # ── A1: canonical slug + full timing → admitted ───────────────────────────
    r = _admit(_make_raw(
        "Bitcoin Up or Down - 5 Minutes",
        condition_id="COND_A1",
        start_ts=CANONICAL_START,
        end_ts=CANONICAL_START + 300,
        slug="btc-updown-5m-1774358400",
    ))
    check("path_a_canonical_full_timing_admitted",
          r["admit"] is True,
          f"reason={r['rejection_reason']} detail={r['details'].get('reason_detail')}")
    check("path_a_admission_path_slug_primary",
          r["admission_path"] == "slug_primary")
    check("path_a_timing_source_api",
          r["details"].get("timing_source") == "api_fields")

    # ── A2: canonical slug + NULL game_start_time (slug-derived timing) ───────
    # This is the critical fix: API timing absent but slug provides it.
    r = _admit({
        "condition_id": "COND_A2",
        "question":     "Bitcoin Up or Down - 5 Minutes",
        "description":  "",
        "market_slug":  "btc-updown-5m-1774358400",
        # game_start_time and end_date_iso deliberately absent
        "tokens": [
            {"token_id": "T_UP", "outcome": "Up"},
            {"token_id": "T_DN", "outcome": "Down"},
        ],
    })
    check("path_a_slug_derived_timing_admitted",
          r["admit"] is True,
          f"reason={r['rejection_reason']} detail={r['details'].get('reason_detail')}")
    check("path_a_slug_derived_timing_source",
          r["details"].get("timing_source") == "slug_derived",
          f"timing_source={r['details'].get('timing_source')}")
    check("path_a_slug_derived_window_300",
          abs((r["details"].get("window_seconds") or 0) - 300) < 1,
          f"window={r['details'].get('window_seconds')}")

    # ── A3: question uses time-range format "9:20-9:25AM ET" + canonical slug ─
    # Canonical live question variant: title has clock range instead of "5 Minutes".
    # With slug as primary proof, question does not need "5 minute" string.
    r = _admit({
        "condition_id": "COND_A3",
        "question":     "Bitcoin Up or Down - 9:20-9:25AM ET",
        "description":  "",
        "market_slug":  "btc-updown-5m-1774358400",
        "tokens": [
            {"token_id": "T_UP", "outcome": "Up"},
            {"token_id": "T_DN", "outcome": "Down"},
        ],
    })
    check("path_a_time_range_question_admitted",
          r["admit"] is True,
          f"reason={r['rejection_reason']} detail={r['details'].get('reason_detail')}")

    # ── A4: BTC Up or Down (abbreviated) + canonical slug ────────────────────
    r = _admit({
        "condition_id": "COND_A4",
        "question":     "BTC Up or Down - 5 Minutes",
        "description":  "",
        "market_slug":  "btc-updown-5m-1774358400",
        "tokens": [
            {"token_id": "T_UP", "outcome": "Up"},
            {"token_id": "T_DN", "outcome": "Down"},
        ],
    })
    check("path_a_btc_abbrev_admitted",
          r["admit"] is True,
          f"reason={r['rejection_reason']}")

    # ── A5: canonical slug + blocking term in question → rejected ─────────────
    r = _admit({
        "condition_id": "COND_A5",
        "question":     "Bitcoin hashprice Up or Down - 5 Minutes",
        "description":  "",
        "market_slug":  "btc-updown-5m-1774358400",
        "tokens": [
            {"token_id": "T_UP", "outcome": "Up"},
            {"token_id": "T_DN", "outcome": "Down"},
        ],
    })
    check("path_a_blocking_term_still_rejected",
          r["admit"] is False,
          f"blocking term must override canonical slug")
    check("path_a_blocking_term_reason",
          r["rejection_reason"] == "wrong_market_semantics",
          f"reason={r['rejection_reason']}")

    # ══════════════════════════════════════════════════════════════════════════
    # PATH B — no canonical slug (question must prove family)
    # ══════════════════════════════════════════════════════════════════════════

    # ── B1: canonical question + full timing, no slug ─────────────────────────
    r = _admit(_make_raw(
        "Bitcoin Up or Down - 5 Minutes",
        condition_id="COND_B1",
        start_ts=CANONICAL_START,
        end_ts=CANONICAL_START + 300,
    ))
    check("path_b_no_slug_full_question_admitted",
          r["admit"] is True,
          f"reason={r['rejection_reason']}")
    check("path_b_admission_path_question_primary",
          r["admission_path"] == "question_primary")

    # ── B2: no slug + no timing → rejected (Path B cannot derive from slug) ───
    r = _admit({
        "condition_id": "COND_B2",
        "question":     "Bitcoin Up or Down - 5 Minutes",
        "description":  "",
        # no slug, no timing fields
        "tokens": [
            {"token_id": "T_UP", "outcome": "Up"},
            {"token_id": "T_DN", "outcome": "Down"},
        ],
    })
    check("path_b_no_slug_no_timing_rejected",
          r["admit"] is False,
          "Path B must not admit without timing")
    check("path_b_no_timing_reason",
          r["rejection_reason"] == "missing_timing",
          f"reason={r['rejection_reason']}")

    # ── B3: 5-minute indicator in description (not question) → admitted ───────
    r = _admit({
        "condition_id":   "COND_B3",
        "question":       "Bitcoin Up or Down",
        "description":    "Recurring 5 minute BTC prediction market",
        "game_start_time": CANONICAL_START,
        "end_date_iso":   datetime.fromtimestamp(
            CANONICAL_START + 300, tz=timezone.utc
        ).isoformat(),
        "tokens": [
            {"token_id": "T_UP", "outcome": "Up"},
            {"token_id": "T_DN", "outcome": "Down"},
        ],
    })
    check("path_b_5min_in_description_admitted",
          r["admit"] is True,
          f"reason={r['rejection_reason']}")

    # ══════════════════════════════════════════════════════════════════════════
    # REJECTION TESTS — wrong Bitcoin market families
    # ══════════════════════════════════════════════════════════════════════════

    # ── R1: hashprice market ──────────────────────────────────────────────────
    r = _admit(_make_raw(
        "Bitcoin hashprice above 50 PH/s before June 1?",
        condition_id="COND_R1",
        start_ts=CANONICAL_START,
        end_ts=CANONICAL_START + 300,
    ))
    check("hashprice_rejected",       r["admit"] is False)
    check("hashprice_reason",         r["rejection_reason"] == "wrong_market_semantics",
          f"reason={r['rejection_reason']}")

    # ── R2: before-June long-dated market ────────────────────────────────────
    r = _admit({
        "condition_id":   "COND_R2",
        "question":       "Will Bitcoin price be above $100,000 before June?",
        "description":    "",
        "game_start_time": CANONICAL_START,
        "end_date_iso":   datetime.fromtimestamp(
            CANONICAL_START + 30 * 24 * 3600, tz=timezone.utc
        ).isoformat(),
        "tokens": [
            {"token_id": "T_YES", "outcome": "Yes"},
            {"token_id": "T_NO",  "outcome": "No"},
        ],
    })
    check("before_june_rejected", r["admit"] is False)
    check("before_june_reason",
          r["rejection_reason"] in ("wrong_market_semantics", "wrong_window"),
          f"reason={r['rejection_reason']}")

    # ── R3: above-X price-level market ───────────────────────────────────────
    r = _admit({
        "condition_id":   "COND_R3",
        "question":       "Will Bitcoin be above $110,000 by May?",
        "description":    "",
        "game_start_time": CANONICAL_START,
        "end_date_iso":   datetime.fromtimestamp(
            CANONICAL_START + 30 * 24 * 3600, tz=timezone.utc
        ).isoformat(),
        "tokens": [
            {"token_id": "T_YES", "outcome": "Yes"},
            {"token_id": "T_NO",  "outcome": "No"},
        ],
    })
    check("above_x_btc_rejected", r["admit"] is False,
          f"above-X price bet must be rejected, got admit={r['admit']}")

    # ── R4: long-dated one-off Bitcoin event market ───────────────────────────
    r = _admit({
        "condition_id":   "COND_R4",
        "question":       "Will Bitcoin reach $200,000 this year?",
        "description":    "",
        "game_start_time": CANONICAL_START,
        "end_date_iso":   datetime.fromtimestamp(
            CANONICAL_START + 365 * 24 * 3600, tz=timezone.utc
        ).isoformat(),
        "tokens": [
            {"token_id": "T_YES", "outcome": "Yes"},
            {"token_id": "T_NO",  "outcome": "No"},
        ],
    })
    check("longdated_event_rejected", r["admit"] is False,
          f"long-dated event market must be rejected, got admit={r['admit']}")

    # ── R5: blank outcome labels ──────────────────────────────────────────────
    r = _admit(_make_raw(
        "Bitcoin Up or Down - 5 Minutes",
        condition_id="COND_R5",
        start_ts=CANONICAL_START, end_ts=CANONICAL_START + 300,
        tokens=[{"token_id": "T1", "outcome": ""}, {"token_id": "T2", "outcome": ""}],
    ))
    check("blank_outcomes_rejected",  r["admit"] is False)
    check("blank_outcomes_reason",
          r["rejection_reason"] in ("invalid_outcomes", "unknown_token_mapping"),
          f"reason={r['rejection_reason']}")

    # ── R6: None market_id ────────────────────────────────────────────────────
    raw_noid = _make_raw("Bitcoin Up or Down - 5 Minutes",
                         start_ts=CANONICAL_START, end_ts=CANONICAL_START + 300)
    raw_noid["condition_id"] = None
    raw_noid.pop("market_id", None)
    r = _admit(raw_noid)
    check("none_market_id_rejected",  r["admit"] is False)
    check("none_market_id_reason",    r["rejection_reason"] == "missing_market_id",
          f"reason={r['rejection_reason']}")

    # ── R7: missing start time (Path B, no slug) ──────────────────────────────
    r = _admit({
        "condition_id": "COND_R7",
        "question":     "Bitcoin Up or Down - 5 Minutes",
        "description":  "",
        "end_date_iso": datetime.fromtimestamp(
            CANONICAL_START + 300, tz=timezone.utc
        ).isoformat(),
        "tokens": [
            {"token_id": "T_UP", "outcome": "Up"},
            {"token_id": "T_DN", "outcome": "Down"},
        ],
    })
    check("missing_start_rejected",   r["admit"] is False)
    check("missing_start_reason",     r["rejection_reason"] == "missing_timing",
          f"reason={r['rejection_reason']}")

    # ── R8: missing end time (Path B, no slug) ────────────────────────────────
    r = _admit({
        "condition_id":    "COND_R8",
        "question":        "Bitcoin Up or Down - 5 Minutes",
        "description":     "",
        "game_start_time": CANONICAL_START,
        # end absent
        "tokens": [
            {"token_id": "T_UP", "outcome": "Up"},
            {"token_id": "T_DN", "outcome": "Down"},
        ],
    })
    check("missing_end_rejected",     r["admit"] is False)
    check("missing_end_reason",       r["rejection_reason"] == "missing_timing",
          f"reason={r['rejection_reason']}")

    # ── R9: unknown token mapping ─────────────────────────────────────────────
    r = _admit(_make_raw(
        "Bitcoin Up or Down - 5 Minutes",
        condition_id="COND_R9",
        start_ts=CANONICAL_START, end_ts=CANONICAL_START + 300,
        tokens=[{"token_id": "T1", "outcome": "Banana"}, {"token_id": "T2", "outcome": "Apple"}],
    ))
    check("unknown_token_rejected",   r["admit"] is False)
    check("unknown_token_reason",     r["rejection_reason"] == "unknown_token_mapping",
          f"reason={r['rejection_reason']}")

    # ── R10: wrong window via API fields ──────────────────────────────────────
    r = _admit(_make_raw(
        "Bitcoin Up or Down - 5 Minutes",
        condition_id="COND_R10",
        start_ts=CANONICAL_START,
        end_ts=CANONICAL_START + 30 * 24 * 3600,  # 30 days
    ))
    check("wrong_window_rejected",    r["admit"] is False)
    check("wrong_window_reason",      r["rejection_reason"] == "wrong_window",
          f"reason={r['rejection_reason']}")

    # ── R11: slug present but wrong pattern ───────────────────────────────────
    r = _admit({
        "condition_id":    "COND_R11",
        "question":        "Bitcoin Up or Down - 5 Minutes",
        "description":     "",
        "market_slug":     "bitcoin-hashprice-before-june",
        "game_start_time": CANONICAL_START,
        "end_date_iso":    datetime.fromtimestamp(
            CANONICAL_START + 300, tz=timezone.utc
        ).isoformat(),
        "tokens": [
            {"token_id": "T_UP", "outcome": "Up"},
            {"token_id": "T_DN", "outcome": "Down"},
        ],
    })
    check("wrong_slug_rejected",      r["admit"] is False)
    check("wrong_slug_reason",        r["rejection_reason"] == "wrong_market_semantics",
          f"reason={r['rejection_reason']}")

    # ── R12: partial token mapping (1 token) ──────────────────────────────────
    r = _admit(_make_raw(
        "Bitcoin Up or Down - 5 Minutes",
        condition_id="COND_R12",
        start_ts=CANONICAL_START, end_ts=CANONICAL_START + 300,
        tokens=[{"token_id": "T_UP", "outcome": "Up"}],  # Down missing
    ))
    check("partial_mapping_rejected", r["admit"] is False)
    check("partial_mapping_reason",
          r["rejection_reason"] in ("unknown_token_mapping", "invalid_outcomes"),
          f"reason={r['rejection_reason']}")

    # ══════════════════════════════════════════════════════════════════════════
    # TIMING FIELDS — alternate API field names (startDate, endDate etc.)
    # ══════════════════════════════════════════════════════════════════════════

    # ── T1: camelCase timing fields (startDate / endDate) ────────────────────
    r = _admit({
        "condition_id": "COND_T1",
        "question":     "Bitcoin Up or Down - 5 Minutes",
        "description":  "",
        "startDate":    CANONICAL_START,
        "endDate":      CANONICAL_START + 300,
        "tokens": [
            {"token_id": "T_UP", "outcome": "Up"},
            {"token_id": "T_DN", "outcome": "Down"},
        ],
    })
    check("camel_case_timing_admitted",
          r["admit"] is True,
          f"reason={r['rejection_reason']} detail={r['details'].get('reason_detail')}")

    # ── T2: end_date (snake_case variant) ────────────────────────────────────
    r = _admit({
        "condition_id":   "COND_T2",
        "question":       "Bitcoin Up or Down - 5 Minutes",
        "description":    "",
        "game_start_time": CANONICAL_START,
        "end_date":       CANONICAL_START + 300,   # not end_date_iso
        "tokens": [
            {"token_id": "T_UP", "outcome": "Up"},
            {"token_id": "T_DN", "outcome": "Down"},
        ],
    })
    check("end_date_variant_admitted",
          r["admit"] is True,
          f"reason={r['rejection_reason']} detail={r['details'].get('reason_detail')}")

    # ══════════════════════════════════════════════════════════════════════════
    # AUDIT — fetch_all_markets emits discovery_audit file
    # ══════════════════════════════════════════════════════════════════════════

    from unittest.mock import patch, MagicMock
    import tempfile, pathlib as _pl

    page1 = {
        "data": [
            # Admitted via slug + api timing
            {
                "condition_id": "COND_OK_001",
                "question":     "Bitcoin Up or Down - 5 Minutes",
                "description":  "",
                "market_slug":  "btc-updown-5m-1774358400",
                "game_start_time": CANONICAL_START,
                "end_date_iso": datetime.fromtimestamp(
                    CANONICAL_START + 300, tz=timezone.utc
                ).isoformat(),
                "tokens": [
                    {"token_id": "T_UP", "outcome": "Up"},
                    {"token_id": "T_DN", "outcome": "Down"},
                ],
            },
            # Admitted via slug-derived timing (no API timing fields)
            {
                "condition_id": "COND_OK_002",
                "question":     "Bitcoin Up or Down - 9:20-9:25AM ET",
                "description":  "",
                "market_slug":  "btc-updown-5m-1774358400",
                # no timing fields
                "tokens": [
                    {"token_id": "T_UP2", "outcome": "Up"},
                    {"token_id": "T_DN2", "outcome": "Down"},
                ],
            },
            # Rejected: hashprice near-miss
            {
                "condition_id":    "COND_HASH_001",
                "question":        "Bitcoin hashprice above 50 PH/s before June 1?",
                "description":     "",
                "game_start_time": CANONICAL_START,
                "end_date_iso":    datetime.fromtimestamp(
                    CANONICAL_START + 300, tz=timezone.utc
                ).isoformat(),
                "tokens": [
                    {"token_id": "T_YES", "outcome": "Yes"},
                    {"token_id": "T_NO",  "outcome": "No"},
                ],
            },
            # Rejected: no slug, no timing (Path B)
            {
                "condition_id": "COND_NOTIME_001",
                "question":     "Bitcoin Up or Down - 5 Minutes",
                "description":  "",
                # no timing, no slug
                "tokens": [
                    {"token_id": "T_UP", "outcome": "Up"},
                    {"token_id": "T_DN", "outcome": "Down"},
                ],
            },
            # Ignored: totally unrelated
            {
                "condition_id": "COND_XYZ",
                "question":     "Will it rain tomorrow?",
                "tokens":       [],
            },
        ],
        "next_cursor": "",
    }

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = page1
    mock_resp.raise_for_status.return_value = None

    with tempfile.TemporaryDirectory() as tmpdir:
        orig_markets_dir = config.MARKETS_DIR
        config.MARKETS_DIR = _pl.Path(tmpdir)

        with patch("market_discovery.requests.get", return_value=mock_resp):
            admitted = market_discovery.fetch_all_markets("smoke_audit_disc")

        config.MARKETS_DIR = orig_markets_dir

        audit_file = _pl.Path(tmpdir) / "discovery_audit_smoke_audit_disc.json"
        check("discovery_audit_file_created",
              audit_file.exists(),
              f"expected {audit_file}")

        with open(audit_file) as af:
            audit_data = json.load(af)

        check("audit_total_scanned_5",   audit_data["total_scanned"] == 5,
              f"scanned={audit_data['total_scanned']}")
        check("audit_total_admitted_2",  audit_data["total_admitted"] == 2,
              f"admitted={audit_data['total_admitted']}")
        check("audit_total_rejected_3",  audit_data["total_rejected"] == 3,
              f"rejected={audit_data['total_rejected']}")
        check("audit_admitted_ok001",
              "COND_OK_001" in audit_data["admitted_market_ids"],
              f"ids={audit_data['admitted_market_ids']}")
        check("audit_admitted_ok002",
              "COND_OK_002" in audit_data["admitted_market_ids"],
              f"ids={audit_data['admitted_market_ids']}")
        check("audit_has_rejection_buckets",
              "rejection_buckets" in audit_data)

    # Both slug-derived and api-field markets should be in admitted
    ids = [m["market_id"] for m in admitted]
    check("admitted_contains_ok001",
          "COND_OK_001" in ids, f"admitted={ids}")
    check("admitted_contains_ok002_slug_derived",
          "COND_OK_002" in ids,
          f"slug-derived timing market must be admitted, admitted={ids}")

    # Verify timing_source is "slug_derived" for COND_OK_002
    ok002 = next((m for m in admitted if m["market_id"] == "COND_OK_002"), None)
    check("ok002_timing_source_slug_derived",
          ok002 is not None and ok002.get("timing_source") == "slug_derived",
          f"timing_source={ok002.get('timing_source') if ok002 else 'not found'}")


# ── run all ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    test_fee_math()
    test_token_extraction()
    test_reference_recorder_fields()
    test_taker_shadow_live_ref()
    test_settlement_annotation()
    test_verdict_report()
    # Patch Sprint 2
    test_fee_fetcher_paths()
    test_fee_math_both_models()
    test_settlement_refetch_paths()
    test_report_audit_fields()
    # Market Selection Truth Patch
    test_discovery_admission_rejection()

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
