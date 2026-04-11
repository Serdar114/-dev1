"""
metadata/market_metadata.py — CLOB + Gamma metadata fetching.

Source priority for fee rate:
  1. Gamma feeSchedule.rate           → provenance "canonical_market_object" (best)
  2. CLOB market response fee fields  → provenance "canonical_market_object"
  3. config default_taker_fee_rate    → provenance "fallback_config" (flagged, not blocked)
  4. None                             → provenance "missing" (no-trade block)

Field name reference (official Gamma market object):
  orderPriceMinTickSize  — tick size
  orderMinSize           — minimum order size
  acceptingOrders        — market is accepting orders
  ready                  — market is ready to trade

New fields tracked per-record:
  fee_schedule_present   — True only when a feeSchedule object was found
  fees_enabled           — from feesEnabled in Gamma response
  accepting_orders       — from acceptingOrders in Gamma / enable_order_book in CLOB
  ready                  — from ready in Gamma market object

No-trade if:
  tick_size is None, min_order_size is None, or fee provenance == "missing"
"""
from __future__ import annotations

import logging
import time
from typing import Optional

import requests

from loggingx.schemas import MetadataFailedEvent, MetadataReadyEvent
from state import MarketMetadata

log = logging.getLogger(__name__)
_TIMEOUT = 10


class MarketMetadataFetcher:
    """
    Fetches metadata from CLOB API, supplemented by Gamma market object.
    Returns MarketMetadata with explicit provenance on every critical field.
    """

    def __init__(self, config: dict, event_logger=None) -> None:
        self._clob_base = config["polymarket"]["clob_base"]
        self._logger = event_logger

    def _log(self, event) -> None:
        if self._logger:
            self._logger.log(event)

    def fetch(
        self,
        condition_id: str,
        taker_fee_rate_fallback: float,
        gamma_data: Optional[dict] = None,
    ) -> MarketMetadata:
        """
        Fetch metadata for condition_id.

        gamma_data:            raw Gamma API market object (from discovery), may be None.
        taker_fee_rate_fallback: used only if all canonical sources fail.
        """
        meta = MarketMetadata(condition_id=condition_id)
        url = f"{self._clob_base}/markets/{condition_id}"

        try:
            resp = requests.get(url, timeout=_TIMEOUT)
            resp.raise_for_status()
            clob_data = resp.json()
            meta.raw_clob_response = clob_data
            meta.fetched_at = time.time()

            # --- tick_size: prefer Gamma orderPriceMinTickSize, fall back to CLOB ---
            raw_tick = (
                (gamma_data or {}).get("orderPriceMinTickSize")
                or clob_data.get("minimum_tick_size")
                or clob_data.get("tick_size")
            )
            if raw_tick is not None:
                try:
                    meta.tick_size = float(raw_tick)
                    meta.tick_size_provenance = "canonical_market_object"
                except (TypeError, ValueError):
                    log.warning("Could not parse tick_size: %s", raw_tick)

            # --- min_order_size: prefer Gamma orderMinSize, fall back to CLOB ---
            raw_min = (
                (gamma_data or {}).get("orderMinSize")
                or clob_data.get("minimum_order_size")
                or clob_data.get("min_order_size")
            )
            if raw_min is not None:
                try:
                    meta.min_order_size = float(raw_min)
                    meta.min_order_size_provenance = "canonical_market_object"
                except (TypeError, ValueError):
                    log.warning("Could not parse min_order_size: %s", raw_min)

            # --- accepting_orders: prefer Gamma acceptingOrders, fall back to CLOB ---
            raw_active = (
                (gamma_data or {}).get("acceptingOrders")
                if gamma_data is not None and "acceptingOrders" in gamma_data
                else clob_data.get("enable_order_book") or clob_data.get("accepting_orders")
            )
            if raw_active is not None:
                meta.accepting_orders = bool(raw_active)

            # --- ready (Gamma only) ---
            if gamma_data is not None and "ready" in gamma_data:
                meta.ready = bool(gamma_data["ready"])

            # --- fee: try sources in priority order ---
            # Priority 1: Gamma feeSchedule (most canonical)
            if gamma_data is not None:
                gamma_fee, schedule_present, fees_enabled = _extract_fee_from_gamma(gamma_data)
                meta.fee_schedule_present = schedule_present
                meta.fees_enabled = fees_enabled
                if gamma_fee is not None:
                    meta.taker_fee_rate = gamma_fee
                    meta.fee_provenance = "canonical_market_object"
                    log.info(
                        "Fee from Gamma feeSchedule.rate for %s: %.6f",
                        condition_id, gamma_fee,
                    )

            # Priority 2: CLOB response (if Gamma didn't provide it)
            if meta.taker_fee_rate is None:
                clob_fee = _extract_fee_from_clob(clob_data)
                if clob_fee is not None:
                    meta.taker_fee_rate = clob_fee
                    meta.fee_provenance = "canonical_market_object"
                    log.info(
                        "Fee from CLOB response for %s: %.6f",
                        condition_id, clob_fee,
                    )

            # Priority 3: config default (fallback — provenance clearly marked)
            if meta.taker_fee_rate is None:
                meta.taker_fee_rate = taker_fee_rate_fallback
                meta.fee_provenance = "fallback_config"
                log.info(
                    "Fee not in Gamma or CLOB for %s; fallback_config=%.4f",
                    condition_id, taker_fee_rate_fallback,
                )

            self._log(MetadataReadyEvent(
                condition_id=condition_id,
                tick_size=meta.tick_size,
                min_order_size=meta.min_order_size,
                taker_fee_rate=meta.taker_fee_rate,
                fee_provenance=meta.fee_provenance,
                readiness=meta.readiness_label(),
                window_id=0,
            ))
            return meta

        except requests.RequestException as exc:
            log.error("CLOB metadata fetch failed for %s: %s", condition_id, exc)
            self._log(MetadataFailedEvent(
                condition_id=condition_id,
                error=str(exc),
                window_id=0,
            ))
            # Still try to extract fee from Gamma even if CLOB failed
            if gamma_data is not None:
                gamma_fee, schedule_present, fees_enabled = _extract_fee_from_gamma(gamma_data)
                meta.fee_schedule_present = schedule_present
                meta.fees_enabled = fees_enabled
                if gamma_fee is not None:
                    meta.taker_fee_rate = gamma_fee
                    meta.fee_provenance = "canonical_market_object"
            if meta.taker_fee_rate is None:
                meta.taker_fee_rate = taker_fee_rate_fallback
                meta.fee_provenance = "fallback_config"
            meta.fetched_at = time.time()
            return meta


def _extract_fee_from_gamma(data: dict) -> tuple:
    """
    Extract taker fee rate from a Gamma market object.
    Returns (fee_rate_decimal, schedule_present, fees_enabled).

    Official feeSchedule fields:
      rate      — taker fee integer (e.g. 720 for 7.2%)
      exponent  — scale divisor exponent (e.g. 4 → divide by 10^4)
                  If absent, heuristic: value > 1 → divide by 10000.

    Example: rate=720, exponent=4 → 720 / 10000 = 0.072 (crypto market rate)
    """
    fees_enabled: Optional[bool] = None
    raw_enabled = data.get("feesEnabled")
    if raw_enabled is not None:
        fees_enabled = bool(raw_enabled)

    schedule = data.get("feeSchedule") or data.get("fee_schedule")
    if schedule is None or not isinstance(schedule, dict):
        return None, False, fees_enabled

    log.debug("Gamma feeSchedule raw: %s", schedule)

    # Primary field: rate. Fall back to legacy names.
    raw_rate = (
        schedule.get("rate")
        or schedule.get("takerBaseFee")
        or schedule.get("takerFee")
        or schedule.get("taker")
    )
    if raw_rate is None:
        return None, True, fees_enabled  # schedule present but no rate field

    try:
        val = float(raw_rate)
        raw_exp = schedule.get("exponent")
        if raw_exp is not None:
            try:
                exp = int(raw_exp)
                val = val / (10 ** exp)
            except (TypeError, ValueError):
                # exponent unparseable — fall through to heuristic
                if val > 1.0:
                    val = val / 10000.0
        elif val > 1.0:
            # Heuristic: large integer → basis points with 10^4 divisor
            val = val / 10000.0

        # Sanity: 0 to 15% (crypto markets can be up to ~10%)
        if 0.0 <= val <= 0.15:
            return val, True, fees_enabled
        log.warning("Gamma feeSchedule rate out of range after normalization: raw=%s → %.6f", raw_rate, val)
        return None, True, fees_enabled
    except (TypeError, ValueError):
        log.warning("Could not parse Gamma feeSchedule rate: %s", raw_rate)
        return None, True, fees_enabled


def _extract_fee_from_clob(data: dict) -> Optional[float]:
    """
    Extract taker fee from CLOB market response.
    Returns None if not determinable. Never guesses.
    """
    for key in ("taker_fee", "takerFee", "neg_risk_reward_fee"):
        val = data.get(key)
        if val is not None:
            try:
                f = float(val)
                if 0.0 <= f <= 0.10:
                    return f
            except (TypeError, ValueError):
                continue
    return None
