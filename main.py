"""
Polymarket BTC up/down 5m + 15m observation logger.

Observation-only — no trades, no alarms, no signal logic.
Collects enriched market data and writes it to JSONL streams under --log-dir.

Usage:
  python main.py --duration 1800 --log-dir logs

Components:
  RTDSClient      — Binance + Chainlink prices via Polymarket RTDS WS
  CLOBWebSocket   — Live order books via Polymarket CLOB WS
  clob_rest       — REST fallback for order books (WS-first)
  gamma_api       — Market discovery (condition IDs, token IDs, metadata)
  VolEngine       — 60-minute realised annualised vol from Chainlink ticks
  Logger          — Six JSONL streams; all writes flushed immediately
"""
import argparse
import math
import signal
import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional

import requests

try:
    from scipy.stats import norm as _scipy_norm  # type: ignore
    def _norm_cdf(x: float) -> float:
        return float(_scipy_norm.cdf(x))
    SCIPY_AVAILABLE = True
except ImportError:
    SCIPY_AVAILABLE = False

    def _norm_cdf(x: float) -> float:
        """Abramowitz & Stegun 26.2.17 approximation of standard normal CDF."""
        b1, b2, b3, b4, b5 = (
            0.319381530, -0.356563782, 1.781477937, -1.821255978, 1.330274429
        )
        p_coef = 0.2316419
        c = 0.39894228034  # 1/sqrt(2π)
        if x < 0:
            return 1.0 - _norm_cdf(-x)
        t = 1.0 / (1.0 + p_coef * x)
        poly = ((((b5 * t + b4) * t + b3) * t + b2) * t + b1) * t
        return 1.0 - c * math.exp(-0.5 * x * x) * poly


from clob_rest import get_book as clob_rest_get_book
from clob_ws import CLOBWebSocket
from gamma_api import select_markets_by_family
from logger import Logger
from rtds_client import RTDSClient
from schemas import BookSnapshot, DualReferenceSnapshot, JoinedObservation, MarketPair
from vol_engine import VolEngine

OBSERVATION_INTERVAL_S = 2
HEARTBEAT_INTERVAL_S = 30
DISCOVERY_INTERVAL_S = 30


class Orchestrator:
    def __init__(self, duration: int, log_dir: str) -> None:
        self._duration = duration
        self._shutdown = threading.Event()

        self._logger = Logger(log_dir=log_dir)
        self._session = requests.Session()

        self._rtds = RTDSClient(
            on_event=self._on_system_event,
            shutdown_event=self._shutdown,
        )
        self._clob_ws = CLOBWebSocket(
            on_event=self._on_system_event,
            shutdown_event=self._shutdown,
        )
        self._vol_engine = VolEngine()

        self._market_pairs: Dict[str, MarketPair] = {}
        self._market_lock = threading.Lock()

        self._last_discovery: float = 0.0
        self._last_heartbeat: float = 0.0

    # ------------------------------------------------------------------ #
    # Event callbacks                                                      #
    # ------------------------------------------------------------------ #

    def _on_system_event(self, event_type: str, data: dict) -> None:
        self._logger.log_system_event(event_type, data)

    # ------------------------------------------------------------------ #
    # Market discovery                                                     #
    # ------------------------------------------------------------------ #

    def _discover_markets(self) -> None:
        try:
            new_pairs = select_markets_by_family(self._session)
        except Exception as exc:
            self._logger.log_system_event(
                "discovery_error", {"error": str(exc)}
            )
            return

        token_ids = set()
        with self._market_lock:
            for family, pair in new_pairs.items():
                old = self._market_pairs.get(family)
                if old is None or old.condition_id != pair.condition_id:
                    self._logger.log_market_discovery(
                        family,
                        {
                            "condition_id": pair.condition_id,
                            "slug": pair.slug,
                            "end_time_ms": pair.end_time_ms,
                            "up_token_id": pair.up_token_id,
                            "down_token_id": pair.down_token_id,
                            "start_price": pair.start_price,
                            "fee_rate": pair.fee_rate,
                            "fee_c": pair.fee_c,
                        },
                    )
                self._market_pairs[family] = pair

            for pair in self._market_pairs.values():
                token_ids.add(pair.up_token_id)
                token_ids.add(pair.down_token_id)

        self._clob_ws.set_subscriptions(token_ids)

    # ------------------------------------------------------------------ #
    # Book helpers                                                         #
    # ------------------------------------------------------------------ #

    def _get_book(self, token_id: str) -> Optional[BookSnapshot]:
        """WS snapshot first; REST fallback if WS has no data yet."""
        snap = self._clob_ws.get_snapshot(token_id)
        if snap is not None:
            return snap
        return clob_rest_get_book(token_id, self._session)

    # ------------------------------------------------------------------ #
    # Observation building                                                 #
    # ------------------------------------------------------------------ #

    def _build_observation(
        self, pair: MarketPair, ref: DualReferenceSnapshot
    ) -> JoinedObservation:
        now_ms = int(time.time() * 1000)

        up_book = self._get_book(pair.up_token_id)
        down_book = self._get_book(pair.down_token_id)

        if up_book is not None:
            self._logger.log_book_update(up_book)
        if down_book is not None:
            self._logger.log_book_update(down_book)

        seconds_to_expiry = max((pair.end_time_ms - now_ms) / 1000.0, 0.0)

        obs = JoinedObservation(
            # core
            ts_local=now_ms,
            family=pair.family,
            condition_id=pair.condition_id,
            slug=pair.slug,
            end_time_ms=pair.end_time_ms,
            seconds_to_expiry=seconds_to_expiry,
            # up side
            up_token_id=pair.up_token_id,
            up_best_bid=up_book.best_bid if up_book else None,
            up_best_ask=up_book.best_ask if up_book else None,
            up_mid=up_book.mid if up_book else None,
            up_spread=up_book.spread if up_book else None,
            up_book_source=up_book.source if up_book else None,
            # down side
            down_token_id=pair.down_token_id,
            down_best_bid=down_book.best_bid if down_book else None,
            down_best_ask=down_book.best_ask if down_book else None,
            down_mid=down_book.mid if down_book else None,
            down_spread=down_book.spread if down_book else None,
            down_book_source=down_book.source if down_book else None,
        )

        # ---- External reference ----------------------------------------
        obs.external_btc_price_binance = ref.binance_price
        obs.external_btc_price_chainlink = ref.chainlink_price
        obs.external_basis_bps = ref.basis_bps
        obs.external_lag_ms = ref.lag_ms
        obs.stale_binance = ref.binance_stale
        obs.stale_chainlink = ref.chainlink_stale

        # ---- Market metadata -------------------------------------------
        obs.barrier_price = pair.start_price
        obs.seconds_to_resolution = seconds_to_expiry

        # ---- Vol & sigma -----------------------------------------------
        realized_vol = self._vol_engine.get_annualized_vol()
        obs.realized_vol_60m = realized_vol

        chainlink_price = ref.chainlink_price
        barrier = pair.start_price
        sec_to_res = seconds_to_expiry

        if (
            chainlink_price is not None
            and barrier is not None
            and realized_vol is not None
            and sec_to_res > 0
        ):
            period_sigma = realized_vol * math.sqrt(sec_to_res / 31_536_000)
            if period_sigma > 0:
                expected_move = chainlink_price * period_sigma
                if expected_move > 0:
                    obs.sigma_distance = (chainlink_price - barrier) / expected_move
                    obs.fair_up_prob = _norm_cdf(obs.sigma_distance)

        # ---- Edge layer ------------------------------------------------
        up_ask = obs.up_best_ask
        down_ask = obs.down_best_ask
        fair = obs.fair_up_prob

        if fair is not None and up_ask is not None:
            obs.edge_up_raw = fair - up_ask
        if fair is not None and down_ask is not None:
            obs.edge_down_raw = (1.0 - fair) - down_ask

        fee_rate = pair.fee_rate
        fee_c = pair.fee_c
        if fee_rate is not None and fee_c is not None and up_ask is not None:
            p = up_ask
            obs.effective_fee_estimate = fee_c * fee_rate * p * (1.0 - p)

        if obs.edge_up_raw is not None and obs.effective_fee_estimate is not None:
            obs.edge_up_net = obs.edge_up_raw - obs.effective_fee_estimate
        if obs.edge_down_raw is not None and obs.effective_fee_estimate is not None:
            obs.edge_down_net = obs.edge_down_raw - obs.effective_fee_estimate

        return obs

    # ------------------------------------------------------------------ #
    # Main loop                                                            #
    # ------------------------------------------------------------------ #

    def run(self) -> None:
        self._logger.log_system_event(
            "startup",
            {
                "duration_s": self._duration,
                "scipy_available": SCIPY_AVAILABLE,
            },
        )

        self._rtds.start()
        self._clob_ws.start()

        self._discover_markets()
        now = time.monotonic()
        self._last_discovery = now
        self._last_heartbeat = now
        start_ts = now

        try:
            while not self._shutdown.is_set():
                now = time.monotonic()

                if self._duration > 0 and (now - start_ts) >= self._duration:
                    self._logger.log_system_event(
                        "duration_reached", {"duration_s": self._duration}
                    )
                    break

                if now - self._last_discovery >= DISCOVERY_INTERVAL_S:
                    self._discover_markets()
                    self._last_discovery = now

                if now - self._last_heartbeat >= HEARTBEAT_INTERVAL_S:
                    self._logger.log_heartbeat(
                        {
                            "bar_count": self._vol_engine.bar_count(),
                            "market_families": list(self._market_pairs.keys()),
                            "uptime_s": int(now - start_ts),
                        }
                    )
                    self._last_heartbeat = now

                # --- Observation tick ---
                ref = self._rtds.get_snapshot()
                self._logger.log_rtds_snapshot(ref)

                if (
                    ref.chainlink_price is not None
                    and not ref.chainlink_stale
                    and ref.chainlink_source_ts is not None
                ):
                    self._vol_engine.push_price(
                        ref.chainlink_price, ref.chainlink_source_ts
                    )
                elif (
                    ref.chainlink_price is not None
                    and not ref.chainlink_stale
                ):
                    self._vol_engine.push_price(ref.chainlink_price, ref.ts_local)

                with self._market_lock:
                    pairs = dict(self._market_pairs)

                for family, pair in pairs.items():
                    try:
                        obs = self._build_observation(pair, ref)
                        self._logger.log_joined_observation(obs)
                    except Exception as exc:
                        self._logger.log_system_event(
                            "observation_error",
                            {"family": family, "error": str(exc)},
                        )

                self._shutdown.wait(OBSERVATION_INTERVAL_S)

        except KeyboardInterrupt:
            pass
        finally:
            self._shutdown_all()

    def _shutdown_all(self) -> None:
        self._logger.log_system_event("shutdown_initiated", {})
        self._shutdown.set()
        self._rtds.stop()
        self._clob_ws.stop()
        self._vol_engine.force_persist()
        self._logger.log_system_event("shutdown_complete", {})
        self._logger.close()

    def signal_shutdown(self, signum: int, frame) -> None:
        self._logger.log_system_event("signal_received", {"signum": signum})
        self._shutdown.set()


# ------------------------------------------------------------------ #
# Entry point                                                          #
# ------------------------------------------------------------------ #

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Polymarket BTC observation logger (no trading)"
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=14400,
        help="Run duration in seconds. 0 = unlimited. (default: 14400 = 4h)",
    )
    parser.add_argument(
        "--log-dir",
        default="logs",
        help="Directory for JSONL log files. (default: logs)",
    )
    args = parser.parse_args()

    orchestrator = Orchestrator(duration=args.duration, log_dir=args.log_dir)

    signal.signal(signal.SIGINT, orchestrator.signal_shutdown)
    try:
        signal.signal(signal.SIGTERM, orchestrator.signal_shutdown)
    except (OSError, ValueError):
        # SIGTERM not available on all platforms (e.g. Windows)
        pass

    orchestrator.run()


if __name__ == "__main__":
    main()
