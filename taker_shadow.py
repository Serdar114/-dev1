"""
taker_shadow.py - Shadow evaluator for ultra-selective late-window taker entries.

Purpose:
  Evaluate hypothetical taker entries against strict criteria WITHOUT placing
  any live orders. Log every candidate and the reason it passed or failed.
  Use real fee math throughout.

Evaluation criteria (ALL must pass):
  1. seconds_to_close in [TAKER_MIN_SECONDS_TO_CLOSE, TAKER_MAX_SECONDS_TO_CLOSE]
  2. spread_pct < TAKER_MAX_SPREAD_PCT
  3. top-of-book ask depth >= MIN_TAKER_ASK_DEPTH_USDC (liquidity check)
  4. min_order_size constraint passes under MAX_POSITION_USDC
  5. reference price delta directional signal available
  6. net EV after fee exceeds TAKER_MIN_EDGE_AFTER_FEE (fee-adjusted)
  7. bankroll remaining > total_cost_usdc

Signal logic:
  We use a VERY simple directional heuristic: if BTC has moved > threshold %
  in the last N seconds (based on reference snapshots), assume directional
  momentum. This is NOT a strategy claim. It's a measurement of whether
  a clear directional condition even exists in these markets.
  We log both "would have been correct" and "would have been wrong" after settlement.

Persistence:
  data/shadows/taker_candidates_<session_id>.jsonl   – every evaluated candidate
  data/shadows/taker_candidates_<session_id>.csv     – flattened

No live orders. No actual edge claims.
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
    side_label: str,       # "YES" or "NO"
    ts_ms: int,
    seconds_to_close: float,
    best_bid: float,
    best_ask: float,
    spread_pct: float,
    ask_depth_usdc: float,
    min_order_size: float,
    btc_delta_pct: Optional[float],
    direction_signal: Optional[str],  # "UP", "DOWN", or None
    true_prob_estimate: Optional[float],
    bankroll_remaining: float,
) -> Dict:
    """Build and evaluate a full taker candidate record."""

    ts_utc = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat(timespec="milliseconds")
    position_usdc = min(config.MAX_POSITION_USDC, bankroll_remaining)

    # ── gate checks ───────────────────────────────────────────────────────────
    gates: Dict[str, bool] = {}
    reasons: Dict[str, str] = {}

    # Gate 1: timing window
    in_window = config.TAKER_MIN_SECONDS_TO_CLOSE <= seconds_to_close <= config.TAKER_MAX_SECONDS_TO_CLOSE
    gates["timing"] = in_window
    reasons["timing"] = (
        f"stc={seconds_to_close:.1f}s in [{config.TAKER_MIN_SECONDS_TO_CLOSE},{config.TAKER_MAX_SECONDS_TO_CLOSE}]"
        if in_window else
        f"stc={seconds_to_close:.1f}s OUTSIDE window"
    )

    # Gate 2: spread
    tight_spread = spread_pct < config.TAKER_MAX_SPREAD_PCT * 100  # config is fraction, compare to %
    gates["spread"] = tight_spread
    reasons["spread"] = (
        f"spread={spread_pct:.2f}% < {config.TAKER_MAX_SPREAD_PCT*100:.1f}%"
        if tight_spread else
        f"spread={spread_pct:.2f}% TOO WIDE"
    )

    # Gate 3: ask depth
    deep_enough = ask_depth_usdc >= MIN_TAKER_ASK_DEPTH_USDC
    gates["depth"] = deep_enough
    reasons["depth"] = (
        f"ask_depth={ask_depth_usdc:.2f} USDC ok"
        if deep_enough else
        f"ask_depth={ask_depth_usdc:.2f} USDC < {MIN_TAKER_ASK_DEPTH_USDC}"
    )

    # Gate 4: min order size under budget
    desired_shares = fee_math.max_shares_from_usdc(position_usdc, best_ask)
    size_check = fee_math.check_min_size(
        shares=desired_shares,
        min_shares=min_order_size,
        ask_price=best_ask,
        usdc_budget=position_usdc,
    )
    gates["min_size"] = size_check["viable"]
    reasons["min_size"] = size_check["reason"]

    # Gate 5: directional signal
    has_signal = direction_signal is not None
    gates["signal"] = has_signal
    reasons["signal"] = (
        f"signal={direction_signal} btc_delta={btc_delta_pct:.3f}%"
        if has_signal else
        "no directional signal"
    )

    # Gate 6: fee-adjusted EV
    ev_ok = False
    net_ev = None
    if true_prob_estimate is not None and gates.get("min_size", False):
        order = fee_math.compute_taker_buy(
            price_per_share=best_ask,
            shares=size_check["shares_floored"],
            true_prob=true_prob_estimate,
        )
        net_ev = order.edge_at_true_prob  # edge per share
        ev_ok = (net_ev is not None and net_ev > config.TAKER_MIN_EDGE_AFTER_FEE)
        gates["ev"] = ev_ok
        reasons["ev"] = (
            f"net_ev_per_share={net_ev:.4f} > {config.TAKER_MIN_EDGE_AFTER_FEE}"
            if ev_ok else
            f"net_ev_per_share={net_ev:.4f} INSUFFICIENT"
        )
    else:
        gates["ev"] = False
        reasons["ev"] = "no true_prob_estimate or min_size failed"

    # Gate 7: bankroll
    total_cost = size_check.get("cost_usdc", 9999)
    bank_ok = total_cost <= bankroll_remaining
    gates["bankroll"] = bank_ok
    reasons["bankroll"] = (
        f"cost={total_cost:.4f} <= bankroll={bankroll_remaining:.4f}"
        if bank_ok else
        f"cost={total_cost:.4f} > bankroll={bankroll_remaining:.4f}"
    )

    all_pass = all(gates.values())

    # ── fee math for the hypothetical order ───────────────────────────────────
    order_detail: Dict = {}
    if size_check["viable"] and bankroll_remaining > 0:
        try:
            order = fee_math.compute_taker_buy(
                price_per_share=best_ask,
                shares=size_check["shares_floored"],
                true_prob=true_prob_estimate,
            )
            order_detail = order.to_dict()
        except Exception as exc:
            order_detail = {"error": str(exc)}

    candidate = {
        "session_id":          session_id,
        "market_id":           market_id,
        "token_id":            token_id,
        "side_label":          side_label,
        "ts_ms":               ts_ms,
        "ts_utc":              ts_utc,
        "seconds_to_close":    round(seconds_to_close, 3),
        "best_bid":            best_bid,
        "best_ask":            best_ask,
        "spread_pct":          round(spread_pct, 4),
        "ask_depth_usdc":      round(ask_depth_usdc, 4),
        "min_order_size":      min_order_size,
        "btc_delta_pct":       round(btc_delta_pct, 4) if btc_delta_pct is not None else None,
        "direction_signal":    direction_signal,
        "true_prob_estimate":  true_prob_estimate,
        "bankroll_remaining":  round(bankroll_remaining, 4),
        "position_usdc":       round(position_usdc, 4),
        "gates":               gates,
        "reasons":             reasons,
        "all_gates_pass":      all_pass,
        "net_ev_per_share":    round(net_ev, 6) if net_ev is not None else None,
        "order_detail":        order_detail,
        # Settlement outcome fields (filled in post-settlement)
        "settled_price":       None,
        "hypothetical_pnl":   None,
        "would_have_won":      None,
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
      - book_recorder.MarketBookRecorder.recent  (ring buffer)
      - reference snapshots (dict of label -> btc_price)
      - runtime_logger

    Call run() in a dedicated thread per market.
    """

    def __init__(
        self,
        session_id: str,
        market: Dict,
        book_recorders: Dict,     # {"yes": MarketBookRecorder, "no": ...}
        ref_snapshots: List[Dict], # reference snapshots (populated in real-time)
        runtime_logger,
        shutdown_event: threading.Event,
        bankroll_ref: List[float], # mutable single-element list for bankroll
    ):
        self.session_id    = session_id
        self.market_id     = market.get("market_id") or market.get("condition_id", "?")
        self.market        = market
        self.book_recs     = book_recorders
        self.ref_snaps     = ref_snapshots
        self.runtime_log   = runtime_logger
        self.shutdown      = shutdown_event
        self.bankroll_ref  = bankroll_ref
        self.min_order_sz  = float(market.get("min_order_size") or config.DEFAULT_MIN_SHARES)

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

    def _btc_delta_from_refs(self) -> Tuple[Optional[float], Optional[str]]:
        """
        Compute recent BTC price delta % from reference snapshots.
        Returns (delta_pct, direction) where direction is "UP", "DOWN", or None.
        """
        prices = [
            s["btc_price_usdt"]
            for s in self.ref_snaps[-BTC_LOOKBACK_SNAPSHOTS:]
            if s.get("btc_price_usdt") is not None
        ]
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

        # Map direction signal to which side we'd hypothetically buy
        # UP signal -> buy YES (expect YES=1 at settlement)
        # DOWN signal -> buy NO (expect NO=1 at settlement)
        if direction_signal == "UP" and side_label != "YES":
            return None
        if direction_signal == "DOWN" and side_label != "NO":
            return None

        # True probability estimate = mid-price (naive, no model)
        # We treat the mid as the market's implied probability.
        # For shadow purposes we set true_prob = mid (zero claimed edge beyond signal).
        true_prob = snap.get("mid")

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
            btc_delta_pct=btc_delta_pct,
            direction_signal=direction_signal,
            true_prob_estimate=true_prob,
            bankroll_remaining=self.bankroll_ref[0],
        )

        return candidate

    def _log_candidate(self, candidate: Dict):
        """Persist candidate to JSONL and CSV."""
        with open(self._jsonl_path, "a") as f:
            f.write(json.dumps(candidate) + "\n")
        self._csv.write(candidate)
        self.candidates.append(candidate)
        if candidate["all_gates_pass"]:
            self.passed_count += 1
            log.info(
                "[taker-shadow] PASS market=%s %s stc=%.1fs ask=%.4f spread=%.2f%% "
                "ev=%.4f cost=%.2f USDC",
                self.market_id[:10], candidate["side_label"],
                candidate["seconds_to_close"],
                candidate["best_ask"],
                candidate["spread_pct"],
                candidate.get("net_ev_per_share") or 0,
                candidate.get("order_detail", {}).get("total_cost_usdc") or 0,
            )
        else:
            failed = [k for k, v in candidate["gates"].items() if not v]
            log.debug("[taker-shadow] FAIL %s %s stc=%.1fs gates_failed=%s",
                      self.market_id[:10], candidate["side_label"],
                      candidate["seconds_to_close"], failed)

    def annotate_settlement(self, settled_yes_price: float):
        """
        After settlement, go back through candidates and annotate hypothetical PnL.
        settled_yes_price: 1.0 if YES won, 0.0 if NO won.
        """
        for c in self.candidates:
            side = c["side_label"]
            od   = c.get("order_detail", {})
            if not od:
                continue

            shares = od.get("shares") or 0
            cost   = od.get("total_cost_usdc") or 0

            outcome = settled_yes_price if side == "YES" else (1.0 - settled_yes_price)
            pnl = shares * outcome - cost

            c["settled_price"]     = settled_yes_price
            c["hypothetical_pnl"]  = round(pnl, 6)
            c["would_have_won"]    = outcome == 1.0

        # Rewrite JSONL with annotations
        with open(self._jsonl_path, "w") as f:
            for c in self.candidates:
                f.write(json.dumps(c) + "\n")
        log.info("[taker-shadow] market=%s annotated %d candidates with settlement=%.1f",
                 self.market_id[:10], len(self.candidates), settled_yes_price)

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
    ref_snapshots_by_market: Dict[str, List[Dict]],
    runtime_logger,
    shutdown_event: threading.Event,
    bankroll_ref: List[float],
) -> Dict[str, TakerShadowEvaluator]:
    """Launch one taker shadow evaluator per market."""
    evaluators: Dict[str, TakerShadowEvaluator] = {}

    for market in markets:
        mid = market.get("market_id") or market.get("condition_id", "?")
        book_recs = book_recorders.get(mid, {})
        ref_snaps = ref_snapshots_by_market.get(mid, [])

        ev = TakerShadowEvaluator(
            session_id=session_id,
            market=market,
            book_recorders=book_recs,
            ref_snapshots=ref_snaps,
            runtime_logger=runtime_logger,
            shutdown_event=shutdown_event,
            bankroll_ref=bankroll_ref,
        )
        t = threading.Thread(target=ev.run, name=f"taker-shadow-{mid[:8]}", daemon=True)
        t.start()
        evaluators[mid] = ev
        log.info("[taker-shadow] Launched for market=%s", mid[:12])

    return evaluators
