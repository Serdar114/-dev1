"""
main.py — Polymarket BTC up/down feasibility logger.

Observation loop: 2-second ticks, dual lane (5m + 15m families).
Each tick produces one JoinedObservation row per locked family.

PATCH additions: dual reference enrichment, sigma/vol layer, edge layer.
No trading, no alarms, no signal logic — data collection only.
"""
from __future__ import annotations

import argparse
import logging
import math
import signal
import threading
import time
from typing import Dict, Optional

import requests

# ── patch: scipy is optional (fallback approximation is provided) ─────────────
try:
    from scipy.stats import norm as _scipy_norm
    _HAS_SCIPY = True
except ImportError:  # pragma: no cover
    _HAS_SCIPY = False

from schemas import DualReferenceSnapshot, JoinedObservation, MarketPair
from logger import Logger
from gamma_api import select_markets_by_family
from clob_ws import CLOBWSClient
from clob_rest import get_book
from rtds_client import RTDSClient
from vol_engine import VolEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("main")

_OBSERVATION_INTERVAL_S = 2.0
_DISCOVERY_INTERVAL_S = 30.0
_HEARTBEAT_INTERVAL_S = 30.0
_SECONDS_PER_YEAR = 31_536_000.0    # 365 × 24 × 3600


# ── math helpers ──────────────────────────────────────────────────────────────

def _norm_cdf(x: float) -> float:
    """Standard normal CDF. Uses scipy when available, A&S approximation otherwise."""
    if _HAS_SCIPY:
        return float(_scipy_norm.cdf(x))
    # Abramowitz & Stegun 26.2.17 (max error 7.5e-8)
    t = 1.0 / (1.0 + 0.2316419 * abs(x))
    poly = t * (0.319381530 + t * (-0.356563782 + t * (1.781477937 + t * (-1.821255978 + t * 1.330274429))))
    cdf = 1.0 - (1.0 / math.sqrt(2.0 * math.pi)) * math.exp(-0.5 * x * x) * poly
    return cdf if x >= 0.0 else 1.0 - cdf


# ── observation builder ───────────────────────────────────────────────────────

def _build_observation(
    family: str,
    pair: MarketPair,
    snap: DualReferenceSnapshot,
    vol_engine: VolEngine,
    clob_ws: CLOBWSClient,
    rest_session: requests.Session,
    now_ms: int,
) -> JoinedObservation:
    """
    Build one enriched JoinedObservation for the given family/pair.
    Every field that cannot be computed is left None — no defaults, no dummies.
    """
    # ── book state (WS first, REST fallback) ──────────────────────────────────
    up_book = clob_ws.get_snapshot(pair.up_token_id)
    down_book = clob_ws.get_snapshot(pair.down_token_id)
    if up_book is None:
        up_book = get_book(pair.up_token_id, session=rest_session)
    if down_book is None:
        down_book = get_book(pair.down_token_id, session=rest_session)

    seconds_to_expiry = (pair.end_time_ms - now_ms) / 1000.0 if pair.end_time_ms else None

    obs = JoinedObservation(
        ts_local=now_ms,
        family=family,
        condition_id=pair.condition_id,
        slug=pair.slug,
        end_time_ms=pair.end_time_ms,
        seconds_to_expiry=seconds_to_expiry,
        up_token_id=pair.up_token_id,
        up_best_bid=up_book.best_bid if up_book else None,
        up_best_ask=up_book.best_ask if up_book else None,
        up_mid=up_book.mid if up_book else None,
        up_spread=up_book.spread if up_book else None,
        up_book_source=up_book.source if up_book else None,
        down_token_id=pair.down_token_id,
        down_best_bid=down_book.best_bid if down_book else None,
        down_best_ask=down_book.best_ask if down_book else None,
        down_mid=down_book.mid if down_book else None,
        down_spread=down_book.spread if down_book else None,
        down_book_source=down_book.source if down_book else None,
    )

    # ── PATCH: dual reference enrichment ──────────────────────────────────────
    obs.external_btc_price = snap.binance_price            # legacy alias
    obs.external_btc_price_binance = snap.binance_price
    obs.external_btc_price_chainlink = snap.chainlink_price
    obs.external_basis_bps = snap.basis_bps
    obs.external_lag_ms = snap.lag_ms
    obs.stale_binance = snap.binance_stale
    obs.stale_chainlink = snap.chainlink_stale

    # ── PATCH: market metadata ────────────────────────────────────────────────
    obs.barrier_price = pair.start_price
    obs.seconds_to_resolution = seconds_to_expiry

    # ── PATCH: realized vol / sigma distance ──────────────────────────────────
    realized_vol = vol_engine.get_annualized_vol()
    obs.realized_vol_60m = realized_vol

    chainlink_price = snap.chainlink_price
    barrier = pair.start_price
    sto_res = obs.seconds_to_resolution

    sigma_distance: Optional[float] = None
    if (
        chainlink_price is not None
        and barrier is not None
        and realized_vol is not None
        and sto_res is not None
        and sto_res > 0.0
    ):
        period_sigma = realized_vol * math.sqrt(sto_res / _SECONDS_PER_YEAR)
        if period_sigma > 0.0:
            expected_move = chainlink_price * period_sigma
            sigma_distance = (chainlink_price - barrier) / expected_move
    obs.sigma_distance = sigma_distance

    fair_up_prob: Optional[float] = None
    if sigma_distance is not None:
        fair_up_prob = _norm_cdf(sigma_distance)
    obs.fair_up_prob = fair_up_prob

    # ── PATCH: edge layer ─────────────────────────────────────────────────────
    up_ask = obs.up_best_ask
    down_ask = obs.down_best_ask

    edge_up_raw: Optional[float] = None
    edge_down_raw: Optional[float] = None
    if fair_up_prob is not None and up_ask is not None:
        edge_up_raw = fair_up_prob - up_ask
    if fair_up_prob is not None and down_ask is not None:
        edge_down_raw = (1.0 - fair_up_prob) - down_ask
    obs.edge_up_raw = edge_up_raw
    obs.edge_down_raw = edge_down_raw

    # Effective fee estimate: C × feeRate × p × (1-p)
    # p = best ask price of whichever side is available (Up preferred)
    fee_estimate: Optional[float] = None
    fee_p = up_ask if up_ask is not None else down_ask
    if pair.fee_c is not None and pair.fee_rate is not None and fee_p is not None:
        fee_estimate = pair.fee_c * pair.fee_rate * fee_p * (1.0 - fee_p)
    obs.effective_fee_estimate = fee_estimate

    obs.edge_up_net = (
        (edge_up_raw - fee_estimate)
        if (edge_up_raw is not None and fee_estimate is not None)
        else None
    )
    obs.edge_down_net = (
        (edge_down_raw - fee_estimate)
        if (edge_down_raw is not None and fee_estimate is not None)
        else None
    )

    return obs


# ── heartbeat thread ──────────────────────────────────────────────────────────

def _heartbeat_loop(
    shutdown: threading.Event,
    logger: Logger,
    rtds: RTDSClient,
    vol_engine: VolEngine,
    family_pairs: Dict[str, MarketPair],
    interval_s: float,
) -> None:
    while not shutdown.is_set():
        shutdown.wait(interval_s)
        if shutdown.is_set():
            break
        try:
            snap = rtds.get_snapshot()
            logger.log_heartbeat(
                binance_price=snap.binance_price,
                binance_stale=snap.binance_stale,
                chainlink_price=snap.chainlink_price,
                chainlink_stale=snap.chainlink_stale,
                vol_bars=vol_engine.bar_count(),
                vol_ready=vol_engine.bar_count() >= 60,
                locked_families=list(family_pairs.keys()),
                locked_slugs={f: p.slug for f, p in family_pairs.items()},
            )
        except Exception as exc:
            log.warning("heartbeat error: %s", exc)


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Polymarket BTC up/down feasibility logger — observation enrichment patch"
    )
    parser.add_argument(
        "--duration", type=int, default=14_400,
        help="Session duration in seconds (default: 14400 = 4 h)",
    )
    parser.add_argument("--log-dir", default="logs", help="Directory for JSONL log files")
    args = parser.parse_args()

    shutdown = threading.Event()

    def _sig_handler(sig, _frame):
        log.info("Signal %s received — initiating clean shutdown", sig)
        shutdown.set()

    signal.signal(signal.SIGINT, _sig_handler)
    signal.signal(signal.SIGTERM, _sig_handler)

    logger = Logger(log_dir=args.log_dir)
    logger.log_system_event(
        "startup",
        duration_s=args.duration,
        log_dir=args.log_dir,
        scipy_available=_HAS_SCIPY,
    )
    log.info("Session started — duration=%ds log_dir=%s scipy=%s",
             args.duration, args.log_dir, _HAS_SCIPY)

    # ── initialise subsystems ─────────────────────────────────────────────────
    rtds = RTDSClient(shutdown_event=shutdown)
    clob_ws = CLOBWSClient(shutdown_event=shutdown)
    vol_engine = VolEngine()
    rest_session = requests.Session()

    rtds.start()
    clob_ws.start()

    # ── initial market discovery ──────────────────────────────────────────────
    family_pairs: Dict[str, MarketPair] = {}
    try:
        family_pairs = select_markets_by_family(session=rest_session)
        for family, pair in family_pairs.items():
            logger.log_market_discovery(family, [pair.slug])
    except Exception as exc:
        log.error("Initial discovery failed: %s", exc)
        logger.log_system_event("discovery_failed", error=str(exc))

    if not family_pairs:
        log.warning("No markets found at startup — will retry during observation loop")

    # Subscribe CLOB WS to all locked token IDs
    _refresh_clob_subscriptions(clob_ws, family_pairs)

    # ── heartbeat thread ──────────────────────────────────────────────────────
    hb_thread = threading.Thread(
        target=_heartbeat_loop,
        args=(shutdown, logger, rtds, vol_engine, family_pairs, _HEARTBEAT_INTERVAL_S),
        name="heartbeat",
        daemon=True,
    )
    hb_thread.start()

    # ── main observation loop ─────────────────────────────────────────────────
    session_end = time.time() + args.duration
    last_discovery_ts = time.time()
    next_obs_ts = time.time()

    log.info("Entering observation loop")

    while not shutdown.is_set():
        now = time.time()
        now_ms = int(now * 1000)

        if now >= session_end:
            log.info("Session duration elapsed — clean shutdown")
            shutdown.set()
            break

        # ── periodic rediscovery ──────────────────────────────────────────────
        if now - last_discovery_ts >= _DISCOVERY_INTERVAL_S:
            _run_rediscovery(
                family_pairs=family_pairs,
                clob_ws=clob_ws,
                rest_session=rest_session,
                logger=logger,
            )
            last_discovery_ts = now

        # ── 2-second observation tick ─────────────────────────────────────────
        if now >= next_obs_ts:
            snap = rtds.get_snapshot()

            # Feed vol engine with latest Chainlink price (even if same as last tick)
            if snap.chainlink_price is not None:
                vol_engine.push_price(snap.chainlink_price, snap.chainlink_source_ts)

            # Log RTDS snapshot
            logger.log_rtds_snapshot(snap)

            # One JoinedObservation per locked family
            for family, pair in list(family_pairs.items()):
                try:
                    obs = _build_observation(
                        family=family,
                        pair=pair,
                        snap=snap,
                        vol_engine=vol_engine,
                        clob_ws=clob_ws,
                        rest_session=rest_session,
                        now_ms=now_ms,
                    )
                    logger.log_joined_observation(obs)
                except Exception as exc:
                    log.warning("observation build failed family=%s: %s", family, exc)
                    logger.log_system_event("observation_error", family=family, error=str(exc))

            next_obs_ts = now + _OBSERVATION_INTERVAL_S

        shutdown.wait(0.1)

    # ── clean shutdown ────────────────────────────────────────────────────────
    log.info("Shutting down…")
    logger.log_system_event("shutdown")
    shutdown.set()
    hb_thread.join(timeout=5.0)
    logger.close()
    log.info("Done.")


# ── helpers ───────────────────────────────────────────────────────────────────

def _refresh_clob_subscriptions(
    clob_ws: CLOBWSClient,
    family_pairs: Dict[str, MarketPair],
) -> None:
    token_ids = []
    for pair in family_pairs.values():
        token_ids.extend([pair.up_token_id, pair.down_token_id])
    clob_ws.set_subscriptions(token_ids)


def _run_rediscovery(
    family_pairs: Dict[str, MarketPair],
    clob_ws: CLOBWSClient,
    rest_session: requests.Session,
    logger: Logger,
) -> None:
    try:
        new_pairs = select_markets_by_family(session=rest_session)
    except Exception as exc:
        log.warning("Rediscovery failed: %s", exc)
        return

    changed = False
    for family, new_pair in new_pairs.items():
        old_pair = family_pairs.get(family)
        if old_pair is None or old_pair.condition_id != new_pair.condition_id:
            log.info(
                "Family lock changed: %s  %s → %s",
                family,
                old_pair.slug if old_pair else "none",
                new_pair.slug,
            )
            logger.log_system_event(
                "family_lock_change",
                family=family,
                old_slug=old_pair.slug if old_pair else None,
                new_slug=new_pair.slug,
                new_end_ms=new_pair.end_time_ms,
            )
            logger.log_market_discovery(family, [new_pair.slug])
            family_pairs[family] = new_pair
            changed = True

    if changed:
        _refresh_clob_subscriptions(clob_ws, family_pairs)


if __name__ == "__main__":
    main()
