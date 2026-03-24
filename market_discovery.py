"""
market_discovery.py - Fetch and persist active Polymarket BTC 5-minute markets.

Outputs:
  data/markets/markets_<session_id>.jsonl  – one JSON object per market
  data/markets/markets_<session_id>.csv    – flattened summary for quick review

What we record per market:
  market_id, condition_id, question, category,
  yes_token_id, no_token_id,
  start_time_utc, end_time_utc, window_seconds,
  tick_size, min_order_size, min_tick_size,
  status, active, closed, archived,
  outcome_yes_label, outcome_no_label,
  last_fetched_utc

Filtering logic:
  1. question must contain a BTC keyword (case-insensitive)
  2. question or description must contain a 5-minute keyword
  3. market duration (end - start) must be within WINDOW_TOLERANCE of WINDOW_SECONDS
  4. status must be "active" or unknown (we surface everything, flag non-active)

No strategy assumptions. This is pure discovery.
"""

import csv
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

import config

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format=config.LOG_FORMAT, datefmt=config.LOG_DATEFMT)


# ── helpers ───────────────────────────────────────────────────────────────────

def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _parse_ts(val: Any) -> Optional[datetime]:
    """Parse a timestamp that may be unix epoch (int/float) or ISO string."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return datetime.fromtimestamp(float(val), tz=timezone.utc)
    if isinstance(val, str):
        val = val.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(val)
        except ValueError:
            return None
    return None


def _window_seconds(start: Optional[datetime], end: Optional[datetime]) -> Optional[float]:
    if start is None or end is None:
        return None
    return (end - start).total_seconds()


def _contains_any(text: str, keywords: List[str]) -> bool:
    lower = text.lower()
    return any(kw in lower for kw in keywords)


def _is_btc_5m_market(raw: Dict) -> bool:
    """Return True if this market looks like a BTC 5-minute up/down market."""
    question    = raw.get("question", "") or ""
    description = raw.get("description", "") or ""
    combined    = question + " " + description

    if not _contains_any(combined, config.BTC_KEYWORDS):
        return False

    if not _contains_any(combined, config.FIVEMIN_KEYWORDS):
        return False

    # Duration check
    start_dt = _parse_ts(raw.get("game_start_time") or raw.get("start_date_iso"))
    end_dt   = _parse_ts(raw.get("end_date_iso") or raw.get("end_date"))
    window   = _window_seconds(start_dt, end_dt)
    if window is not None:
        if abs(window - config.WINDOW_SECONDS) > config.WINDOW_TOLERANCE:
            log.debug("Skipping market %s: window %.0fs outside tolerance", raw.get("condition_id"), window)
            return False

    return True


def _extract_tokens(raw: Dict) -> tuple:
    """
    Return (yes_token_id, no_token_id, mapping_confidence, outcome_labels).

    BTC 5-minute markets use outcome labels "Up"/"Down" rather than "Yes"/"No".
    We map: Up  -> yes_token_id (price goes up  = YES wins)
            Down -> no_token_id  (price goes down = NO  wins)

    mapping_confidence values:
      "exact"       – outcomes were literally "Yes"/"No"
      "directional" – outcomes were "Up"/"Down" mapped to YES/NO
      "partial"     – only one side found
      "unknown"     – no recognisable outcome labels; token IDs will be None
    """
    tokens = raw.get("tokens", []) or []
    yes_id = no_id = None
    outcome_labels = []

    for tok in tokens:
        outcome = (tok.get("outcome") or "").strip()
        outcome_labels.append(outcome)
        ou = outcome.upper()
        if ou in ("YES",):
            yes_id = tok.get("token_id")
        elif ou in ("NO",):
            no_id = tok.get("token_id")
        elif ou in ("UP",):
            yes_id = tok.get("token_id")   # Up = YES in directional markets
        elif ou in ("DOWN",):
            no_id = tok.get("token_id")    # Down = NO in directional markets

    # Assess confidence
    uppers = [o.upper() for o in outcome_labels]
    if set(uppers) >= {"YES", "NO"}:
        confidence = "exact"
    elif set(uppers) >= {"UP", "DOWN"}:
        confidence = "directional"
    elif yes_id or no_id:
        confidence = "partial"
    else:
        confidence = "unknown"

    return yes_id, no_id, confidence, outcome_labels


def _flatten(raw: Dict, fetched_utc: str) -> Dict:
    """Flatten a raw Polymarket market object into our canonical record."""
    start_dt = _parse_ts(raw.get("game_start_time") or raw.get("start_date_iso"))
    end_dt   = _parse_ts(raw.get("end_date_iso") or raw.get("end_date"))
    window   = _window_seconds(start_dt, end_dt)
    yes_id, no_id, token_mapping_confidence, outcome_labels = _extract_tokens(raw)

    # Warn loudly on unknown mapping – downstream callers depend on correct token IDs
    if token_mapping_confidence == "unknown":
        log.error(
            "TOKEN MAPPING UNKNOWN for market %s – yes/no token IDs will be None. "
            "Raw outcome labels: %s",
            raw.get("condition_id"), outcome_labels,
        )
    elif token_mapping_confidence == "partial":
        log.warning(
            "TOKEN MAPPING PARTIAL for market %s – one side missing. "
            "Raw outcome labels: %s",
            raw.get("condition_id"), outcome_labels,
        )
    else:
        log.info(
            "TOKEN MAPPING: market=%s  confidence=%s  labels=%s  "
            "yes_token=%s  no_token=%s",
            (raw.get("condition_id") or "")[:12],
            token_mapping_confidence,
            outcome_labels,
            (yes_id or "")[:12],
            (no_id or "")[:12],
        )

    # Prefer per-token tick_size/min_size if available; otherwise fall back to market level
    tick_size     = raw.get("minimum_tick_size") or raw.get("tick_size")
    min_order_sz  = raw.get("min_order_size")

    # Log explicitly whether min_order_size came from API or will use default
    if min_order_sz is None:
        log.warning(
            "min_order_size not present in market data for %s – "
            "downstream will fall back to DEFAULT_MIN_SHARES=%s",
            raw.get("condition_id"), config.DEFAULT_MIN_SHARES,
        )

    return {
        "market_id":               raw.get("condition_id") or raw.get("market_id"),
        "condition_id":            raw.get("condition_id"),
        "question":                raw.get("question", ""),
        "category":                raw.get("category", ""),
        "yes_token_id":            yes_id,
        "no_token_id":             no_id,
        "token_mapping_confidence": token_mapping_confidence,
        "outcome_labels":          outcome_labels,
        "start_time_utc":          start_dt.isoformat() if start_dt else None,
        "end_time_utc":            end_dt.isoformat() if end_dt else None,
        "window_seconds":          window,
        "tick_size":               tick_size,
        "min_order_size":          min_order_sz,
        "min_order_size_source":   "market_data" if min_order_sz is not None else "missing",
        "min_tick_size":           raw.get("minimum_tick_size"),
        "status":                  raw.get("status", "unknown"),
        "active":                  raw.get("active", None),
        "closed":                  raw.get("closed", None),
        "archived":                raw.get("archived", None),
        "outcome_yes_label":       raw.get("outcome_prices", [None])[0] if raw.get("outcome_prices") else None,
        "outcome_no_label":        (raw.get("outcome_prices") or [None, None])[1] if len(raw.get("outcome_prices") or []) > 1 else None,
        "last_fetched_utc":        fetched_utc,
        "raw_json":                json.dumps(raw),
    }


# ── REST pagination ───────────────────────────────────────────────────────────

def _fetch_markets_page(next_cursor: Optional[str] = None) -> Dict:
    """Fetch one page of markets from the CLOB. Returns raw API response dict."""
    params: Dict[str, Any] = {}
    if next_cursor and next_cursor not in ("", "LTE="):
        params["next_cursor"] = next_cursor

    for attempt in range(1, config.HTTP_MAX_RETRIES + 1):
        try:
            resp = requests.get(
                config.CLOB_MARKETS,
                params=params,
                timeout=config.HTTP_TIMEOUT_S,
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            log.warning("CLOB /markets attempt %d/%d failed: %s", attempt, config.HTTP_MAX_RETRIES, exc)
            if attempt < config.HTTP_MAX_RETRIES:
                time.sleep(config.HTTP_RETRY_BACKOFF_S * attempt)
    raise RuntimeError("Failed to fetch markets after retries")


def fetch_all_markets(session_id: str) -> List[Dict]:
    """
    Paginate through ALL Polymarket markets, filter for BTC 5-minute,
    persist both JSONL and CSV, return list of flattened records.
    """
    jsonl_path = config.MARKETS_DIR / f"markets_{session_id}.jsonl"
    csv_path   = config.MARKETS_DIR / f"markets_{session_id}.csv"
    raw_path   = config.MARKETS_DIR / f"markets_raw_{session_id}.jsonl"

    btc_markets: List[Dict] = []
    all_seen = 0
    next_cursor: Optional[str] = None

    log.info("Starting market discovery session=%s", session_id)

    with open(raw_path, "w") as raw_fh:
        while True:
            page = _fetch_markets_page(next_cursor)
            items = page.get("data", [])
            if not items:
                log.info("Empty page – discovery complete")
                break

            fetched_utc = _utcnow_iso()
            for raw in items:
                all_seen += 1
                # Always persist raw for forensics
                raw_fh.write(json.dumps(raw) + "\n")

                if _is_btc_5m_market(raw):
                    flat = _flatten(raw, fetched_utc)
                    btc_markets.append(flat)
                    log.info("BTC 5m market found: %s | %s | status=%s | window=%.0fs",
                             flat["market_id"], flat["question"][:60],
                             flat["status"], flat["window_seconds"] or -1)

            next_cursor = page.get("next_cursor")
            done = not next_cursor or next_cursor in ("", "LTE=")
            log.debug("Page done: seen=%d total, btc_5m=%d, next_cursor=%s",
                      all_seen, len(btc_markets), next_cursor)
            if done:
                break
            # polite pacing
            time.sleep(0.25)

    log.info("Discovery complete: %d total markets scanned, %d BTC 5m found",
             all_seen, len(btc_markets))

    if not btc_markets:
        log.warning("No BTC 5-minute markets found – check keyword filters or market availability")
        return []

    # ── Persist JSONL (one record per line) ───────────────────────────────────
    with open(jsonl_path, "w") as jf:
        for rec in btc_markets:
            jf.write(json.dumps(rec) + "\n")
    log.info("Saved %d markets to %s", len(btc_markets), jsonl_path)

    # ── Persist CSV (no raw_json column to keep it human-readable) ────────────
    csv_fields = [k for k in btc_markets[0].keys() if k != "raw_json"]
    with open(csv_path, "w", newline="") as cf:
        writer = csv.DictWriter(cf, fieldnames=csv_fields)
        writer.writeheader()
        for rec in btc_markets:
            row = {k: rec[k] for k in csv_fields}
            writer.writerow(row)
    log.info("Saved CSV to %s", csv_path)

    return btc_markets


def load_markets(session_id: str) -> List[Dict]:
    """Load previously persisted market records for a session."""
    jsonl_path = config.MARKETS_DIR / f"markets_{session_id}.jsonl"
    if not jsonl_path.exists():
        raise FileNotFoundError(f"No market file for session {session_id}: {jsonl_path}")
    records = []
    with open(jsonl_path) as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


# ── CLI ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    session_id = sys.argv[1] if len(sys.argv) > 1 else datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    markets = fetch_all_markets(session_id)
    print(f"\n{'='*60}")
    print(f"Found {len(markets)} BTC 5-minute markets")
    for m in markets:
        print(f"  {m['market_id'][:12]}…  status={m['status']:10s}  "
              f"window={m['window_seconds']}s  "
              f"question={m['question'][:50]}")
    print(f"{'='*60}")
