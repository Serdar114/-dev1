"""
metadata/market_metadata.py — CLOB metadata fetching.

Fetches tick_size and min_order_size from the CLOB API.
These are canonical values; no fallback substitution is applied here.
If the field is absent from CLOB response, provenance is "missing".
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
    Fetches market metadata from the Polymarket CLOB API.
    Returns a MarketMetadata with explicit provenance on each field.
    """

    def __init__(self, config: dict, event_logger=None) -> None:
        self._clob_base = config["polymarket"]["clob_base"]
        self._logger = event_logger

    def _log(self, event) -> None:
        if self._logger:
            self._logger.log(event)

    def fetch(self, condition_id: str, taker_fee_rate_fallback: float) -> MarketMetadata:
        """
        Fetch metadata for condition_id.
        taker_fee_rate_fallback: used only if CLOB does not provide fee.
        """
        meta = MarketMetadata(condition_id=condition_id)
        url = f"{self._clob_base}/markets/{condition_id}"

        try:
            resp = requests.get(url, timeout=_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            meta.raw_clob_response = data
            meta.fetched_at = time.time()

            # --- tick_size ---
            raw_tick = data.get("minimum_tick_size") or data.get("tick_size")
            if raw_tick is not None:
                try:
                    meta.tick_size = float(raw_tick)
                    meta.tick_size_provenance = "canonical"
                except (TypeError, ValueError):
                    log.warning("Could not parse tick_size: %s", raw_tick)

            # --- min_order_size ---
            raw_min = data.get("minimum_order_size") or data.get("min_order_size")
            if raw_min is not None:
                try:
                    meta.min_order_size = float(raw_min)
                    meta.min_order_size_provenance = "canonical"
                except (TypeError, ValueError):
                    log.warning("Could not parse min_order_size: %s", raw_min)

            # --- fee_rate: attempt canonical fetch, else apply fallback ---
            canonical_fee = _extract_fee_from_clob(data)
            if canonical_fee is not None:
                meta.taker_fee_rate = canonical_fee
                meta.fee_provenance = "canonical"
            else:
                meta.taker_fee_rate = taker_fee_rate_fallback
                meta.fee_provenance = "config_default"
                log.info(
                    "Fee not in CLOB response for %s; using config_default=%.4f",
                    condition_id,
                    taker_fee_rate_fallback,
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
            meta.fetched_at = time.time()
            return meta


def _extract_fee_from_clob(data: dict) -> Optional[float]:
    """
    Attempt to extract taker fee rate from CLOB market response.
    Returns None if not determinable. Never guesses.
    """
    # Known CLOB fields to try
    for key in ("taker_fee", "takerFee", "maker_base_fee", "neg_risk_reward_fee"):
        val = data.get(key)
        if val is not None:
            try:
                f = float(val)
                # Sanity: fee should be between 0 and 0.10 (0–10%)
                if 0.0 <= f <= 0.10:
                    return f
            except (TypeError, ValueError):
                continue
    return None
