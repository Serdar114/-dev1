"""
taker_shadow.py - Shadow evaluator for late-window taker entries.

Purpose:
  Evaluate hypothetical taker entries WITHOUT placing live orders.
  Log every candidate with full gate breakdown.

Candidate classification (replaces single all_gates_pass bool):
  signal_candidate    – timing + BTC directional signal present
  pricing_candidate   – signal_candidate + spread tight + depth ok
  execution_candidate – pricing_candidate + min_size fits bankroll
  ev_candidate        – execution_candidate + edge_known=True AND edge > threshold
                        NOTE: edge_known is only True if caller supplied a real
                        true_prob estimate. Using market mid as true_prob is
                        circular (mid < ask < ask*(1+fee), so edge is always
                        negative by construction). If no honest true_prob is
                        available, ev_candidate is set to None (unknown),
                        NOT False.

EV gate:
  The previous implementation set true_prob = market mid. Since mid < ask,
  edge = mid - ask*(1+fee) < 0 always, making gate_ev structurally always
  False and all_gates_pass always False. That is now removed.
  The ev_candidate field is "unknown" (not False) when no external true_prob
  is supplied. Callers must supply an honest true_prob to get ev_candidate=True.

Signal wiring:
  The evaluator holds a direct reference to the recorder's live snapshots list
  (not a copy). Snapshots are read dynamically on each evaluation tick.
  A debug log line prints the snapshot count each tick to confirm live state.

Persistence:
  data/shadows/taker_candidates_<session_id>.jsonl
  data/shadows/taker_candidates_<session_id>.csv

No live orders. No edge claims.
"""

import csv
import json
import logging
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional, Tuple

import config
import fee_math

log = logging.getLogger(__name__)


# ── constants ─────────────────────────────────────────────────────────────────
MIN_TAKER_ASK_DEPTH_USDC  = 5.0   # require at least $5 at best ask
BTC_MOMENTUM_THRESHOLD_PCT = 0.05  # BTC must move ≥ 0.05% to have directional signal
BTC_LOOKBACK_SNAPSHOTS     = 6     # use last 6 reference snapshots for delta computation


# ── candidate record ──────────────────────────────────────────────────────────

def _make_candidate(
    session_id: str,
    market_id: str,
    token_id: str,
    side_label: str,          # "YES"/"NO" or "UP"/"DOWN"
    ts_ms: int,
    seconds_to_close: float,
    best_bid: float,
    best_ask: float,
    spread_pct: float,
    ask_depth_usdc: float,
    min_order_size: float,
    min_order_size_source: str,  # "market_data" or "default"
    btc_delta_pct: Optional[float],
    direction_signal: Optional[str],  # "UP", "DOWN", or None
    true_prob_estimate: Optional[float],  # None = no honest estimate available
    bankroll_remaining: float,
    fee_truth_info: Optional[Dict] = None,  # from fee_fetcher; None → config_fallback
) -> Dict:
    """
    Build and evaluate a taker candidate record.

    Returns a dict with four classification fields instead of a single bool:
      signal_candidate    : timing window + directional signal present
      pricing_candidate   : signal + spread + depth ok
      execution_candidate : pricing + min_size fits bankroll
      ev_candidate        : True/False/None
                            True  = execution + edge_known + edge > threshold
                            False = execution ok BUT edge known and insufficient
                            None  = ev cannot be assessed (no true_prob supplied)
    """
    ts_utc = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat(timespec="milliseconds")
    position_usdc = min(config.MAX_POSITION_USDC, bankroll_remaining)

    gates: Dict[str, bool] = {}
    reasons: Dict[str, str] = {}

    # ── Gate 1: timing ────────────────────────────────────────────────────────
    in_window = (config.TAKER_MIN_SECONDS_TO_CLOSE
                 <= seconds_to_close
                 <= config.TAKER_MAX_SECONDS_TO_CLOSE)
    gates["timing"] = in_window
    reasons["timing"] = (
        f"stc={seconds_to_close:.1f}s in [{config.TAKER_MIN_SECONDS_TO_CLOSE},{config.TAKER_MAX_SECONDS_TO_CLOSE}]"
        if in_window else f"stc={seconds_to_close:.1f}s OUTSIDE window"
    )

    # ── Gate 2: spread ────────────────────────────────────────────────────────
    tight_spread = spread_pct < config.TAKER_MAX_SPREAD_PCT * 100
    gates["spread"] = tight_spread
    reasons["spread"] = (
        f"spread={spread_pct:.2f}% < {config.TAKER_MAX_SPREAD_PCT*100:.1f}%"
        if tight_spread else f"spread={spread_pct:.2f}% TOO WIDE"
    )

    # ── Gate 3: depth ─────────────────────────────────────────────────────────
    deep_enough = ask_depth_usdc >= MIN_TAKER_ASK_DEPTH_USDC
    gates["depth"] = deep_enough
    reasons["depth"] = (
        f"ask_depth={ask_depth_usdc:.2f} USDC ok"
        if deep_enough else f"ask_depth={ask_depth_usdc:.2f} USDC < {MIN_TAKER_ASK_DEPTH_USDC}"
    )

    # ── Gate 4: min_size ──────────────────────────────────────────────────────
    desired_shares = fee_math.max_shares_from_usdc(position_usdc, best_ask)
    size_check = fee_math.check_min_size(
        shares=desired_shares,
        min_shares=min_order_size,
        ask_price=best_ask,
        usdc_budget=position_usdc,
        min_size_source=min_order_size_source,
    )
    gates["min_size"] = size_check["viable"]
    reasons["min_size"] = size_check["reason"]

    # ── Gate 5: directional signal ────────────────────────────────────────────
    has_signal = direction_signal is not None
    gates["signal"] = has_signal
    reasons["signal"] = (
        f"signal={direction_signal} btc_delta={btc_delta_pct:.3f}%"
        if has_signal else "no directional signal"
    )

    # ── Gate 6: bankroll ──────────────────────────────────────────────────────
    total_cost = size_check.get("cost_usdc", 9999)
    bank_ok = total_cost <= bankroll_remaining
    gates["bankroll"] = bank_ok
    reasons["bankroll"] = (
        f"cost={total_cost:.4f} <= bankroll={bankroll_remaining:.4f}"
        if bank_ok else f"cost={total_cost:.4f} > bankroll={bankroll_remaining:.4f}"
    )

    # ── Candidate tier classification ─────────────────────────────────────────
    signal_candidate    = gates["timing"] and gates["signal"]
    pricing_candidate   = signal_candidate and gates["spread"] and gates["depth"]
    execution_candidate = pricing_candidate and gates["min_size"] and gates["bankroll"]

    # ── EV assessment (NOT a gate – logged as True/False/None) ───────────────
    # WARNING: do NOT pass true_prob=market_mid here.
    # mid < ask always, so edge = mid - ask*(1+fee) < 0 always.
    # That makes ev_candidate permanently False by construction, not by signal.
    # ev_candidate is None when true_prob_estimate is None.
    net_ev       = None
    ev_candidate: Optional[bool] = None
    ev_reason    = "no true_prob_estimate supplied – ev_candidate=None (unknown)"

    if true_prob_estimate is not None and execution_candidate:
        order = fee_math.compute_taker_buy(
            price_per_share=best_ask,
            shares=size_check["shares_floored"],
            true_prob=true_prob_estimate,
            fee_truth_info=fee_truth_info,
            token_id=token_id,
            min_order_size_used=min_order_size,
        )
        net_ev = order.edge_at_true_prob
        if net_ev is not None:
            ev_candidate = net_ev > config.TAKER_MIN_EDGE_AFTER_FEE
            ev_reason = (
                f"edge={net_ev:.4f} > {config.TAKER_MIN_EDGE_AFTER_FEE} (POSITIVE)"
                if ev_candidate else
                f"edge={net_ev:.4f} <= {config.TAKER_MIN_EDGE_AFTER_FEE} (INSUFFICIENT)"
            )

    # ── Fee math for the hypothetical order ───────────────────────────────────
    order_detail: Dict = {}
    if size_check["viable"] and bankroll_remaining > 0:
        try:
            order = fee_math.compute_taker_buy(
                price_per_share=best_ask,
                shares=size_check["shares_floored"],
                true_prob=true_prob_estimate,
                fee_truth_info=fee_truth_info,
                token_id=token_id,
                min_order_size_used=min_order_size,
            )
            order_detail = order.to_dict()
        except Exception as exc:
            order_detail = {"error": str(exc)}

    candidate = {
        "session_id":              session_id,
        "market_id":               market_id,
        "token_id":                token_id,
        "side_label":              side_label,
        "ts_ms":                   ts_ms,
        "ts_utc":                  ts_utc,
        "seconds_to_close":        round(seconds_to_close, 3),
        "best_bid":                best_bid,
        "best_ask":                best_ask,
        "spread_pct":              round(spread_pct, 4),
        "ask_depth_usdc":          round(ask_depth_usdc, 4),
        "min_order_size":          min_order_size,
        "min_order_size_source":   min_order_size_source,
        "btc_delta_pct":           round(btc_delta_pct, 4) if btc_delta_pct is not None else None,
        "direction_signal":        direction_signal,
        "true_prob_estimate":      true_prob_estimate,
        "bankroll_remaining":      round(bankroll_remaining, 4),
        "position_usdc":           round(position_usdc, 4),
        "gates":                   gates,
        "reasons":                 reasons,
        # ── Tier classification (replaces all_gates_pass) ──────────────────
        "signal_candidate":        signal_candidate,
        "pricing_candidate":       pricing_candidate,
        "execution_candidate":     execution_candidate,
        "ev_candidate":            ev_candidate,    # True / False / None
        "ev_reason":               ev_reason,
        "net_ev_per_share":        round(net_ev, 6) if net_ev is not None else None,
        # Backwards compat: all_gates_pass is True only if exec+ev both True
        "all_gates_pass":          execution_candidate and (ev_candidate is True),
        "order_detail":            order_detail,
        # Settlement outcome fields (filled in post-settlement)
        "settled_price":           None,
        "hypothetical_pnl":        None,
        "would_have_won":          None,
        "settlement_source":       None,
    }

    return candidate


# ── CSV writer ────────────────────────────────────────────────────────────────

class TakerCandidateCsvWriter:
    FIELDS = [
        "session_id", "market_id", "token_id", "side_label",
        "ts_ms", "ts_utc", "seconds_to_close",
        "best_bid", "best_ask", "spread_pct", "ask_depth_usdc", "min_order_size",
        "btc_delta_pct", "direction_signal", "true_prob_estimate",
        "bankroll_remaining", "position_usdc",
        "gate_timing", "gate_spread", "gate_depth", "gate_min_size",
        "gate_signal", "gate_ev", "gate_bankroll",
        "all_gates_pass", "net_ev_per_share",
        "order_total_cost", "order_shares", "order_pnl_if_win", "order_pnl_if_loss",
        "settled_price", "hypothetical_pnl", "would_have_won",
    ]

    def __init__(self, session_id: str):
        self._path = config.SHADOWS_DIR / f"taker_candidates_{session_id}.csv"
        self._lock = threading.Lock()
        with open(self._path, "w", newline="") as f:
            csv.DictWriter(f, fieldnames=self.FIELDS).writeheader()

    def write(self, candidate: Dict):
        gates = candidate.get("gates", {})
        od = candidate.get("order_detail", {})
        row = {
            **{k: candidate.get(k, "") for k in self.FIELDS
               if k not in ("gate_timing", "gate_spread", "gate_depth",
                            "gate_min_size", "gate_signal", "gate_ev", "gate_bankroll",
                            "order_total_cost", "order_shares",
                            "order_pnl_if_win", "order_pnl_if_loss")},
            "gate_timing":        gates.get("timing", ""),
            "gate_spread":        gates.get("spread", ""),
            "gate_depth":         gates.get("depth", ""),
            "gate_min_size":      gates.get("min_size", ""),
            "gate_signal":        gates.get("signal", ""),
            "gate_ev":            gates.get("ev", ""),
            "gate_bankroll":      gates.get("bankroll", ""),
            "order_total_cost":   od.get("total_cost_usdc", ""),
            "order_shares":       od.get("shares", ""),
            "order_pnl_if_win":   od.get("pnl_if_win", ""),
            "order_pnl_if_loss":  od.get("pnl_if_loss", ""),
        }
        with self._lock:
            with open(self._path, "a", newline="") as f:
                csv.DictWriter(f, fieldnames=self.FIELDS).writerow(
                    {k: row.get(k, "") for k in self.FIELDS}
                )


# ── shadow evaluator ──────────────────────────────────────────────────────────

class TakerShadowEvaluator:
    """
    Periodically evaluate each market's current book snapshot against
    taker entry criteria. No live orders placed.

    Wired to:
      - book_recorder.MarketBookRecorder.recent  (ring buffer, live reference)
      - ref_recorder.MarketReferenceRecorder     (live reference, not a snapshot copy)
      - runtime_logger

    IMPORTANT: pass ref_recorder (the recorder object), not a snapshot list copy.
    The recorder's .snapshots list is appended to by its thread; we read it live.
    """

    def __init__(
        self,
        session_id: str,
        market: Dict,
        book_recorders: Dict,    # {"yes": MarketBookRecorder, "no": ...}
        ref_recorder,            # MarketReferenceRecorder instance (live reference)
                                 # NOT a copied list. Use None if unavailable.
        runtime_logger,
        shutdown_event: threading.Event,
        bankroll_ref: List[float],
        fee_results: Optional[Dict] = None,  # from fee_fetcher.session_fee_fetch()
    ):
        self.session_id    = session_id
        self.market_id     = market.get("market_id") or market.get("condition_id", "?")
        self.market        = market
        self.book_recs     = book_recorders
        self._ref_recorder = ref_recorder   # live recorder object; .snapshots is live
        self.runtime_log   = runtime_logger
        self.shutdown      = shutdown_event
        self.bankroll_ref  = bankroll_ref
        # fee_results: dict keyed by token_id -> fee_truth_info dict
        # If None (not yet wired), all order costs default to config_fallback labeling
        self._fee_results  = fee_results or {}

        # min_order_size: prefer market data, fall back to default with explicit log
        raw_min = market.get("min_order_size")
        if raw_min is not None:
            self.min_order_sz = float(raw_min)
            self.min_order_sz_source = "market_data"
        else:
            self.min_order_sz = config.DEFAULT_MIN_SHARES
            self.min_order_sz_source = "default"
            log.warning(
                "[taker-shadow] market=%s min_order_size not in market data – "
                "using DEFAULT_MIN_SHARES=%.1f",
                self.market_id[:12], config.DEFAULT_MIN_SHARES,
            )

        end_str = market.get("end_time_utc")
        self.close_ts: float = 0.0
        if end_str:
            try:
                self.close_ts = datetime.fromisoformat(end_str.replace("Z", "+00:00")).timestamp()
            except ValueError:
                pass

        self.candidates: List[Dict] = []
        self.passed_count: int = 0

        jsonl_path = config.SHADOWS_DIR / f"taker_candidates_{session_id}_{self.market_id[:12].replace('/', '_')}.jsonl"
        self._jsonl_path = jsonl_path
        self._csv = TakerCandidateCsvWriter(session_id)

    def _live_ref_snaps(self) -> List[Dict]:
        """
        Return the live list of reference snapshots from the recorder.
        This accesses the recorder's actual list object (not a copy), so it
        reflects snapshots added after the evaluator was initialized.
        Returns [] if no recorder attached.
        """
        if self._ref_recorder is None:
            return []
        return self._ref_recorder.snapshots  # live list, not a copy

    def _btc_delta_from_refs(self) -> Tuple[Optional[float], Optional[str]]:
        """
        Compute recent BTC price delta % from live reference snapshots.
        Returns (delta_pct, direction) where direction is "UP", "DOWN", or None.
        """
        snaps = self._live_ref_snaps()
        prices = [
            s["btc_price_usdt"]
            for s in snaps[-BTC_LOOKBACK_SNAPSHOTS:]
            if s.get("btc_price_usdt") is not None
        ]
        log.debug(
            "[taker-shadow] market=%s ref_snap_count=%d prices_available=%d",
            self.market_id[:12], len(snaps), len(prices),
        )
        if len(prices) < 2:
            return None, None

        oldest = prices[0]
        newest = prices[-1]
        if oldest == 0:
            return None, None

        delta_pct = (newest - oldest) / oldest * 100

        if abs(delta_pct) >= BTC_MOMENTUM_THRESHOLD_PCT:
            direction = "UP" if delta_pct > 0 else "DOWN"
        else:
            direction = None

        return round(delta_pct, 6), direction

    def _evaluate_side(
        self,
        side_label: str,
        book_rec,
        seconds_to_close: float,
        btc_delta_pct: Optional[float],
        direction_signal: Optional[str],
    ) -> Optional[Dict]:
        """Evaluate one side (YES or NO) and return a candidate dict if worth logging."""
        if not book_rec.recent:
            return None

        snap = book_rec.recent[-1]
        if not snap.get("fetch_ok"):
            return None

        best_bid       = snap.get("best_bid")
        best_ask       = snap.get("best_ask")
        spread_pct     = snap.get("spread_pct")
        ask_depth_usdc = snap.get("ask_depth_usdc")

        if None in (best_bid, best_ask, spread_pct, ask_depth_usdc):
            return None

        # Map direction signal to which side we'd hypothetically buy.
        # UP -> buy YES (price goes up = YES wins)
        # DOWN -> buy NO (price goes down = NO wins)
        # When direction_signal is None, we still log all candidates so that
        # the spread/depth/timing distribution is captured regardless of signal.
        # The signal gate will show False in those cases.
        if direction_signal == "UP" and side_label not in ("YES", "UP"):
            return None
        if direction_signal == "DOWN" and side_label not in ("NO", "DOWN"):
            return None

        # true_prob_estimate: we deliberately pass None here.
        # Do NOT pass market mid as true_prob – that makes ev_candidate
        # structurally always False (mid < ask < ask*(1+fee) always).
        # ev_candidate will be None (unknown) until an external model
        # provides a calibrated true probability.
        true_prob = None

        # Look up fee truth info for this specific token; fall back to global or None
        fee_truth_info = (
            self._fee_results.get(book_rec.token_id)
            or self._fee_results.get("_global")
            or None
        )

        candidate = _make_candidate(
            session_id=self.session_id,
            market_id=self.market_id,
            token_id=book_rec.token_id,
            side_label=side_label,
            ts_ms=snap.get("snapshot_ts_ms", int(time.time() * 1000)),
            seconds_to_close=seconds_to_close,
            best_bid=best_bid,
            best_ask=best_ask,
            spread_pct=spread_pct,
            ask_depth_usdc=ask_depth_usdc,
            min_order_size=self.min_order_sz,
            min_order_size_source=self.min_order_sz_source,
            btc_delta_pct=btc_delta_pct,
            direction_signal=direction_signal,
            true_prob_estimate=true_prob,
            bankroll_remaining=self.bankroll_ref[0],
            fee_truth_info=fee_truth_info,
        )

        return candidate

    def inspect_state(self) -> Dict:
        """
        Return a snapshot of current shared state for debugging.
        Call this from outside the thread to verify live wiring.
        """
        snaps = self._live_ref_snaps()
        book_yes = self.book_recs.get("yes")
        book_no  = self.book_recs.get("no")
        return {
            "market_id":           self.market_id[:12],
            "ref_snap_count":      len(snaps),
            "ref_last_label":      snaps[-1].get("snapshot_label") if snaps else None,
            "ref_last_btc":        snaps[-1].get("btc_price_usdt") if snaps else None,
            "book_yes_snap_count": len(book_yes.recent) if book_yes else 0,
            "book_no_snap_count":  len(book_no.recent) if book_no else 0,
            "bankroll":            self.bankroll_ref[0],
            "min_order_sz":        self.min_order_sz,
            "min_order_sz_source": self.min_order_sz_source,
            "candidates_total":    len(self.candidates),
            "signal_candidates":   sum(1 for c in self.candidates if c.get("signal_candidate")),
            "pricing_candidates":  sum(1 for c in self.candidates if c.get("pricing_candidate")),
            "execution_candidates": sum(1 for c in self.candidates if c.get("execution_candidate")),
            "ev_true":             sum(1 for c in self.candidates if c.get("ev_candidate") is True),
            "ev_false":            sum(1 for c in self.candidates if c.get("ev_candidate") is False),
            "ev_unknown":          sum(1 for c in self.candidates if c.get("ev_candidate") is None),
        }

    def _log_candidate(self, candidate: Dict):
        """Persist candidate to JSONL and CSV."""
        with open(self._jsonl_path, "a") as f:
            f.write(json.dumps(candidate) + "\n")
        self._csv.write(candidate)
        self.candidates.append(candidate)

        tier = (
            "EXEC" if candidate.get("execution_candidate") else
            "PRICE" if candidate.get("pricing_candidate") else
            "SIGNAL" if candidate.get("signal_candidate") else
            "NOISE"
        )
        ev_str = {True: "ev=YES", False: "ev=NO", None: "ev=UNKNOWN"}.get(
            candidate.get("ev_candidate"), "ev=UNKNOWN"
        )
        log.info(
            "[taker-shadow] %s %s %s stc=%.1fs ask=%.4f spread=%.2f%% depth=%.2f %s",
            tier, self.market_id[:10], candidate["side_label"],
            candidate["seconds_to_close"],
            candidate["best_ask"],
            candidate["spread_pct"],
            candidate.get("ask_depth_usdc", 0),
            ev_str,
        )

    def annotate_settlement(
        self,
        settled_yes_price: Optional[float],
        settlement_source: str = "unresolved",
    ):
        """
        Annotate candidates with settlement outcome.

        settled_yes_price: 1.0 if YES won, 0.0 if NO won, None if unknown.
        settlement_source: "market_resolved", "market_closed_no_outcome",
                           "market_closed_ambiguous", "unresolved", "pending"

        When settled_yes_price is None:
          - hypothetical_pnl remains None
          - would_have_won remains None
          - settlement_source is recorded

        Do NOT pass a last_trade_price as settled_yes_price.
        """
        for c in self.candidates:
            c["settlement_source"] = settlement_source

            if settled_yes_price is None:
                c["settled_price"]    = None
                c["hypothetical_pnl"] = None
                c["would_have_won"]   = None
                continue

            side = c["side_label"]
            od   = c.get("order_detail", {})
            if not od:
                c["settled_price"]    = settled_yes_price
                c["hypothetical_pnl"] = None
                c["would_have_won"]   = None
                continue

            # Determine which outcome applies to this side
            # YES/UP side wins when YES token settles at 1.0
            # NO/DOWN side wins when NO token settles at 1.0 (= YES at 0.0)
            if side in ("YES", "UP"):
                outcome = settled_yes_price
            else:
                outcome = 1.0 - settled_yes_price

            shares_net = od.get("shares_net") or od.get("shares") or 0
            cost       = od.get("total_cost_usdc") or 0
            pnl        = shares_net * outcome - cost

            c["settled_price"]    = settled_yes_price
            c["hypothetical_pnl"] = round(pnl, 6)
            c["would_have_won"]   = outcome == 1.0

        # Rewrite JSONL with annotations
        with open(self._jsonl_path, "w") as f:
            for c in self.candidates:
                f.write(json.dumps(c) + "\n")

        annotated_count = sum(1 for c in self.candidates if c.get("hypothetical_pnl") is not None)
        log.info(
            "[taker-shadow] market=%s settlement annotated: source=%s settled_yes=%s "
            "annotated=%d/%d",
            self.market_id[:10], settlement_source, settled_yes_price,
            annotated_count, len(self.candidates),
        )

    def run(self):
        """Main evaluation loop. Runs until market closes."""
        if self.close_ts == 0:
            log.error("[taker-shadow] market=%s missing close_ts – aborting", self.market_id[:12])
            return

        log.info("[taker-shadow] Starting for market=%s", self.market_id[:12])
        eval_interval = 1.0  # evaluate every second

        while not self.shutdown.is_set():
            stc = self.close_ts - time.time()
            if stc < -5:
                log.info("[taker-shadow] market=%s closed, stopping", self.market_id[:12])
                break

            btc_delta, direction = self._btc_delta_from_refs()

            for side_label, rec_key in [("YES", "yes"), ("NO", "no")]:
                book_rec = self.book_recs.get(rec_key)
                if book_rec is None:
                    continue
                candidate = self._evaluate_side(
                    side_label=side_label,
                    book_rec=book_rec,
                    seconds_to_close=stc,
                    btc_delta_pct=btc_delta,
                    direction_signal=direction,
                )
                if candidate is not None:
                    self._log_candidate(candidate)

            if self.shutdown.wait(timeout=eval_interval):
                break

        log.info("[taker-shadow] market=%s done: %d total, %d passed all gates",
                 self.market_id[:12], len(self.candidates), self.passed_count)


# ── session-level entry point ─────────────────────────────────────────────────

def run_taker_shadows(
    session_id: str,
    markets: List[Dict],
    book_recorders: Dict[str, Dict],
    ref_recorders_by_market: Dict[str, Any],  # market_id -> MarketReferenceRecorder
    runtime_logger,
    shutdown_event: threading.Event,
    bankroll_ref: List[float],
    fee_results: Optional[Dict] = None,       # from fee_fetcher.session_fee_fetch()
) -> Dict[str, "TakerShadowEvaluator"]:
    """
    Launch one taker shadow evaluator per market.

    ref_recorders_by_market must be a dict of live recorder objects
    (from reference_recorder.run_reference_recorders()), NOT a dict of
    snapshot list copies. The evaluators read .snapshots from the recorder
    in real time.

    fee_results is from fee_fetcher.session_fee_fetch(). If None, all order
    costs default to fee_rate_source="config_fallback", fee_truth_status="assumed".
    """
    evaluators: Dict[str, TakerShadowEvaluator] = {}

    for market in markets:
        mid = market.get("market_id") or market.get("condition_id", "?")
        book_recs  = book_recorders.get(mid, {})
        ref_rec    = ref_recorders_by_market.get(mid)   # live recorder object or None

        if ref_rec is None:
            log.warning(
                "[taker-shadow] market=%s: no ref_recorder provided – "
                "BTC delta signal will always be unavailable",
                mid[:12],
            )

        ev = TakerShadowEvaluator(
            session_id=session_id,
            market=market,
            book_recorders=book_recs,
            ref_recorder=ref_rec,
            runtime_logger=runtime_logger,
            shutdown_event=shutdown_event,
            bankroll_ref=bankroll_ref,
            fee_results=fee_results,
        )
        t = threading.Thread(target=ev.run, name=f"taker-shadow-{mid[:8]}", daemon=True)
        t.start()
        evaluators[mid] = ev
        log.info("[taker-shadow] Launched for market=%s", mid[:12])

    return evaluators
