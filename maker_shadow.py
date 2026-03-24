"""
maker_shadow.py - Shadow maker quote logger and adverse selection measurement.

Purpose:
  Log hypothetical maker quote opportunities.
  After a simulated "fill", track how the fair price moves.
  Compute fill-conditioned toxicity metrics (adverse selection).

  We do NOT assume maker edge. We measure whether it exists.

Core measurement:
  At each evaluation tick, if the spread is wide enough to quote at our
  target spread, we log a hypothetical resting order.

  A "shadow fill" is triggered when the book moves through our hypothetical
  quote price (i.e. best bid crosses our ask, or best ask crosses our bid).

  Post-fill, we track mid-price drift over MAKER_DRIFT_WINDOW_S seconds and
  classify: was the fill informed (price moved against us) or uninformed
  (price reverted)?

Toxicity metric:
  adverse_selection_rate = (fills where price moved against us) / total fills
  mean_drift_bps = mean mid-price drift (in basis points) from fill to window end
  If mean_drift_bps is negative (for our side), the market is adversely selecting.

Persistence:
  data/shadows/maker_quotes_<session_id>.jsonl     – quote opportunities
  data/shadows/maker_fills_<session_id>.jsonl      – shadow fill events
  data/shadows/maker_drift_<session_id>.jsonl      – drift measurements per fill
  data/shadows/maker_summary_<session_id>.csv      – aggregate toxicity summary

No live orders. Pure measurement.
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


# ── helpers ───────────────────────────────────────────────────────────────────

def _ts_ms() -> int:
    return int(time.time() * 1000)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _bps(price_from: float, price_to: float) -> float:
    """Basis-point change: (to - from) / from * 10000."""
    if price_from == 0:
        return 0.0
    return (price_to - price_from) / price_from * 10_000


# ── shadow quote record ───────────────────────────────────────────────────────

def _make_quote(
    session_id: str,
    market_id: str,
    token_id: str,
    side_label: str,
    ts_ms: int,
    seconds_to_close: float,
    current_bid: float,
    current_ask: float,
    current_mid: float,
    our_bid: float,
    our_ask: float,
    quote_spread_pct: float,
    top_bid_depth_usdc: float,
    top_ask_depth_usdc: float,
) -> Dict:
    """Record a hypothetical maker quote opportunity."""
    return {
        "session_id":         session_id,
        "market_id":          market_id,
        "token_id":           token_id,
        "side_label":         side_label,
        "ts_ms":              ts_ms,
        "ts_utc":             datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat(timespec="milliseconds"),
        "seconds_to_close":   round(seconds_to_close, 3),
        "market_bid":         current_bid,
        "market_ask":         current_ask,
        "market_mid":         round(current_mid, 6),
        "our_bid":            round(our_bid, 6),
        "our_ask":            round(our_ask, 6),
        "quote_spread_pct":   round(quote_spread_pct, 4),
        "market_spread_pct":  round((current_ask - current_bid) / current_mid * 100, 4) if current_mid > 0 else None,
        "top_bid_depth_usdc": top_bid_depth_usdc,
        "top_ask_depth_usdc": top_ask_depth_usdc,
        "quote_id":           f"{market_id[:8]}_{token_id[:8]}_{ts_ms}",
        # filled in if shadow fill occurs
        "filled_bid":         False,
        "filled_ask":         False,
        "fill_ts_ms":         None,
        "fill_price":         None,
        "drift_recorded":     False,
    }


# ── shadow fill record ────────────────────────────────────────────────────────

def _make_fill(
    quote: Dict,
    fill_side: str,     # "bid" or "ask"
    fill_ts_ms: int,
    fill_price: float,
    post_mid: float,
) -> Dict:
    """Record a shadow fill event."""
    direction = "ask_fill" if fill_side == "ask" else "bid_fill"
    return {
        "quote_id":       quote["quote_id"],
        "session_id":     quote["session_id"],
        "market_id":      quote["market_id"],
        "token_id":       quote["token_id"],
        "side_label":     quote["side_label"],
        "fill_ts_ms":     fill_ts_ms,
        "fill_utc":       datetime.fromtimestamp(fill_ts_ms / 1000, tz=timezone.utc).isoformat(timespec="milliseconds"),
        "fill_side":      fill_side,
        "fill_price":     fill_price,
        "mid_at_fill":    post_mid,
        "seconds_to_close_at_fill": quote["seconds_to_close"],
        # Drift window starts now
        "drift_start_mid": post_mid,
        "drift_end_mid":   None,
        "drift_bps":       None,
        "drift_window_s":  config.MAKER_DRIFT_WINDOW_S,
        "adverse":         None,   # True = adversely selected
        "drift_captured":  False,
    }


# ── CSV writers ───────────────────────────────────────────────────────────────

class MakerSummaryCsvWriter:
    FIELDS = [
        "session_id", "market_id", "side_label",
        "total_quotes", "total_fills", "bid_fills", "ask_fills",
        "adverse_fills", "uninformed_fills", "no_drift_yet",
        "adverse_selection_rate",
        "mean_drift_bps", "min_drift_bps", "max_drift_bps",
        "mean_seconds_to_close_at_fill",
    ]

    def __init__(self, session_id: str):
        self._path = config.SHADOWS_DIR / f"maker_summary_{session_id}.csv"
        with open(self._path, "w", newline="") as f:
            csv.DictWriter(f, fieldnames=self.FIELDS).writeheader()

    def write(self, row: Dict):
        with open(self._path, "a", newline="") as f:
            csv.DictWriter(f, fieldnames=self.FIELDS).writerow(
                {k: row.get(k, "") for k in self.FIELDS}
            )


# ── per-market maker shadow ───────────────────────────────────────────────────

class MakerShadowEvaluator:
    """
    Evaluates hypothetical maker opportunities for one token on one market.
    Tracks shadow fills and measures post-fill drift (adverse selection).
    """

    def __init__(
        self,
        session_id: str,
        market: Dict,
        token_id: str,
        side_label: str,
        book_rec,                   # MarketBookRecorder instance
        close_ts: float,
        runtime_logger,
        shutdown_event: threading.Event,
    ):
        self.session_id  = session_id
        self.market_id   = market.get("market_id") or market.get("condition_id", "?")
        self.token_id    = token_id
        self.side_label  = side_label
        self.book_rec    = book_rec
        self.close_ts    = close_ts
        self.runtime_log = runtime_logger
        self.shutdown    = shutdown_event

        mid_id = self.market_id[:12].replace("/", "_")
        self._quotes_path = config.SHADOWS_DIR / f"maker_quotes_{session_id}_{mid_id}_{side_label.lower()}.jsonl"
        self._fills_path  = config.SHADOWS_DIR / f"maker_fills_{session_id}_{mid_id}_{side_label.lower()}.jsonl"
        self._drift_path  = config.SHADOWS_DIR / f"maker_drift_{session_id}_{mid_id}_{side_label.lower()}.jsonl"

        self.quotes: List[Dict] = []
        self.fills:  List[Dict] = []
        self._active_quote: Optional[Dict] = None   # current resting hypothetical quote
        self._active_fill:  Optional[Dict] = None   # fill awaiting drift measurement

    def _seconds_to_close(self) -> float:
        return self.close_ts - time.time()

    def _compute_our_quotes(self, mid: float) -> Tuple[float, float, float]:
        """
        Compute our hypothetical bid and ask around mid.
        We target MAKER_QUOTE_SPREAD_TARGET / 2 on each side.
        Returns (our_bid, our_ask, spread_pct).
        """
        half_spread = config.MAKER_QUOTE_SPREAD_TARGET / 2
        our_bid = round(max(0.001, mid - half_spread), 6)
        our_ask = round(min(0.999, mid + half_spread), 6)
        spread_pct = round((our_ask - our_bid) / mid * 100, 4) if mid > 0 else 0.0
        return our_bid, our_ask, spread_pct

    def _can_quote(self, snap: Dict) -> bool:
        """Check conditions for placing a hypothetical maker quote."""
        if not snap.get("fetch_ok"):
            return False
        mid = snap.get("mid")
        bid = snap.get("best_bid")
        ask = snap.get("best_ask")
        bid_depth = snap.get("bid_depth_usdc", 0) or 0
        ask_depth = snap.get("ask_depth_usdc", 0) or 0
        if None in (mid, bid, ask):
            return False
        if bid_depth < config.MAKER_MIN_DEPTH_USDC or ask_depth < config.MAKER_MIN_DEPTH_USDC:
            return False
        return True

    def _check_shadow_fill(self, snap: Dict) -> Optional[str]:
        """
        Check if the current book has moved through our active quote.
        Returns "bid" or "ask" if filled, None otherwise.
        """
        if self._active_quote is None:
            return None

        our_bid = self._active_quote["our_bid"]
        our_ask = self._active_quote["our_ask"]
        mkt_bid = snap.get("best_bid")
        mkt_ask = snap.get("best_ask")

        if mkt_bid is None or mkt_ask is None:
            return None

        # Our ask got hit: market bid crossed up through our ask
        if mkt_bid >= our_ask:
            return "ask"

        # Our bid got hit: market ask crossed down through our bid
        if mkt_ask <= our_bid:
            return "bid"

        return None

    def _capture_drift(self, fill: Dict, current_mid: float):
        """Record drift measurement for a shadow fill."""
        fill["drift_end_mid"]  = current_mid
        fill["drift_bps"]      = round(_bps(fill["drift_start_mid"], current_mid), 4)
        fill["drift_captured"] = True

        # Adverse if price moved against our fill side:
        # ask fill (we sold): adverse if mid went up (buyer was informed)
        # bid fill (we bought): adverse if mid went down (seller was informed)
        if fill["fill_side"] == "ask":
            fill["adverse"] = fill["drift_bps"] > 0
        else:
            fill["adverse"] = fill["drift_bps"] < 0

        with open(self._drift_path, "a") as f:
            f.write(json.dumps(fill) + "\n")

        log.info(
            "[maker-shadow] DRIFT market=%s %s fill=%s drift=%.1fbps adverse=%s",
            self.market_id[:10], self.side_label,
            fill["fill_side"], fill["drift_bps"], fill["adverse"],
        )

    def _log_quote(self, quote: Dict):
        with open(self._quotes_path, "a") as f:
            f.write(json.dumps(quote) + "\n")
        self.quotes.append(quote)

    def _log_fill(self, fill: Dict):
        with open(self._fills_path, "a") as f:
            f.write(json.dumps(fill) + "\n")
        self.fills.append(fill)

    def summary(self) -> Dict:
        """Aggregate toxicity metrics across all shadow fills."""
        total_fills  = len(self.fills)
        bid_fills    = sum(1 for f in self.fills if f["fill_side"] == "bid")
        ask_fills    = sum(1 for f in self.fills if f["fill_side"] == "ask")
        adverse      = [f for f in self.fills if f.get("drift_captured") and f.get("adverse")]
        uninformed   = [f for f in self.fills if f.get("drift_captured") and not f.get("adverse")]
        no_drift     = [f for f in self.fills if not f.get("drift_captured")]
        drift_bps_vals = [f["drift_bps"] for f in self.fills if f.get("drift_bps") is not None]

        adverse_rate = len(adverse) / total_fills if total_fills > 0 else None
        mean_drift   = sum(drift_bps_vals) / len(drift_bps_vals) if drift_bps_vals else None
        stc_at_fills = [f.get("seconds_to_close_at_fill") for f in self.fills if f.get("seconds_to_close_at_fill") is not None]

        return {
            "session_id":               self.session_id,
            "market_id":                self.market_id,
            "side_label":               self.side_label,
            "total_quotes":             len(self.quotes),
            "total_fills":              total_fills,
            "bid_fills":                bid_fills,
            "ask_fills":                ask_fills,
            "adverse_fills":            len(adverse),
            "uninformed_fills":         len(uninformed),
            "no_drift_yet":             len(no_drift),
            "adverse_selection_rate":   round(adverse_rate, 4) if adverse_rate is not None else None,
            "mean_drift_bps":           round(mean_drift, 2) if mean_drift is not None else None,
            "min_drift_bps":            round(min(drift_bps_vals), 2) if drift_bps_vals else None,
            "max_drift_bps":            round(max(drift_bps_vals), 2) if drift_bps_vals else None,
            "mean_seconds_to_close_at_fill": round(sum(stc_at_fills)/len(stc_at_fills), 2) if stc_at_fills else None,
        }

    def run(self):
        """Main evaluation loop."""
        log.info("[maker-shadow] Starting for market=%s %s", self.market_id[:12], self.side_label)
        eval_interval = 0.5   # check every 500ms

        while not self.shutdown.is_set():
            stc = self._seconds_to_close()
            if stc < -5:
                log.info("[maker-shadow] market=%s %s closed", self.market_id[:12], self.side_label)
                break

            if not self.book_rec.recent:
                if self.shutdown.wait(timeout=eval_interval):
                    break
                continue

            snap = self.book_rec.recent[-1]
            mid  = snap.get("mid")

            # ── drift capture for pending fill ────────────────────────────────
            if self._active_fill and not self._active_fill["drift_captured"]:
                fill_ts = self._active_fill["fill_ts_ms"]
                drift_deadline = fill_ts + config.MAKER_DRIFT_WINDOW_S * 1000
                if _ts_ms() >= drift_deadline:
                    if mid is not None:
                        self._capture_drift(self._active_fill, mid)
                    self._active_fill = None

            # ── check for shadow fill on active quote ─────────────────────────
            if self._active_quote and self._active_fill is None:
                fill_side = self._check_shadow_fill(snap)
                if fill_side:
                    fill_price = (self._active_quote["our_ask"]
                                  if fill_side == "ask"
                                  else self._active_quote["our_bid"])
                    fill = _make_fill(
                        quote=self._active_quote,
                        fill_side=fill_side,
                        fill_ts_ms=_ts_ms(),
                        fill_price=fill_price,
                        post_mid=mid or fill_price,
                    )
                    self._log_fill(fill)
                    self._active_fill = fill
                    self._active_quote = None
                    log.info(
                        "[maker-shadow] FILL market=%s %s side=%s price=%.4f mid=%.4f",
                        self.market_id[:10], self.side_label, fill_side, fill_price, mid or 0,
                    )
                    continue

            # ── post new quote if no active quote ─────────────────────────────
            if self._active_quote is None and self._active_fill is None:
                if self._can_quote(snap) and mid is not None:
                    our_bid, our_ask, spread_pct = self._compute_our_quotes(mid)
                    quote = _make_quote(
                        session_id=self.session_id,
                        market_id=self.market_id,
                        token_id=self.token_id,
                        side_label=self.side_label,
                        ts_ms=snap.get("snapshot_ts_ms", _ts_ms()),
                        seconds_to_close=stc,
                        current_bid=snap.get("best_bid", 0),
                        current_ask=snap.get("best_ask", 0),
                        current_mid=mid,
                        our_bid=our_bid,
                        our_ask=our_ask,
                        quote_spread_pct=spread_pct,
                        top_bid_depth_usdc=snap.get("bid_depth_usdc", 0) or 0,
                        top_ask_depth_usdc=snap.get("ask_depth_usdc", 0) or 0,
                    )
                    self._log_quote(quote)
                    self._active_quote = quote
                    log.debug(
                        "[maker-shadow] QUOTE market=%s %s bid=%.4f ask=%.4f spread=%.2f%%",
                        self.market_id[:10], self.side_label, our_bid, our_ask, spread_pct,
                    )

            if self.shutdown.wait(timeout=eval_interval):
                break

        log.info("[maker-shadow] market=%s %s done: %d quotes, %d fills",
                 self.market_id[:12], self.side_label,
                 len(self.quotes), len(self.fills))


# ── session-level entry point ─────────────────────────────────────────────────

def run_maker_shadows(
    session_id: str,
    markets: List[Dict],
    book_recorders: Dict[str, Dict],
    runtime_logger,
    shutdown_event: threading.Event,
    csv_writer: Optional[MakerSummaryCsvWriter] = None,
) -> Dict[str, List[MakerShadowEvaluator]]:
    """Launch maker shadow evaluators for each market and side."""
    if csv_writer is None:
        csv_writer = MakerSummaryCsvWriter(session_id)

    result: Dict[str, List[MakerShadowEvaluator]] = {}

    for market in markets:
        mid       = market.get("market_id") or market.get("condition_id", "?")
        yes_tok   = market.get("yes_token_id")
        no_tok    = market.get("no_token_id")
        end_str   = market.get("end_time_utc")
        close_ts  = 0.0

        if end_str:
            try:
                close_ts = datetime.fromisoformat(end_str.replace("Z", "+00:00")).timestamp()
            except ValueError:
                pass

        if close_ts == 0.0:
            log.error("[maker-shadow] market=%s no close_ts, skipping", mid[:12])
            continue

        book_recs = book_recorders.get(mid, {})
        evaluators_for_market: List[MakerShadowEvaluator] = []

        for token_id, side_label, rec_key in [(yes_tok, "YES", "yes"), (no_tok, "NO", "no")]:
            if not token_id:
                continue
            book_rec = book_recs.get(rec_key)
            if book_rec is None:
                log.warning("[maker-shadow] No book recorder for %s %s", mid[:12], side_label)
                continue

            ev = MakerShadowEvaluator(
                session_id=session_id,
                market=market,
                token_id=token_id,
                side_label=side_label,
                book_rec=book_rec,
                close_ts=close_ts,
                runtime_logger=runtime_logger,
                shutdown_event=shutdown_event,
            )
            t = threading.Thread(
                target=ev.run,
                name=f"maker-shadow-{side_label.lower()}-{mid[:8]}",
                daemon=True,
            )
            t.start()
            evaluators_for_market.append(ev)
            log.info("[maker-shadow] Launched for market=%s %s", mid[:12], side_label)

        result[mid] = evaluators_for_market

    return result


def collect_maker_summaries(
    evaluators: Dict[str, List["MakerShadowEvaluator"]],
    csv_writer: MakerSummaryCsvWriter,
) -> List[Dict]:
    """Collect and persist summary from all maker shadow evaluators."""
    summaries = []
    for mid, evlist in evaluators.items():
        for ev in evlist:
            s = ev.summary()
            summaries.append(s)
            csv_writer.write(s)
    return summaries
