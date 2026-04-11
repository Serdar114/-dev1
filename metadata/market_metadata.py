"""
metadata/market_metadata.py — CLOB + Gamma metadata fetching.

PATCH 4 — corrected fee/metadata source hierarchy.

Source priority for fee rate:
  1. Gamma feeSchedule.takerBaseFee   → provenance "gamma_fee_schedule" (best)
  2. CLOB market response fee fields  → provenance "clob_response"
  3. config default_taker_fee_rate    → provenance "config_default" (flagged, not blocked)
  4. None                             → provenance "missing" (no-trade block)

New fields tracked per-record:
  fee_schedule_present   — True only when a feeSchedule object was found
  fees_enabled           — from feesEnabled in Gamma response
  accepting_orders       — from enable_order_book / accepting_orders in CLOB

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

            # --- tick_size (CLOB is canonical source) ---
            raw_tick = clob_data.get("minimum_tick_size") or clob_data.get("tick_size")
            if raw_tick is not None:
                try:
                    meta.tick_size = float(raw_tick)
                    meta.tick_size_provenance = "canonical"
                except (TypeError, ValueError):
                    log.warning("Could not parse tick_size: %s", raw_tick)

            # --- min_order_size (CLOB is canonical source) ---
            raw_min = clob_data.get("minimum_order_size") or clob_data.get("min_order_size")
            if raw_min is not None:
                try:
                    meta.min_order_size = float(raw_min)
                    meta.min_order_size_provenance = "canonical"
                except (TypeError, ValueError):
                    log.warning("Could not parse min_order_size: %s", raw_min)

            # --- accepting_orders ---
            raw_active = clob_data.get("enable_order_book") or clob_data.get("accepting_orders")
            if raw_active is not None:
                meta.accepting_orders = bool(raw_active)

            # --- fee: try sources in priority order ---
            # Priority 1: Gamma feeSchedule (most canonical)
            if gamma_data is not None:
                gamma_fee, schedule_present, fees_enabled = _extract_fee_from_gamma(gamma_data)
                meta.fee_schedule_present = schedule_present
                meta.fees_enabled = fees_enabled
                if gamma_fee is not None:
                    meta.taker_fee_rate = gamma_fee
                    meta.fee_provenance = "gamma_fee_schedule"
                    log.info(
                        "Fee from Gamma feeSchedule for %s: %.6f",
                        condition_id, gamma_fee,
                    )

            # Priority 2: CLOB response (if Gamma didn't provide it)
            if meta.taker_fee_rate is None:
                clob_fee = _extract_fee_from_clob(clob_data)
                if clob_fee is not None:
                    meta.taker_fee_rate = clob_fee
                    meta.fee_provenance = "clob_response"
                    log.info(
                        "Fee from CLOB response for %s: %.6f",
                        condition_id, clob_fee,
                    )

            # Priority 3: config default (fallback — provenance clearly marked)
            if meta.taker_fee_rate is None:
                meta.taker_fee_rate = taker_fee_rate_fallback
                meta.fee_provenance = "config_default"
                log.info(
                    "Fee not in Gamma or CLOB for %s; config_default=%.4f",
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
                    meta.fee_provenance = "gamma_fee_schedule"
            if meta.taker_fee_rate is None:
                meta.taker_fee_rate = taker_fee_rate_fallback
                meta.fee_provenance = "config_default"
            meta.fetched_at = time.time()
            return meta


def _extract_fee_from_gamma(data: dict) -> tuple:
    """
    Extract taker fee rate from a Gamma market object.
    Returns (fee_rate_decimal, schedule_present, fees_enabled).

    feeSchedule.takerBaseFee may be:
      - Integer basis points (e.g. 200 → 0.02)
      - Decimal already (e.g. 0.02)
    """
    fees_enabled: Optional[bool] = None
    raw_enabled = data.get("feesEnabled")
    if raw_enabled is not None:
        fees_enabled = bool(raw_enabled)

    schedule = data.get("feeSchedule") or data.get("fee_schedule")
    if schedule is None or not isinstance(schedule, dict):
        return None, False, fees_enabled

    raw_taker = schedule.get("takerBaseFee") or schedule.get("takerFee") or schedule.get("taker")
    if raw_taker is None:
        return None, True, fees_enabled  # schedule present but no taker fee field

    try:
        val = float(raw_taker)
        # Normalise: if > 1, assume basis points
        if val > 1.0:
            val = val / 10000.0
        # Sanity: 0 to 10%
        if 0.0 <= val <= 0.10:
            return val, True, fees_enabled
        log.warning("Gamma feeSchedule takerBaseFee out of range: %s", raw_taker)
        return None, True, fees_enabled
    except (TypeError, ValueError):
        log.warning("Could not parse Gamma feeSchedule takerBaseFee: %s", raw_taker)
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
