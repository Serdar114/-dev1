"""
clob_rest.py — Read-only CLOB REST client for Polymarket.

No auth. No orders. No signed requests.
All fee / tick / min-size values are fetched dynamically — never hardcoded.

Endpoints used:
  GET /book?token_id={token_id}
  POST /books  (batch)
  GET /midpoint?token_id={token_id}
  GET /tick-size?token_id={token_id}
  GET /spread?token_id={token_id}       (if available)
  GET /neg-risk-market-info/{condition_id}  (for neg-risk fee data)

If an endpoint is unavailable or returns unexpected shape, the missing field
is returned as an explicit "unknown" state — not silently defaulted.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

import requests

import logger as log

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CLOB_BASE = "https://clob.polymarket.com"
HTTP_TIMEOUT_S = 10
# Stale threshold: if we haven't refreshed fees in this many seconds, warn
FEE_STALE_THRESHOLD_S = 300

# Per-endpoint "known unavailable" flags — set on first 404/405; skip on future calls
_market_info_unavailable: bool = False
_rewards_rates_unavailable: bool = False

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_ms() -> int:
    return int(time.time() * 1000)


def _get(path: str, params: Optional[Dict[str, Any]] = None) -> Optional[Any]:
    """GET request to CLOB. Returns parsed JSON or None on error."""
    url = f"{CLOB_BASE}{path}"
    try:
        resp = requests.get(url, params=params, timeout=HTTP_TIMEOUT_S)
        resp.raise_for_status()
        return resp.json()
    except requests.HTTPError as exc:
        log.log_exception(f"clob_rest.GET {path}", exc, {"status": exc.response.status_code if exc.response else None})
        return None
    except requests.RequestException as exc:
        log.log_exception(f"clob_rest.GET {path}", exc)
        return None
    except ValueError as exc:
        log.log_exception(f"clob_rest.GET {path} json_decode", exc)
        return None


def _post(path: str, body: Any) -> Optional[Any]:
    """POST request to CLOB. Returns parsed JSON or None on error."""
    url = f"{CLOB_BASE}{path}"
    try:
        resp = requests.post(url, json=body, timeout=HTTP_TIMEOUT_S)
        resp.raise_for_status()
        return resp.json()
    except requests.HTTPError as exc:
        log.log_exception(f"clob_rest.POST {path}", exc, {"status": exc.response.status_code if exc.response else None})
        return None
    except requests.RequestException as exc:
        log.log_exception(f"clob_rest.POST {path}", exc)
        return None
    except ValueError as exc:
        log.log_exception(f"clob_rest.POST {path} json_decode", exc)
        return None


def _parse_price_levels(
    raw_levels: Any, side: str, token_id: str
) -> List[Dict[str, float]]:
    """
    Parse a list of {price, size} dicts from CLOB book response.
    Returns list of {"price": float, "size": float}, sorted best-first.
    bids: descending price; asks: ascending price.
    """
    if not isinstance(raw_levels, list):
        if raw_levels is not None:
            log.log_parse_anomaly(
                f"clob_rest.book/{token_id}", side, type(raw_levels).__name__, "expected list"
            )
        return []

    result: List[Dict[str, float]] = []
    for i, lvl in enumerate(raw_levels):
        if not isinstance(lvl, dict):
            log.log_parse_anomaly(
                f"clob_rest.book/{token_id}", f"{side}[{i}]", lvl, "expected dict"
            )
            continue
        try:
            price = float(lvl.get("price", lvl.get("p", 0)))
            size = float(lvl.get("size", lvl.get("s", 0)))
            result.append({"price": price, "size": size})
        except (TypeError, ValueError) as exc:
            log.log_parse_anomaly(
                f"clob_rest.book/{token_id}", f"{side}[{i}]", lvl, f"float conversion: {exc}"
            )

    if side.lower() in ("bids", "buy"):
        result.sort(key=lambda x: x["price"], reverse=True)
    else:
        result.sort(key=lambda x: x["price"])
    return result


def _top_of_book(
    bids: List[Dict[str, float]], asks: List[Dict[str, float]]
) -> Dict[str, Optional[float]]:
    best_bid = bids[0]["price"] if bids else None
    bid_size = bids[0]["size"] if bids else None
    best_ask = asks[0]["price"] if asks else None
    ask_size = asks[0]["size"] if asks else None

    spread_abs: Optional[float] = None
    spread_pct: Optional[float] = None
    if best_bid is not None and best_ask is not None:
        spread_abs = round(best_ask - best_bid, 6)
        mid = (best_bid + best_ask) / 2
        if mid > 0:
            spread_pct = round(spread_abs / mid * 100, 4)

    return {
        "best_bid": best_bid,
        "bid_size": bid_size,
        "best_ask": best_ask,
        "ask_size": ask_size,
        "spread_abs": spread_abs,
        "spread_pct": spread_pct,
    }


# ---------------------------------------------------------------------------
# Book endpoints
# ---------------------------------------------------------------------------

def get_book(token_id: str) -> Dict[str, Any]:
    """
    Fetch the full order book for a single token.

    Returns dict with keys:
      token_id, ts_local, bids, asks, best_bid, bid_size, best_ask, ask_size,
      spread_abs, spread_pct, raw_timestamp, book_state_flags
    """
    ts_local = _now_ms()
    data = _get("/book", params={"token_id": token_id})

    if data is None:
        return {
            "token_id": token_id,
            "ts_local": ts_local,
            "error": "fetch_failed",
            "book_state_flags": ["fetch_error"],
        }

    if not isinstance(data, dict):
        log.log_parse_anomaly("clob_rest.get_book", "response", type(data).__name__, "expected dict")
        return {
            "token_id": token_id,
            "ts_local": ts_local,
            "error": "unexpected_shape",
            "book_state_flags": ["malformed"],
        }

    # Field names vary: bids/buys, asks/sells
    raw_bids = data.get("bids") or data.get("buys") or []
    raw_asks = data.get("asks") or data.get("sells") or []
    raw_ts = data.get("timestamp") or data.get("hash")

    bids = _parse_price_levels(raw_bids, "bids", token_id)
    asks = _parse_price_levels(raw_asks, "asks", token_id)
    top = _top_of_book(bids, asks)

    flags: List[str] = []
    if not bids:
        flags.append("empty_bid")
    if not asks:
        flags.append("empty_ask")
    if top["best_bid"] is not None and top["best_ask"] is not None:
        if top["best_bid"] >= top["best_ask"]:
            flags.append("crossed")

    return {
        "token_id": token_id,
        "ts_local": ts_local,
        "bids": bids,
        "asks": asks,
        **top,
        "raw_timestamp": raw_ts,
        "book_state_flags": flags,
    }


def get_books(token_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """
    Batch-fetch order books for multiple token ids.
    Falls back to individual fetches if the batch endpoint is unavailable.
    Returns dict keyed by token_id.
    """
    if not token_ids:
        return {}

    ts_local = _now_ms()
    # Try batch POST /books first
    data = _post("/books", {"token_ids": token_ids})

    if data is not None:
        result: Dict[str, Dict[str, Any]] = {}
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                tid = item.get("asset_id") or item.get("token_id")
                if not tid:
                    log.log_parse_anomaly("clob_rest.get_books", "item", item, "no token_id in batch result item")
                    continue
                raw_bids = item.get("bids") or item.get("buys") or []
                raw_asks = item.get("asks") or item.get("sells") or []
                bids = _parse_price_levels(raw_bids, "bids", str(tid))
                asks = _parse_price_levels(raw_asks, "asks", str(tid))
                top = _top_of_book(bids, asks)
                flags: List[str] = []
                if not bids:
                    flags.append("empty_bid")
                if not asks:
                    flags.append("empty_ask")
                if top["best_bid"] is not None and top["best_ask"] is not None:
                    if top["best_bid"] >= top["best_ask"]:
                        flags.append("crossed")
                result[str(tid)] = {
                    "token_id": str(tid),
                    "ts_local": ts_local,
                    "bids": bids,
                    "asks": asks,
                    **top,
                    "raw_timestamp": item.get("timestamp"),
                    "book_state_flags": flags,
                }
            # Fill in missing token ids
            for tid in token_ids:
                if tid not in result:
                    log.log_parse_anomaly(
                        "clob_rest.get_books", "missing_token", tid, "not in batch response"
                    )
                    result[tid] = {
                        "token_id": tid,
                        "ts_local": ts_local,
                        "error": "missing_from_batch",
                        "book_state_flags": ["missing_from_batch"],
                    }
            return result
        else:
            log.log_parse_anomaly("clob_rest.get_books", "response", type(data).__name__, "expected list from batch")

    # Fallback: individual fetches
    log.log_system_event("clob_rest.get_books", detail="batch endpoint failed; falling back to individual fetches")
    return {tid: get_book(tid) for tid in token_ids}


def get_midpoint(token_id: str) -> Optional[float]:
    """
    Fetch midpoint price for a token.
    Returns float or None if unavailable.
    """
    data = _get("/midpoint", params={"token_id": token_id})
    if data is None:
        return None
    if isinstance(data, dict):
        val = data.get("mid") or data.get("midpoint")
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError) as exc:
                log.log_parse_anomaly("clob_rest.get_midpoint", "mid", val, str(exc))
    else:
        log.log_parse_anomaly("clob_rest.get_midpoint", "response", type(data).__name__, "expected dict")
    return None


# ---------------------------------------------------------------------------
# Tick size
# ---------------------------------------------------------------------------

def get_tick_size(token_id: str) -> Dict[str, Any]:
    """
    Fetch minimum tick size for a token from the CLOB endpoint.

    Returns:
      {"token_id": ..., "tick_size": float | None, "status": "ok"|"unknown"|"error",
       "ts_local": int}
    """
    ts_local = _now_ms()
    data = _get("/tick-size", params={"token_id": token_id})

    if data is None:
        return {"token_id": token_id, "tick_size": None, "status": "error", "ts_local": ts_local}

    if isinstance(data, dict):
        val = data.get("minimum_tick_size") or data.get("tick_size") or data.get("minimumTickSize")
        if val is not None:
            try:
                return {
                    "token_id": token_id,
                    "tick_size": float(val),
                    "status": "ok",
                    "ts_local": ts_local,
                    "raw": data,
                }
            except (TypeError, ValueError) as exc:
                log.log_parse_anomaly("clob_rest.get_tick_size", "tick_size", val, str(exc))
        else:
            log.log_parse_anomaly("clob_rest.get_tick_size", "tick_size", data, "no tick_size field in response")
    else:
        log.log_parse_anomaly("clob_rest.get_tick_size", "response", type(data).__name__, "expected dict")

    return {"token_id": token_id, "tick_size": None, "status": "unknown", "ts_local": ts_local}


# ---------------------------------------------------------------------------
# Fee rate — dynamic lookup, never hardcoded
# ---------------------------------------------------------------------------

def get_fee_rate(token_id: str, condition_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Attempt to discover the fee rate for a market.

    Strategy (in order):
      1. Try GET /neg-risk-market-info/{condition_id} if condition_id provided
      2. Try GET /market-info?token_id={token_id} if it exists
      3. Try GET /rewards/rates (global rates if available)

    Returns structured result:
      {
        "token_id": ...,
        "condition_id": ...,
        "fee_rate": float | None,
        "fee_rate_units": "bps" | "pct" | "unknown" | None,
        "fee_rate_source": str,
        "fees_enabled": bool | None,
        "status": "ok" | "unknown" | "error",
        "ts_local": int,
        "raw": dict | None,
      }

    IMPORTANT: If all attempts fail, returns status="unknown" with fee_rate=None.
    Do NOT substitute a hardcoded default.
    """
    ts_local = _now_ms()
    base_result = {
        "token_id": token_id,
        "condition_id": condition_id,
        "fee_rate": None,
        "fee_rate_units": None,
        "fee_rate_source": "not_found",
        "fees_enabled": None,
        "status": "unknown",
        "ts_local": ts_local,
        "raw": None,
    }

    # Attempt 1: neg-risk-market-info endpoint
    if condition_id:
        data = _get(f"/neg-risk-market-info/{condition_id}")
        if data is not None and isinstance(data, dict):
            fee_val = (
                data.get("makerBaseFee")
                or data.get("takerBaseFee")
                or data.get("feeRate")
                or data.get("fee_rate")
            )
            fees_enabled = data.get("feesEnabled") or data.get("fees_enabled")
            if fee_val is not None:
                try:
                    return {
                        **base_result,
                        "fee_rate": float(fee_val),
                        "fee_rate_units": "bps",  # assume bps until docs confirm
                        "fee_rate_source": "neg_risk_market_info",
                        "fees_enabled": bool(fees_enabled) if fees_enabled is not None else None,
                        "status": "ok",
                        "raw": data,
                    }
                except (TypeError, ValueError) as exc:
                    log.log_parse_anomaly("clob_rest.get_fee_rate", "feeRate", fee_val, str(exc))

    # Attempt 2: /market-info endpoint — skip if already known 404/405
    global _market_info_unavailable
    if not _market_info_unavailable:
        data2 = _get("/market-info", params={"token_id": token_id})
        if data2 is None:
            _market_info_unavailable = True
            log.log_system_event(
                "clob_endpoint_unavailable",
                detail="/market-info returned no data; skipping in future calls",
                extra={"endpoint": "/market-info"},
            )
        elif isinstance(data2, dict):
            fee_val = (
                data2.get("makerBaseFee")
                or data2.get("takerBaseFee")
                or data2.get("feeRate")
                or data2.get("fee_rate")
            )
            if fee_val is not None:
                try:
                    return {
                        **base_result,
                        "fee_rate": float(fee_val),
                        "fee_rate_units": "bps",
                        "fee_rate_source": "market_info_endpoint",
                        "fees_enabled": data2.get("feesEnabled"),
                        "status": "ok",
                        "raw": data2,
                    }
                except (TypeError, ValueError) as exc:
                    log.log_parse_anomaly("clob_rest.get_fee_rate", "feeRate", fee_val, str(exc))

    # Attempt 3: /rewards/rates — skip if already known 404/405
    global _rewards_rates_unavailable
    if not _rewards_rates_unavailable:
        data3 = _get("/rewards/rates")
        if data3 is None:
            _rewards_rates_unavailable = True
            log.log_system_event(
                "clob_endpoint_unavailable",
                detail="/rewards/rates returned no data; skipping in future calls",
                extra={"endpoint": "/rewards/rates"},
            )
        elif data3 is not None:
            log.log_system_event(
                "clob_fee_rates_global",
                detail=f"got global rates response (token={token_id}), inspect raw for fee structure",
                extra={"raw_snippet": str(data3)[:300]},
            )
            return {
                **base_result,
                "fee_rate_source": "global_rates_endpoint_unknown_shape",
                "status": "unknown",
                "raw": data3,
            }

    # All attempts failed
    log.log_system_event(
        "clob_fee_unknown",
        detail=f"could not discover fee rate for token={token_id}",
        level="warning",
    )
    return base_result


# ---------------------------------------------------------------------------
# Convenience: enrich a book snapshot with tick/fee metadata
# ---------------------------------------------------------------------------

def enrich_book_with_meta(
    token_id: str,
    condition_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Fetch tick size and fee rate for a token and return as a combined meta dict.
    Used for initial market setup logging.
    """
    tick = get_tick_size(token_id)
    fee = get_fee_rate(token_id, condition_id)
    return {
        "token_id": token_id,
        "tick_size": tick.get("tick_size"),
        "tick_status": tick.get("status"),
        "fee_rate": fee.get("fee_rate"),
        "fee_rate_units": fee.get("fee_rate_units"),
        "fee_rate_source": fee.get("fee_rate_source"),
        "fees_enabled": fee.get("fees_enabled"),
        "fee_status": fee.get("status"),
        "ts_local": _now_ms(),
    }
