"""
metadata/market_metadata.py — Gamma-first metadata extraction.

Source priority:
  1. Gamma market object (passed from discovery) — all canonical fields
  2. CLOB /markets/{condition_id} — supplemental only, attempted silently

Since the CLOB endpoint returns 404 for many active markets, Gamma is primary.
CLOB is tried only if a critical field is still missing after Gamma extraction.

Gamma field names used:
  orderPriceMinTickSize  — tick size
  orderMinSize           — minimum order size
  acceptingOrders        — market accepting orders
  ready                  — market ready to trade
  feeSchedule.rate       — taker fee integer (>1 → divide by 10000)
  feesEnabled            — fees active flag

fee_provenance:
  "canonical_market_object"  — sourced from Gamma/CLOB data
  "fallback_config"          — no canonical source, using configured default
  "missing"                  — no fee found anywhere (no-trade block)
"""
from __future__ import annotations

import logging
import time
from typing import Optional

import requests

from loggingx.schemas import MetadataReadyEvent
from state import MarketMetadata

log = logging.getLogger(__name__)
_TIMEOUT = 8


class MarketMetadataFetcher:
    """
    Gamma-first metadata extractor.
    Extracts all critical fields from the Gamma market object passed from discovery.
    Only attempts CLOB as a supplemental source if critical fields are still missing.
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
        Extract metadata. gamma_data (Gamma market object from discovery) is primary.
        CLOB is tried silently only if tick_size or min_order_size is still missing.
        """
        meta = MarketMetadata(condition_id=condition_id)
        meta.fetched_at = time.time()

        # ── Step 1: Extract everything from Gamma market object ───────────────
        if gamma_data is not None:
            _apply_gamma_fields(meta, gamma_data)

        # ── Step 2: Try CLOB only if Gamma left any critical field empty ────────
        # This is a secondary fallback. CLOB 404 is expected and handled silently.
        if meta.tick_size is None or meta.min_order_size is None or meta.taker_fee_rate is None:
            clob_data = _try_clob_fetch(self._clob_base, condition_id)
            if clob_data is not None:
                meta.raw_clob_response = clob_data
                if meta.tick_size is None:
                    raw_tick = (
                        clob_data.get("minimum_tick_size")
                        or clob_data.get("tick_size")
                    )
                    if raw_tick is not None:
                        try:
                            meta.tick_size = float(raw_tick)
                            meta.tick_size_provenance = "canonical_market_object"
                        except (TypeError, ValueError):
                            pass
                if meta.min_order_size is None:
                    raw_min = (
                        clob_data.get("minimum_order_size")
                        or clob_data.get("min_order_size")
                    )
                    if raw_min is not None:
                        try:
                            meta.min_order_size = float(raw_min)
                            meta.min_order_size_provenance = "canonical_market_object"
                        except (TypeError, ValueError):
                            pass
                # CLOB fee only if Gamma had no feeSchedule
                if meta.taker_fee_rate is None:
                    clob_fee = _extract_fee_from_clob(clob_data)
                    if clob_fee is not None:
                        meta.taker_fee_rate = clob_fee
                        meta.fee_provenance = "canonical_market_object"

        # ── Step 3: Final fee fallback ─────────────────────────────────────────
        if meta.taker_fee_rate is None:
            meta.taker_fee_rate = taker_fee_rate_fallback
            meta.fee_provenance = "fallback_config"
            log.info("metadata[%s] fee fallback_config=%.4f", condition_id[:8], taker_fee_rate_fallback)

        log.info(
            "metadata[%s] tick=%s min_sz=%s fee=%.4f(%s) ready=%s accepting=%s",
            condition_id[:8],
            meta.tick_size, meta.min_order_size,
            meta.taker_fee_rate or 0.0, meta.fee_provenance,
            meta.ready, meta.accepting_orders,
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


def _apply_gamma_fields(meta: MarketMetadata, data: dict) -> None:
    """Extract all canonical fields from a Gamma market object into meta."""
    raw_tick = data.get("orderPriceMinTickSize")
    if raw_tick is not None:
        try:
            meta.tick_size = float(raw_tick)
            meta.tick_size_provenance = "canonical_market_object"
        except (TypeError, ValueError):
            log.warning("gamma: bad orderPriceMinTickSize: %s", raw_tick)

    raw_min = data.get("orderMinSize")
    if raw_min is not None:
        try:
            meta.min_order_size = float(raw_min)
            meta.min_order_size_provenance = "canonical_market_object"
        except (TypeError, ValueError):
            log.warning("gamma: bad orderMinSize: %s", raw_min)

    if "acceptingOrders" in data:
        meta.accepting_orders = bool(data["acceptingOrders"])

    if "ready" in data:
        meta.ready = bool(data["ready"])

    gamma_fee, schedule_present, fees_enabled = _extract_fee_from_gamma(data)
    meta.fee_schedule_present = schedule_present
    meta.fees_enabled = fees_enabled
    if gamma_fee is not None:
        meta.taker_fee_rate = gamma_fee
        meta.fee_provenance = "canonical_market_object"


def _try_clob_fetch(clob_base: str, condition_id: str) -> Optional[dict]:
    """Attempt CLOB fetch. Returns None silently on 404 or any error."""
    try:
        url = f"{clob_base}/markets/{condition_id}"
        resp = requests.get(url, timeout=_TIMEOUT)
        if resp.status_code == 404:
            log.debug("CLOB /markets/%s → 404 (expected, Gamma is primary)", condition_id[:8])
            return None
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as exc:
        log.debug("CLOB fetch skipped for %s: %s", condition_id[:8], exc)
        return None


def _extract_fee_from_gamma(data: dict) -> tuple:
    """
    Extract taker fee rate from a Gamma market object.
    Returns (fee_rate_decimal, schedule_present, fees_enabled).

    Normalization: exponent field is NOT used (observed to cause over-division).
    Heuristic only: if rate > 1, treat as integer basis points → divide by 10000.
    Example: rate=720 → 720/10000 = 0.072 (Polymarket crypto taker rate)
    Audit line: log.info shows raw_rate and normalized value.
    """
    fees_enabled: Optional[bool] = None
    raw_enabled = data.get("feesEnabled")
    if raw_enabled is not None:
        fees_enabled = bool(raw_enabled)

    schedule = data.get("feeSchedule") or data.get("fee_schedule")
    if schedule is None or not isinstance(schedule, dict):
        return None, False, fees_enabled

    # Primary field: rate. Fall back to legacy names.
    raw_rate = (
        schedule.get("rate")
        or schedule.get("takerBaseFee")
        or schedule.get("takerFee")
        or schedule.get("taker")
    )
    if raw_rate is None:
        log.info("Gamma feeSchedule present but no rate field: %s", schedule)
        return None, True, fees_enabled  # schedule present but no rate field

    try:
        val = float(raw_rate)
        # Normalization: do NOT use the exponent field — Gamma's exponent has been
        # observed to cause over-division (e.g. rate=720, exponent=5 → 0.0072 instead
        # of 0.072). Use heuristic only: if val > 1, it's an integer bps value → /10000.
        if val > 1.0:
            val = val / 10000.0

        # Sanity: 0% to 15% covers all known Polymarket fee tiers
        if 0.0 <= val <= 0.15:
            log.info("Gamma feeSchedule: raw_rate=%s -> normalized=%.4f (%.2f%%)", raw_rate, val, val * 100)
            return val, True, fees_enabled
        log.warning("Gamma feeSchedule rate out of range: raw=%s -> %.6f", raw_rate, val)
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
