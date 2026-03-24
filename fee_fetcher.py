"""
fee_fetcher.py - Live fee rate retrieval from Polymarket CLOB endpoints.

Attempts GET /fees on the CLOB REST API. That endpoint requires authentication
per the Polymarket CLOB SDK. Unauthenticated access may return 401/403.
On any failure, falls back to config.TAKER_FEE_RATE and labels the result
explicitly as fee_rate_source="config_fallback", fee_truth_status="assumed".

FEE MODEL TRUTH CONSTRAINT (permanent):
  Even if the live endpoint returns a fee rate value, we cannot determine from
  REST responses alone whether the fee is:
    (a) usdc_extra  – pay extra USDC, receive all shares ordered
    (b) share_cut   – pay face USDC, receive fewer shares (fee deducted from fill)
  Therefore fee_model_assumption is always "unresolved" from this module.
  It can only be upgraded to "confirmed" after examining actual fill receipts
  or reading authenticated order receipts from the CLOB.

  Both model outputs are available via fee_math.compute_both_models().

Usage:
  from fee_fetcher import session_fee_fetch
  fee_results = session_fee_fetch(markets)   # at session start
  # fee_results["_global"]["fee_rate_value"] is the rate to use
  # fee_results[token_id] is per-token dict (inherited from global)
"""

import logging
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

import requests

import config

log = logging.getLogger(__name__)

# Candidate endpoints (annotated with access requirements)
_FEES_ENDPOINT = f"{config.CLOB_BASE_URL}/fees"
# Note: /fees requires CLOB authentication headers (L1/L2 signatures).
# Unauthenticated requests will return HTTP 401 or 403.
# This module attempts it anyway and handles rejection explicitly.


# ── single token or global fetch ─────────────────────────────────────────────

def fetch_live_fee_rate(token_id: Optional[str] = None) -> Dict:
    """
    Attempt to retrieve the current taker fee rate from the CLOB /fees endpoint.

    Returns a dict with:
      token_id              : the token this result is associated with (or None = global)
      fee_rate_value        : float - rate to use for calculations
      fee_rate_source       : "live_endpoint" | "config_fallback"
      fee_truth_status      : "confirmed" | "assumed" | "unresolved"
      fee_model_assumption  : "unresolved" (always - see module docstring)
      fetch_attempted       : bool
      fetch_success         : bool
      fetch_error           : str or None - explicit reason for fallback
      fetch_ts_utc          : ISO timestamp of attempt
      endpoint_used         : str or None
      raw_response          : dict or None - raw JSON if fetch succeeded
    """
    now_utc = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    result = {
        "token_id":              token_id,
        "fetch_attempted":       False,
        "fetch_success":         False,
        "fetch_error":           None,
        "fetch_ts_utc":          now_utc,
        "endpoint_used":         None,
        "raw_response":          None,
        # defaults – overwritten on successful parse
        "fee_rate_source":       "config_fallback",
        "fee_rate_value":        config.TAKER_FEE_RATE,
        "fee_truth_status":      "assumed",
        "fee_model_assumption":  "unresolved",
    }

    endpoint = _FEES_ENDPOINT
    result["fetch_attempted"] = True
    result["endpoint_used"]   = endpoint

    try:
        resp = requests.get(
            endpoint,
            timeout=config.HTTP_TIMEOUT_S,
            headers={"Accept": "application/json"},
        )
        if resp.status_code == 200:
            data = resp.json()
            result["raw_response"] = data
            rate = _parse_taker_fee(data)
            if rate is not None:
                result["fee_rate_value"]   = rate
                result["fee_rate_source"]  = "live_endpoint"
                result["fee_truth_status"] = "confirmed"
                result["fetch_success"]    = True
                log.info(
                    "[fee_fetcher] Live taker fee rate fetched: %.4f  (endpoint=%s)",
                    rate, endpoint,
                )
            else:
                result["fetch_error"] = (
                    f"HTTP 200 but no parseable taker fee rate in response: "
                    f"{str(data)[:300]}"
                )
                log.warning("[fee_fetcher] /fees 200 but rate not parseable: %s",
                            str(data)[:200])
        else:
            result["fetch_error"] = f"HTTP {resp.status_code}: {resp.text[:200]}"
            log.warning(
                "[fee_fetcher] /fees HTTP %d – expected 401/403 if unauthenticated. "
                "Falling back to config.TAKER_FEE_RATE=%.4f",
                resp.status_code, config.TAKER_FEE_RATE,
            )

    except requests.exceptions.Timeout:
        result["fetch_error"] = f"Timeout after {config.HTTP_TIMEOUT_S}s"
        log.warning("[fee_fetcher] /fees timed out. Falling back to config.")
    except requests.exceptions.ConnectionError as exc:
        result["fetch_error"] = f"ConnectionError: {exc}"
        log.warning("[fee_fetcher] /fees connection error: %s. Falling back to config.", exc)
    except Exception as exc:
        result["fetch_error"] = f"{type(exc).__name__}: {exc}"
        log.warning("[fee_fetcher] /fees unexpected error: %s. Falling back to config.", exc)

    # Explicit fallback log – never silent
    if not result["fetch_success"]:
        log.warning(
            "[fee_fetcher] FEE FALLBACK ACTIVE: "
            "fee_rate=%.4f (config.TAKER_FEE_RATE)  "
            "fee_truth_status=assumed  fee_model_assumption=unresolved  "
            "fetch_error=%s",
            result["fee_rate_value"],
            result["fetch_error"],
        )

    return result


def _parse_taker_fee(data) -> Optional[float]:
    """
    Try to extract a numeric taker fee rate from known response shapes.
    Returns float on success, None if not parseable.
    """
    if not isinstance(data, dict):
        return None
    # Direct field names observed in Polymarket SDK responses
    for key in ("taker_fee_rate", "takerFeeRate", "taker_fee", "takerFee"):
        val = data.get(key)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                pass
    # Nested: {"fees": {"taker": 0.02}}
    fees_obj = data.get("fees")
    if isinstance(fees_obj, dict):
        for key in ("taker", "taker_fee_rate", "takerFeeRate"):
            val = fees_obj.get(key)
            if val is not None:
                try:
                    return float(val)
                except (TypeError, ValueError):
                    pass
    return None


# ── session-level batch fetch ─────────────────────────────────────────────────

def session_fee_fetch(markets: List[Dict]) -> Dict:
    """
    Fetch fee rates for all tracked markets at session start.

    The Polymarket CLOB /fees endpoint is global (not per-token).
    One fetch is made and the result is propagated to all tokens.
    Each token entry includes a 'note' field documenting the inheritance.

    Returns dict keyed by:
      "_global"   : the raw global fetch result
      <token_id>  : per-token result (inherits rate from global)
    """
    results: Dict[str, Dict] = {}

    # Single global fetch (not per-token; no per-token fee endpoint known)
    global_result = fetch_live_fee_rate(token_id=None)
    results["_global"] = global_result

    # Propagate to all tracked tokens with explicit inheritance note
    for mkt in markets:
        for id_key in ("yes_token_id", "no_token_id"):
            token_id = mkt.get(id_key)
            if not token_id:
                continue
            per_tok = dict(global_result)
            per_tok["token_id"] = token_id
            per_tok["note"] = (
                "rate inherited from global /fees fetch; "
                "no per-token fee endpoint known on Polymarket CLOB"
            )
            results[token_id] = per_tok

    total_tokens = len(results) - 1  # exclude _global
    log.info(
        "[fee_fetcher] session_fee_fetch complete: "
        "source=%s  rate=%.4f  tokens_covered=%d  truth_status=%s",
        global_result["fee_rate_source"],
        global_result["fee_rate_value"],
        total_tokens,
        global_result["fee_truth_status"],
    )

    return results
