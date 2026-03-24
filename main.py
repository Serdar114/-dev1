"""
main.py - Session orchestrator for the Polymarket BTC 5-minute measurement harness.

Execution flow:
  1. Initialize session (session_id, runtime logger, endpoint probes)
  2. Discover active BTC 5-minute markets
  3. For each market, launch in parallel:
       - reference_recorder  (BTC price at T-60/30/15/10/5/1/0)
       - book_recorder REST  (YES + NO sides, 500ms poll, 200ms in last 60s)
       - book_recorder WS    (supplementary, real-time)
       - taker_shadow        (evaluate hypothetical late-window taker entries)
       - maker_shadow        (log hypothetical quotes, measure adverse selection)
  4. Wait for all markets to close (with configurable max session duration)
  5. Annotate settlement outcomes on taker candidates
  6. Generate and print verdict report

Usage:
  python main.py [--duration 3600] [--session-id <id>] [--dry-run]

Flags:
  --duration N      Max session wall-clock time in seconds (default: 3600)
  --session-id ID   Override auto-generated session ID
  --dry-run         Run discovery only, skip recording threads
  --no-ws           Disable WebSocket book recorder (REST only)
  --log-level LEVEL Python logging level (default: INFO)

No live orders. No strategy claims. Survival-first measurement.
"""

import argparse
import logging
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

import config
import market_discovery
import reference_recorder
import book_recorder
import runtime_logger as rl_module
import taker_shadow
import maker_shadow
import verdict_report
import fee_fetcher

log = logging.getLogger(__name__)


# ── session state ─────────────────────────────────────────────────────────────

class Session:
    def __init__(self, session_id: str, dry_run: bool = False, no_ws: bool = False):
        self.session_id    = session_id
        self.dry_run       = dry_run
        self.no_ws         = no_ws
        self.start_ts      = time.time()
        self.shutdown      = threading.Event()

        # Shared state
        self.markets: List[Dict] = []
        self.book_recorders: Dict[str, Dict] = {}
        self.ref_recorders: Dict[str, reference_recorder.MarketReferenceRecorder] = {}
        self.taker_evaluators: Dict[str, taker_shadow.TakerShadowEvaluator] = {}
        self.maker_evaluators: Dict[str, List[maker_shadow.MakerShadowEvaluator]] = {}

        # Shared mutable bankroll (single-element list for thread-safe pass-by-ref)
        self.bankroll_ref: List[float] = [config.BANKROLL_USDC]

        # Fee fetch results (set in _launch_recorders before shadow threads start)
        self.fee_results: Dict = {}

        # Settlement details – list of per-market refetch records (set in _annotate_settlements)
        self.settlement_details: List[Dict] = []

        # Runtime logger (singleton for session)
        self.runtime = rl_module.RuntimeLogger(session_id)

    def _ref_recorders_by_market(self) -> Dict:
        """
        Return the live recorder objects keyed by market_id.
        Do NOT copy the snapshots list here – taker shadow evaluators
        must hold a reference to the recorder so they read live .snapshots.
        """
        return dict(self.ref_recorders)

    def run(self, max_duration_s: float = 3600.0):
        """Main session loop."""
        log.info("=" * 60)
        log.info("SESSION START  id=%s  dry_run=%s", self.session_id, self.dry_run)
        log.info("Bankroll=%.2f USDC  max_pos=%.2f USDC", config.BANKROLL_USDC, config.MAX_POSITION_USDC)
        log.info("=" * 60)

        self.runtime.log(rl_module.EV_SESSION_START, {
            "session_id": self.session_id,
            "bankroll": config.BANKROLL_USDC,
            "dry_run": self.dry_run,
        }, source="main")

        # ── 1. Probe endpoints ────────────────────────────────────────────────
        log.info("[main] Probing endpoints...")
        rtts = rl_module.probe_endpoints(self.runtime)
        for ep, ms in rtts.items():
            log.info("[main] %s RTT: %.0fms", ep, ms)

        # ── 2. Market discovery ───────────────────────────────────────────────
        log.info("[main] Discovering BTC 5-minute markets...")
        try:
            self.markets = market_discovery.fetch_all_markets(self.session_id)
        except Exception as exc:
            log.error("[main] Market discovery failed: %s", exc)
            self.runtime.log(rl_module.EV_GENERIC_ERROR, {
                "stage": "market_discovery", "error": str(exc)
            }, source="main")
            self.markets = []

        if not self.markets:
            log.warning("[main] No BTC 5-minute markets found. Session will still run for reference recording.")

        if self.dry_run:
            log.info("[main] DRY RUN – stopping after discovery.")
            self._finalize()
            return

        market_ids = [m.get("market_id") or m.get("condition_id", "?") for m in self.markets]
        self.runtime.start_heartbeat_monitor(market_ids)

        # ── 3. Launch per-market recording threads ────────────────────────────
        if self.markets:
            log.info("[main] Launching recorders for %d market(s)...", len(self.markets))
            self._launch_recorders()
        else:
            log.warning("[main] No markets – only runtime probing active.")

        # ── 4. Wait for session end ───────────────────────────────────────────
        log.info("[main] Session running. max_duration=%.0fs. Ctrl-C to stop.", max_duration_s)
        deadline = self.start_ts + max_duration_s

        while not self.shutdown.is_set():
            remaining = deadline - time.time()
            if remaining <= 0:
                log.info("[main] Max session duration reached.")
                break

            # Check if all markets have closed
            if self.markets:
                any_open = any(
                    _market_still_open(m) for m in self.markets
                )
                if not any_open:
                    log.info("[main] All markets closed.")
                    # Extra buffer to capture late book snapshots
                    time.sleep(15)
                    break

            self.shutdown.wait(timeout=30.0)

        # ── 5. Shutdown recording threads ─────────────────────────────────────
        log.info("[main] Signalling shutdown...")
        self.shutdown.set()
        # Give threads a moment to finish writing
        time.sleep(3)

        # ── 6. Annotate settlement on taker candidates ─────────────────────────
        self._annotate_settlements()

        # ── 7. Collect maker summaries ─────────────────────────────────────────
        maker_csv = maker_shadow.MakerSummaryCsvWriter(self.session_id)
        maker_shadow.collect_maker_summaries(self.maker_evaluators, maker_csv)

        # ── 8. Generate verdict report ─────────────────────────────────────────
        self._finalize()

    def _launch_recorders(self):
        """Launch all recording threads for all markets."""
        runtime_log_fn = self.runtime.as_log_fn(source="book_recorder")

        # ── Fee rate fetch (attempt live; fall back to config with explicit label) ─
        log.info("[main] Fetching live fee rate from CLOB...")
        self.fee_results = fee_fetcher.session_fee_fetch(self.markets)
        g = self.fee_results.get("_global", {})
        log.info(
            "[main] Fee rate: value=%.4f  source=%s  truth_status=%s  model_assumption=%s",
            g.get("fee_rate_value", config.TAKER_FEE_RATE),
            g.get("fee_rate_source", "?"),
            g.get("fee_truth_status", "?"),
            g.get("fee_model_assumption", "?"),
        )

        # Reference recorders
        self.ref_recorders = reference_recorder.run_reference_recorders(
            session_id=self.session_id,
            markets=self.markets,
            shutdown_event=self.shutdown,
        )

        # Book recorders (REST + optionally WS)
        self.book_recorders = book_recorder.run_book_recorders(
            session_id=self.session_id,
            markets=self.markets,
            shutdown_event=self.shutdown,
            runtime_log_fn=runtime_log_fn if not self.no_ws else None,
            heartbeat_notify_fn=self.runtime.notify_ws_msg if not self.no_ws else None,
        )

        # Give book recorders a head start before shadow evaluators read from them
        time.sleep(2)

        # Taker shadow evaluators – pass live recorder objects, NOT snapshot copies
        ref_recs_by_market = self._ref_recorders_by_market()
        self.taker_evaluators = taker_shadow.run_taker_shadows(
            session_id=self.session_id,
            markets=self.markets,
            book_recorders=self.book_recorders,
            ref_recorders_by_market=ref_recs_by_market,
            runtime_logger=self.runtime,
            shutdown_event=self.shutdown,
            bankroll_ref=self.bankroll_ref,
            fee_results=self.fee_results,
        )

        # Maker shadow evaluators
        self.maker_evaluators = maker_shadow.run_maker_shadows(
            session_id=self.session_id,
            markets=self.markets,
            book_recorders=self.book_recorders,
            runtime_logger=self.runtime,
            shutdown_event=self.shutdown,
        )

    def _annotate_settlements(self):
        """
        Refetch each market from CLOB REST at session finalization and annotate
        taker candidates with settlement truth.

        NEVER uses self.markets (discovery-time snapshot) as settlement truth.
        ALWAYS refetches a fresh copy of the market object.

        Settlement truth status labels:
          resolved_confirmed        – refetch success, market closed, outcome_prices
                                      contains exactly 1.0 or 0.0 for YES token
          unresolved                – refetch success, market not yet closed
          pending                   – refetch success, market closed but no
                                      outcome_prices yet (resolution may lag)
          unknown                   – refetch failed, or data present but not
                                      parseable to an exact binary outcome
        """
        self.settlement_details = []

        for market in self.markets:
            mid       = market.get("market_id") or market.get("condition_id", "?")
            evaluator = self.taker_evaluators.get(mid)

            # Record discovery-time state (for audit)
            discovery_closed = market.get("closed") is True

            # ── Fresh refetch ─────────────────────────────────────────────────
            refetch_rec = _refetch_market(mid)

            fresh = refetch_rec.get("raw")
            refetch_success = refetch_rec.get("success", False)

            # ── Determine settlement truth from fresh data ────────────────────
            settled_yes: Optional[float] = None
            settlement_truth_status = "unknown"
            official_outcome_available = False
            final_market_closed = False

            if refetch_success and fresh is not None:
                final_market_closed = fresh.get("closed") is True

                if final_market_closed:
                    outcome_prices = fresh.get("outcome_prices")
                    if outcome_prices and len(outcome_prices) >= 2:
                        try:
                            yes_price = float(outcome_prices[0])
                            if yes_price == 1.0 or yes_price == 0.0:
                                settled_yes = yes_price
                                settlement_truth_status = "resolved_confirmed"
                                official_outcome_available = True
                                log.info(
                                    "[main] market=%s settlement=resolved_confirmed "
                                    "YES=%.1f (from fresh refetch)",
                                    mid[:12], settled_yes,
                                )
                            else:
                                settlement_truth_status = "unknown"
                                log.warning(
                                    "[main] market=%s outcome_prices[0]=%s not 0 or 1 "
                                    "in fresh refetch – cannot resolve",
                                    mid[:12], yes_price,
                                )
                        except (ValueError, TypeError) as exc:
                            settlement_truth_status = "unknown"
                            log.warning(
                                "[main] market=%s outcome_prices parse error in refetch: %s",
                                mid[:12], exc,
                            )
                    else:
                        # Closed but no outcome_prices yet; resolution may lag
                        settlement_truth_status = "pending"
                        log.warning(
                            "[main] market=%s closed (refetch) but outcome_prices absent "
                            "– settlement_truth_status=pending. "
                            "Do NOT infer from last trade price.",
                            mid[:12],
                        )
                else:
                    settlement_truth_status = "unresolved"
                    log.info("[main] market=%s not closed at refetch time – unresolved", mid[:12])
            else:
                settlement_truth_status = "unknown"
                log.warning(
                    "[main] market=%s settlement refetch failed: %s – "
                    "status=unknown. Stale discovery snapshot NOT used.",
                    mid[:12], refetch_rec.get("error"),
                )

            # Annotated result label for report
            if settled_yes == 1.0:
                annotated_result = "YES"
            elif settled_yes == 0.0:
                annotated_result = "NO"
            else:
                annotated_result = None

            detail = {
                "market_id":                  mid,
                "discovery_closed":           discovery_closed,
                "final_refetch_attempted":    True,
                "final_refetch_source":       refetch_rec.get("source_url"),
                "final_refetch_success":      refetch_success,
                "final_refetch_ts_utc":       refetch_rec.get("fetch_ts_utc"),
                "final_market_closed":        final_market_closed,
                "official_outcome_available": official_outcome_available,
                "settlement_truth_status":    settlement_truth_status,
                "annotated_result":           annotated_result,
                "refetch_error":              refetch_rec.get("error"),
            }
            self.settlement_details.append(detail)

            if evaluator is not None and evaluator.candidates:
                evaluator.annotate_settlement(
                    settled_yes_price=settled_yes,
                    settlement_source=settlement_truth_status,
                )

    def _finalize(self):
        """Generate verdict report and print to console."""
        self.runtime.log(rl_module.EV_SESSION_END, {
            "session_id": self.session_id,
        }, source="main")
        self.runtime.shutdown()

        report = verdict_report.generate_report(
            session_id=self.session_id,
            markets=self.markets,
            book_recorders=self.book_recorders,
            taker_evaluators=self.taker_evaluators,
            maker_evaluators=self.maker_evaluators,
            runtime_logger=self.runtime,
            session_start_ts=self.start_ts,
            fee_results=self.fee_results,
            settlement_details=self.settlement_details,
        )

        print()
        verdict_report.print_report(report)


# ── helpers ───────────────────────────────────────────────────────────────────

def _market_still_open(market: Dict) -> bool:
    end_str = market.get("end_time_utc")
    if not end_str:
        return True
    try:
        end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
        return end_dt > datetime.now(timezone.utc)
    except ValueError:
        return True


def _refetch_market(condition_id: str) -> Dict:
    """
    Fetch a fresh copy of a market object from the CLOB REST API.

    Tries:
      1. GET /markets/{condition_id}         (single-market endpoint)
      2. GET /markets?condition_id=<id>      (filtered list endpoint)

    Returns a dict with:
      success      : bool
      raw          : dict or None (fresh market object)
      source_url   : str (endpoint that succeeded or last tried)
      fetch_ts_utc : ISO timestamp of fetch
      error        : str or None
    """
    import requests as _req
    from datetime import datetime as _dt, timezone as _tz

    now_utc = _dt.now(_tz.utc).isoformat(timespec="milliseconds")
    result = {
        "success":      False,
        "raw":          None,
        "source_url":   None,
        "fetch_ts_utc": now_utc,
        "error":        None,
    }

    # Attempt 1: direct single-market endpoint
    url1 = f"{config.CLOB_BASE_URL}/markets/{condition_id}"
    result["source_url"] = url1
    try:
        resp = _req.get(url1, timeout=config.HTTP_TIMEOUT_S,
                        headers={"Accept": "application/json"})
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict):
                result["success"] = True
                result["raw"]     = data
                log.info("[main] _refetch_market: %s via %s", condition_id[:12], url1)
                return result
    except Exception as exc:
        result["error"] = f"attempt1 {type(exc).__name__}: {exc}"

    # Attempt 2: filtered list endpoint
    url2 = f"{config.CLOB_BASE_URL}/markets"
    result["source_url"] = url2
    try:
        resp = _req.get(url2, params={"condition_id": condition_id},
                        timeout=config.HTTP_TIMEOUT_S,
                        headers={"Accept": "application/json"})
        if resp.status_code == 200:
            data = resp.json()
            # Response may be {"data": [...], ...} or a direct list
            items = data.get("data", data) if isinstance(data, dict) else data
            if isinstance(items, list):
                for item in items:
                    if (item.get("condition_id") == condition_id
                            or item.get("market_id") == condition_id):
                        result["success"]    = True
                        result["raw"]        = item
                        result["source_url"] = url2
                        log.info("[main] _refetch_market: %s via %s (list)",
                                 condition_id[:12], url2)
                        return result
        result["error"] = (
            (result.get("error") or "") +
            f" | attempt2 HTTP {resp.status_code}"
        )
    except Exception as exc:
        result["error"] = (
            (result.get("error") or "") +
            f" | attempt2 {type(exc).__name__}: {exc}"
        )

    log.warning("[main] _refetch_market FAILED for %s: %s",
                condition_id[:12], result["error"])
    return result


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Polymarket BTC 5-minute measurement harness"
    )
    parser.add_argument(
        "--duration", type=float, default=3600.0,
        help="Max session wall-clock time in seconds (default: 3600)"
    )
    parser.add_argument(
        "--session-id", type=str, default=None,
        help="Override auto-generated session ID"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Run market discovery only, no recording threads"
    )
    parser.add_argument(
        "--no-ws", action="store_true",
        help="Disable WebSocket book recorder (REST only)"
    )
    parser.add_argument(
        "--log-level", type=str, default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Python logging level"
    )
    args = parser.parse_args()

    # Configure logging
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format=config.LOG_FORMAT,
        datefmt=config.LOG_DATEFMT,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(
                config.RUNTIME_DIR / f"harness_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}.log"
            ),
        ],
    )

    session_id = args.session_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    session = Session(session_id=session_id, dry_run=args.dry_run, no_ws=args.no_ws)

    # Graceful shutdown on SIGINT / SIGTERM
    def _signal_handler(sig, frame):
        log.info("[main] Signal %s received – shutting down...", sig)
        session.shutdown.set()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    session.run(max_duration_s=args.duration)


if __name__ == "__main__":
    main()
