"""
market_discovery.py - Fetch and persist active Polymarket BTC 5-minute markets.

ADMISSION POLICY (fail-closed):
A market is admitted ONLY when ALL of the following pass:

  1. market_id (condition_id or market_id) is non-empty
  2. Question semantics match the BTC 5-minute Up/Down recurring pattern:
       - contains BTC keyword (btc / bitcoin)
       - contains BOTH 'up' AND 'down' as word tokens (directional pair)
       - contains a 5-minute indicator
  3. If a slug field is present in the API response, it must also match the
     BTC 5-minute Up/Down slug pattern (btc-updown-5m-<ts> family).
     A slug that doesn't match is a hard reject — not ignored.
  4. No blocking terms present in question or description
     (hashprice, hashrate, long-dated event markers, etc.)
  5. Outcome labels list is non-empty and has at least 2 non-blank labels
  6. Token mapping confidence is 'exact' or 'directional'.
     'unknown' and 'partial' → reject.
  7. Both start and end timestamps parse successfully.
     Missing or unparseable timing → reject (never 'assume OK').
  8. Window duration (end − start) is within WINDOW_TOLERANCE of WINDOW_SECONDS (300 s).

Any single failure → reject with a labelled reason bucket.
No partial admission. No 'window unknown but pass anyway'.

Reference canonical market family:
  URL slug:  btc-updown-5m-<unix_timestamp>
  Question:  "Bitcoin Up or Down - 5 Minutes"
  Outcomes:  Up / Down
  Window:    300 s
  Source:    Chainlink BTC/USD stream
"""

import csv
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

import config

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format=config.LOG_FORMAT, datefmt=config.LOG_DATEFMT)


# ── Blocking terms ─────────────────────────────────────────────────────────────
# Markets whose question/description contains any of these are never BTC 5m up/down.
_BLOCKING_TERMS: List[str] = [
    # wrong market type
    "hashprice", "hash price", "hashrate", "hash rate",
    "mining difficulty", "mining reward",
    "all time high", "all-time high",
    "will bitcoin reach", "will bitcoin hit",
    # long-dated / event markets
    "before june", "before july", "before august", "before september",
    "before october", "before november", "before december",
    "before may", "before april", "before march",
    "before february", "before january",
    "by june", "by july", "by august", "by september",
    "by october", "by november", "by december",
    "by may", "by april",
    "end of year", "by year end", "by end of",
    "this year", "in 2025", "in 2026", "in 2027",
]

# ── Rejection reason buckets ───────────────────────────────────────────────────
_REJECTION_BUCKETS: Tuple[str, ...] = (
    "missing_market_id",
    "wrong_market_semantics",
    "invalid_outcomes",
    "unknown_token_mapping",
    "missing_timing",
    "wrong_window",
    "inactive_or_closed",
)

# Maximum rejection examples stored per bucket in the audit
_MAX_EXAMPLES_PER_BUCKET = 5


# ── helpers ────────────────────────────────────────────────────────────────────

def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _parse_ts(val: Any) -> Optional[datetime]:
    """Parse a timestamp that may be unix epoch (int/float) or ISO string."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        try:
            return datetime.fromtimestamp(float(val), tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    if isinstance(val, str):
        val = val.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(val)
        except ValueError:
            return None
    return None


def _window_seconds(
    start: Optional[datetime], end: Optional[datetime]
) -> Optional[float]:
    if start is None or end is None:
        return None
    return (end - start).total_seconds()


# ── Structural classifiers ─────────────────────────────────────────────────────

def _slug_is_btc_5m_updown(slug: str) -> bool:
    """
    True if slug matches the BTC 5-minute Up/Down market family.
    Canonical form: btc-updown-5m-<timestamp>
    Also accepts minor separator variants.
    """
    s = slug.lower().strip()
    has_btc = "btc" in s or "bitcoin" in s
    # compound 'updown' or separator variants
    has_updown = "updown" in s or "up-down" in s or "up_down" in s
    if not has_updown:
        # fallback: separate word tokens for 'up' and 'down'
        tokens = set(re.findall(r"\b\w+\b", s))
        has_updown = "up" in tokens and "down" in tokens
    has_5m = "5m" in s or "5min" in s or "5-min" in s
    return has_btc and has_updown and has_5m


def _question_is_btc_5m_updown(question: str) -> bool:
    """
    True if question semantically matches the BTC 5-minute Up/Down recurring pattern.

    Requires ALL of:
      - BTC keyword (btc / bitcoin)
      - Both 'up' AND 'down' as whole-word tokens (directional pair)
      - A 5-minute indicator
    """
    q = question.lower()
    if not ("btc" in q or "bitcoin" in q):
        return False
    words = set(re.findall(r"\b\w+\b", q))
    if "up" not in words or "down" not in words:
        return False
    has_5min = (
        "5-minute" in q
        or "5 minute" in q   # also matches "5 minutes"
        or "5min" in q
        or "5 min" in q
        or "5m" in q
    )
    return has_5min


def _blocking_term_present(text: str) -> Optional[str]:
    """Return the first matching blocking term, or None."""
    t = text.lower()
    for term in _BLOCKING_TERMS:
        if term in t:
            return term
    return None


def _is_btc_near_miss(raw: Dict) -> bool:
    """True if market mentions BTC/Bitcoin (worth logging as a near-miss)."""
    q = (raw.get("question") or "").lower()
    return "btc" in q or "bitcoin" in q


# ── Token extraction ───────────────────────────────────────────────────────────

def _extract_tokens(
    raw: Dict,
) -> Tuple[Optional[str], Optional[str], str, List[str]]:
    """
    Return (yes_token_id, no_token_id, mapping_confidence, outcome_labels).

    BTC 5-minute markets use "Up"/"Down" labels:
      Up   → yes_token_id   (price goes up = YES wins)
      Down → no_token_id    (price goes down = NO wins)

    mapping_confidence values:
      "exact"       – labels were literally "Yes"/"No"
      "directional" – labels were "Up"/"Down" mapped to YES/NO
      "partial"     – only one side found
      "unknown"     – no recognisable outcome labels
    """
    tokens = raw.get("tokens", []) or []
    yes_id: Optional[str] = None
    no_id:  Optional[str] = None
    outcome_labels: List[str] = []

    for tok in tokens:
        outcome = (tok.get("outcome") or "").strip()
        outcome_labels.append(outcome)
        ou = outcome.upper()
        if ou == "YES":
            yes_id = tok.get("token_id")
        elif ou == "NO":
            no_id = tok.get("token_id")
        elif ou == "UP":
            yes_id = tok.get("token_id")
        elif ou == "DOWN":
            no_id = tok.get("token_id")

    uppers = {o.upper() for o in outcome_labels if o.strip()}
    if {"YES", "NO"} <= uppers:
        confidence = "exact"
    elif {"UP", "DOWN"} <= uppers:
        confidence = "directional"
    elif yes_id or no_id:
        confidence = "partial"
    else:
        confidence = "unknown"

    return yes_id, no_id, confidence, outcome_labels


# ── Admission classifier ───────────────────────────────────────────────────────

def _classify_market(raw: Dict) -> Dict:
    """
    Classify a raw Polymarket market object for BTC 5m Up/Down admission.

    Returns:
      {
        "admit": bool,
        "rejection_reason": str | None,   # one of _REJECTION_BUCKETS or None
        "details": {
          "question": str,
          "slug": str,
          "condition_id": str,
          "outcome_labels": list,
          "token_mapping_confidence": str,
          "start_time": str | None,
          "end_time": str | None,
          "window_seconds": float | None,
          "reason_detail": str,           # human-readable rejection detail
          ... additional fields on success ...
        }
      }

    Fail-closed: every check must pass; failure at any step is an immediate reject.
    """
    question     = (raw.get("question") or "").strip()
    description  = (raw.get("description") or "").strip()
    slug         = (raw.get("market_slug") or raw.get("slug") or "").strip()
    condition_id = (raw.get("condition_id") or raw.get("market_id") or "").strip()

    details: Dict[str, Any] = {
        "question":     question[:120],
        "slug":         slug,
        "condition_id": condition_id,
        "reason_detail": "",
    }

    # ── 1. market_id must exist ────────────────────────────────────────────────
    if not condition_id:
        details["reason_detail"] = "condition_id and market_id both absent or blank"
        return {"admit": False, "rejection_reason": "missing_market_id", "details": details}

    # ── 2. Question must match BTC 5m Up/Down semantic pattern ────────────────
    if not _question_is_btc_5m_updown(question):
        details["reason_detail"] = (
            "question lacks required BTC + directional-pair(up/down) + 5-minute pattern"
        )
        return {"admit": False, "rejection_reason": "wrong_market_semantics", "details": details}

    # ── 3. Slug: if present, must match BTC 5m Up/Down pattern ───────────────
    if slug and not _slug_is_btc_5m_updown(slug):
        details["reason_detail"] = f"slug present but does not match btc-updown-5m pattern: '{slug}'"
        return {"admit": False, "rejection_reason": "wrong_market_semantics", "details": details}

    # ── 4. Blocking terms ─────────────────────────────────────────────────────
    combined_text = question + " " + description
    block = _blocking_term_present(combined_text)
    if block:
        details["reason_detail"] = f"blocking term found: '{block}'"
        return {"admit": False, "rejection_reason": "wrong_market_semantics", "details": details}

    # ── 5. Outcome labels: non-empty, at least 2 non-blank ───────────────────
    yes_id, no_id, confidence, outcome_labels = _extract_tokens(raw)
    details["outcome_labels"] = outcome_labels
    details["token_mapping_confidence"] = confidence

    if not outcome_labels:
        details["reason_detail"] = "tokens list absent or empty"
        return {"admit": False, "rejection_reason": "invalid_outcomes", "details": details}

    non_blank = [lbl for lbl in outcome_labels if lbl.strip()]
    if len(non_blank) < 2:
        details["reason_detail"] = f"fewer than 2 non-blank outcome labels: {outcome_labels}"
        return {"admit": False, "rejection_reason": "invalid_outcomes", "details": details}

    # ── 6. Token mapping: exact or directional only ───────────────────────────
    if confidence == "unknown":
        details["reason_detail"] = f"unrecognised outcome labels (not Up/Down or Yes/No): {outcome_labels}"
        return {"admit": False, "rejection_reason": "unknown_token_mapping", "details": details}
    if confidence == "partial":
        details["reason_detail"] = f"partial mapping: yes_id={yes_id} no_id={no_id}"
        return {"admit": False, "rejection_reason": "unknown_token_mapping", "details": details}

    # ── 7. Timing: both timestamps required ───────────────────────────────────
    start_raw = raw.get("game_start_time") or raw.get("start_date_iso")
    end_raw   = raw.get("end_date_iso") or raw.get("end_date")
    start_dt  = _parse_ts(start_raw)
    end_dt    = _parse_ts(end_raw)

    details["start_time"] = start_dt.isoformat() if start_dt else None
    details["end_time"]   = end_dt.isoformat() if end_dt else None

    if start_dt is None:
        details["reason_detail"] = (
            f"start timestamp absent or unparseable: raw_value={start_raw!r}. "
            "Markets without timing are rejected — 'window unknown but pass anyway' is forbidden."
        )
        return {"admit": False, "rejection_reason": "missing_timing", "details": details}
    if end_dt is None:
        details["reason_detail"] = (
            f"end timestamp absent or unparseable: raw_value={end_raw!r}. "
            "Markets without timing are rejected."
        )
        return {"admit": False, "rejection_reason": "missing_timing", "details": details}

    # ── 8. Window duration ────────────────────────────────────────────────────
    window = _window_seconds(start_dt, end_dt)
    details["window_seconds"] = window

    if window is None or abs(window - config.WINDOW_SECONDS) > config.WINDOW_TOLERANCE:
        details["reason_detail"] = (
            f"window={window}s expected={config.WINDOW_SECONDS}s "
            f"tolerance=±{config.WINDOW_TOLERANCE}s"
        )
        return {"admit": False, "rejection_reason": "wrong_window", "details": details}

    # ── All checks passed ─────────────────────────────────────────────────────
    details["yes_token_id"]         = yes_id
    details["no_token_id"]          = no_id
    details["slug_checked"]         = bool(slug)
    details["slug_matched"]         = bool(slug) or None  # None = not checked (absent)
    details["question_match"]       = True
    details["admission_reason"]     = (
        f"question_match=True slug_match={'yes' if slug else 'n/a'} "
        f"confidence={confidence} window={window:.0f}s"
    )
    return {"admit": True, "rejection_reason": None, "details": details}


# ── Record flattener ───────────────────────────────────────────────────────────

def _flatten(raw: Dict, fetched_utc: str) -> Dict:
    """Flatten an admitted raw market object into the canonical session record."""
    start_dt = _parse_ts(raw.get("game_start_time") or raw.get("start_date_iso"))
    end_dt   = _parse_ts(raw.get("end_date_iso") or raw.get("end_date"))
    window   = _window_seconds(start_dt, end_dt)
    yes_id, no_id, confidence, outcome_labels = _extract_tokens(raw)

    log.info(
        "ADMITTED: market_id=%s | question=%s | outcomes=%s | "
        "yes_token=%s | no_token=%s | start=%s | end=%s | window=%.0fs | confidence=%s",
        (raw.get("condition_id") or raw.get("market_id") or "")[:16],
        (raw.get("question") or "")[:70],
        outcome_labels,
        (yes_id or "")[:16],
        (no_id or "")[:16],
        start_dt.isoformat() if start_dt else None,
        end_dt.isoformat() if end_dt else None,
        window or 0,
        confidence,
    )

    tick_size    = raw.get("minimum_tick_size") or raw.get("tick_size")
    min_order_sz = raw.get("min_order_size")

    if min_order_sz is None:
        log.warning(
            "min_order_size not present for market %s – "
            "downstream will use DEFAULT_MIN_SHARES=%s",
            raw.get("condition_id"), config.DEFAULT_MIN_SHARES,
        )

    return {
        "market_id":                raw.get("condition_id") or raw.get("market_id"),
        "condition_id":             raw.get("condition_id"),
        "question":                 raw.get("question", ""),
        "category":                 raw.get("category", ""),
        "yes_token_id":             yes_id,
        "no_token_id":              no_id,
        "token_mapping_confidence": confidence,
        "outcome_labels":           outcome_labels,
        "start_time_utc":           start_dt.isoformat() if start_dt else None,
        "end_time_utc":             end_dt.isoformat() if end_dt else None,
        "window_seconds":           window,
        "tick_size":                tick_size,
        "min_order_size":           min_order_sz,
        "min_order_size_source":    "market_data" if min_order_sz is not None else "missing",
        "min_tick_size":            raw.get("minimum_tick_size"),
        "status":                   raw.get("status", "unknown"),
        "active":                   raw.get("active", None),
        "closed":                   raw.get("closed", None),
        "archived":                 raw.get("archived", None),
        "outcome_yes_label":        (raw.get("outcome_prices") or [None])[0],
        "outcome_no_label":         (raw.get("outcome_prices") or [None, None])[1]
                                    if len(raw.get("outcome_prices") or []) > 1 else None,
        "last_fetched_utc":         fetched_utc,
        "raw_json":                 json.dumps(raw),
    }


# ── REST pagination ────────────────────────────────────────────────────────────

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
            log.warning(
                "CLOB /markets attempt %d/%d failed: %s",
                attempt, config.HTTP_MAX_RETRIES, exc,
            )
            if attempt < config.HTTP_MAX_RETRIES:
                time.sleep(config.HTTP_RETRY_BACKOFF_S * attempt)
    raise RuntimeError("Failed to fetch markets after retries")


# ── Main discovery entry-point ─────────────────────────────────────────────────

def fetch_all_markets(session_id: str) -> List[Dict]:
    """
    Paginate through all Polymarket markets, apply fail-closed BTC 5m admission,
    persist output files, return list of admitted flattened records.

    Output files:
      data/markets/markets_<session_id>.jsonl        – admitted records (one per line)
      data/markets/markets_<session_id>.csv          – admitted records (no raw_json)
      data/markets/markets_raw_<session_id>.jsonl    – every raw market seen
      data/markets/discovery_audit_<session_id>.json – full admission/rejection audit
    """
    jsonl_path  = config.MARKETS_DIR / f"markets_{session_id}.jsonl"
    csv_path    = config.MARKETS_DIR / f"markets_{session_id}.csv"
    raw_path    = config.MARKETS_DIR / f"markets_raw_{session_id}.jsonl"
    audit_path  = config.MARKETS_DIR / f"discovery_audit_{session_id}.json"

    admitted_markets: List[Dict] = []
    audit: Dict[str, Any] = {
        "session_id":        session_id,
        "total_scanned":     0,
        "total_admitted":    0,
        "total_rejected":    0,
        "admitted_market_ids": [],
        "rejection_buckets": {b: [] for b in _REJECTION_BUCKETS},
    }

    next_cursor: Optional[str] = None
    log.info("Starting market discovery  session=%s", session_id)

    with open(raw_path, "w") as raw_fh:
        while True:
            page = _fetch_markets_page(next_cursor)
            items = page.get("data", [])
            if not items:
                log.info("Empty page – discovery complete")
                break

            fetched_utc = _utcnow_iso()
            for raw in items:
                audit["total_scanned"] += 1
                raw_fh.write(json.dumps(raw) + "\n")

                classification = _classify_market(raw)

                if classification["admit"]:
                    flat = _flatten(raw, fetched_utc)
                    admitted_markets.append(flat)
                    audit["total_admitted"] += 1
                    audit["admitted_market_ids"].append(flat["market_id"])

                else:
                    reason = classification["rejection_reason"]
                    audit["total_rejected"] += 1

                    # Log BTC near-misses (Bitcoin-related markets that were rejected)
                    if _is_btc_near_miss(raw):
                        log.warning(
                            "REJECTED near-miss: reason=%s | question=%s | detail=%s",
                            reason,
                            (raw.get("question") or "")[:80],
                            classification["details"].get("reason_detail", ""),
                        )

                    # Record examples per bucket (capped)
                    bucket = audit["rejection_buckets"].get(reason)
                    if bucket is not None and len(bucket) < _MAX_EXAMPLES_PER_BUCKET:
                        bucket.append({
                            "question":      (raw.get("question") or "")[:100],
                            "condition_id":  (raw.get("condition_id") or ""),
                            "reason_detail": classification["details"].get("reason_detail", ""),
                        })

            next_cursor = page.get("next_cursor")
            done = not next_cursor or next_cursor in ("", "LTE=")
            log.debug(
                "Page done: scanned=%d admitted=%d rejected=%d next_cursor=%s",
                audit["total_scanned"], audit["total_admitted"],
                audit["total_rejected"], next_cursor,
            )
            if done:
                break
            time.sleep(0.25)

    # ── Admission / rejection audit summary ────────────────────────────────────
    log.info("=" * 70)
    log.info("DISCOVERY AUDIT  session=%s", session_id)
    log.info("  total_scanned : %d", audit["total_scanned"])
    log.info("  total_admitted: %d", audit["total_admitted"])
    log.info("  total_rejected: %d", audit["total_rejected"])
    log.info("  admitted_ids  : %s", audit["admitted_market_ids"])
    log.info("  rejection breakdown:")
    for bucket, examples in audit["rejection_buckets"].items():
        if examples:
            log.info("    %-30s %d examples", bucket, len(examples))
            for ex in examples:
                log.info("      q=%.70s  detail=%s", ex["question"], ex["reason_detail"])
    log.info("=" * 70)

    # ── Save audit JSON ────────────────────────────────────────────────────────
    with open(audit_path, "w") as af:
        json.dump(audit, af, indent=2)
    log.info("Saved discovery audit to %s", audit_path)

    if not admitted_markets:
        log.warning("No BTC 5-minute markets admitted – check filters or market availability")
        return []

    # ── Persist admitted markets JSONL ─────────────────────────────────────────
    with open(jsonl_path, "w") as jf:
        for rec in admitted_markets:
            jf.write(json.dumps(rec) + "\n")
    log.info("Saved %d admitted markets to %s", len(admitted_markets), jsonl_path)

    # ── Persist admitted markets CSV ───────────────────────────────────────────
    csv_fields = [k for k in admitted_markets[0].keys() if k != "raw_json"]
    with open(csv_path, "w", newline="") as cf:
        writer = csv.DictWriter(cf, fieldnames=csv_fields)
        writer.writeheader()
        for rec in admitted_markets:
            writer.writerow({k: rec[k] for k in csv_fields})
    log.info("Saved CSV to %s", csv_path)

    return admitted_markets


def load_markets(session_id: str) -> List[Dict]:
    """Load previously persisted admitted market records for a session."""
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


# ── CLI ────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    session_id = (
        sys.argv[1]
        if len(sys.argv) > 1
        else datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    )
    markets = fetch_all_markets(session_id)
    print(f"\n{'='*70}")
    print(f"Discovery complete: {len(markets)} BTC 5-minute markets admitted")
    for m in markets:
        print(
            f"  {(m['market_id'] or '')[:16]}…  "
            f"status={m['status']:10s}  "
            f"window={m['window_seconds']}s  "
            f"confidence={m['token_mapping_confidence']}  "
            f"q={m['question'][:55]}"
        )
    print(f"{'='*70}")
