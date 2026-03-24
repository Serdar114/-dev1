"""
market_discovery.py - Fetch and persist active Polymarket BTC 5-minute markets.

ADMISSION POLICY (fail-closed, two paths):

PATH A — Canonical slug (PRIMARY signal):
  Triggered when market_slug matches the btc-updown-5m-<timestamp> family.
  The slug IS structural proof of the recurring 5-minute BTC Up/Down family.
  Requirements:
    1. condition_id present
    2. Slug matches btc-updown-5m-<timestamp> pattern
    3. No blocking contradiction in question/description
    4. Outcome labels non-blank, token mapping exact or directional (Up/Down)
    5. Timing: try all known API field names; if still absent, derive from slug timestamp.
       Slug-derived timing is logged explicitly.
    6. No window violation when API timing is present.
       Slug-derived timing always yields exactly 300 s.
  Question check: only requires BTC keyword (no "5 minute" string needed).
  The slug already proves this is the 5-minute family.

PATH B — No canonical slug:
  Triggered when no slug field is present in the API response.
  Higher bar required because the slug confirmation is absent.
  Requirements:
    1. condition_id present
    2. Question/description must contain BTC + up + down + 5-minute indicator
    3. No blocking terms
    4. Same outcome and timing checks as Path A
    5. Timing MUST come from API fields (no slug to derive from)
    6. Window must be ~300 s

ALWAYS REJECTED regardless of path:
  - market_id missing
  - token mapping confidence unknown or partial
  - slug present but does not match btc-updown-5m family
  - blocking terms present (hashprice, hashrate, before-June, before-date,
    above/below long-dated event markers)
  - fewer than 2 non-blank outcome labels
  - timing absent AND no slug to derive from (Path B)
  - window outside tolerance when timing comes from API fields

Reference canonical market family:
  URL/slug:  btc-updown-5m-<unix_timestamp>
  Question:  "Bitcoin Up or Down - 5 Minutes"
             "BTC Up or Down - 5 Minutes"
             "Bitcoin Up or Down - 9:20-9:25AM ET"  (time-range variant)
  Outcomes:  Up / Down
  Window:    300 s exactly
  Source:    Chainlink BTC/USD stream
  Tags:      Bitcoin, Recurring, Up or Down, 5M
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
# Any market whose question/description contains these is never the BTC 5m up/down family.
_BLOCKING_TERMS: List[str] = [
    # wrong market type
    "hashprice", "hash price", "hashrate", "hash rate",
    "mining difficulty", "mining reward",
    "all time high", "all-time high",
    "will bitcoin reach", "will bitcoin hit",
    "above $", "below $",           # above/below price-level bets
    # long-dated / event markets with explicit month/date markers
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

# ── Known timing field names in Polymarket CLOB API ───────────────────────────
# Ordered by preference. First non-None value wins.
_TIMING_START_FIELDS = (
    "game_start_time",
    "start_date_iso",
    "startDate",
    "start_date",
    "startTime",
)
_TIMING_END_FIELDS = (
    "end_date_iso",
    "end_date",
    "endDate",
    "endTime",
    "end_time",
)

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


# ── Slug helpers ───────────────────────────────────────────────────────────────

def _slug_is_btc_5m_updown(slug: str) -> bool:
    """
    True if slug matches the btc-updown-5m-<timestamp> canonical family.
    Canonical form: btc-updown-5m-1774358400
    Also accepts minor separator/spelling variants.
    """
    s = slug.lower().strip()
    has_btc = "btc" in s or "bitcoin" in s
    has_updown = "updown" in s or "up-down" in s or "up_down" in s
    if not has_updown:
        tokens = set(re.findall(r"\b\w+\b", s))
        has_updown = "up" in tokens and "down" in tokens
    has_5m = "5m" in s or "5min" in s or "5-min" in s
    return has_btc and has_updown and has_5m


def _extract_ts_from_slug(slug: str) -> Optional[int]:
    """
    Extract the Unix timestamp suffix from a btc-updown-5m-<timestamp> slug.
    Returns the integer timestamp, or None if not found.
    """
    m = re.search(r"btc-updown-5m-(\d{8,12})", slug.lower())
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None


# ── Timing helper ──────────────────────────────────────────────────────────────

def _get_timing(
    raw: Dict, slug: str = ""
) -> Tuple[Optional[datetime], Optional[datetime], str]:
    """
    Resolve (start_dt, end_dt, source) for a raw market object.

    Source values:
      "api_fields"        – both timestamps found in API response fields
      "slug_derived"      – both derived from slug timestamp (API fields absent)
      "partial_api_slug"  – one from API, one derived from slug
      "missing"           – timing unavailable
    """
    # Try all known API field names in preference order
    start_raw = None
    for f in _TIMING_START_FIELDS:
        v = raw.get(f)
        if v is not None:
            start_raw = v
            break

    end_raw = None
    for f in _TIMING_END_FIELDS:
        v = raw.get(f)
        if v is not None:
            end_raw = v
            break

    start_dt = _parse_ts(start_raw)
    end_dt   = _parse_ts(end_raw)

    if start_dt is not None and end_dt is not None:
        return start_dt, end_dt, "api_fields"

    # Slug-derived fallback for canonical btc-updown-5m family only
    if slug and _slug_is_btc_5m_updown(slug):
        slug_ts = _extract_ts_from_slug(slug)
        if slug_ts is not None:
            slug_start = datetime.fromtimestamp(slug_ts, tz=timezone.utc)
            slug_end   = datetime.fromtimestamp(
                slug_ts + config.WINDOW_SECONDS, tz=timezone.utc
            )
            if start_dt is None and end_dt is None:
                log.info(
                    "Timing derived from slug '%s': start=%s end=%s",
                    slug, slug_start.isoformat(), slug_end.isoformat(),
                )
                return slug_start, slug_end, "slug_derived"
            elif start_dt is None:
                log.info("Start derived from slug '%s', end from API fields", slug)
                return slug_start, end_dt, "partial_api_slug"
            else:
                log.info("End derived from slug '%s', start from API fields", slug)
                return start_dt, slug_end, "partial_api_slug"

    return start_dt, end_dt, "missing"


# ── Semantic classifiers ───────────────────────────────────────────────────────

def _question_is_btc_5m_updown(text: str) -> bool:
    """
    Full semantic check used when no canonical slug is present.
    Text must contain BTC keyword + both 'up' AND 'down' as word tokens + 5-minute indicator.
    Accepts combined question+description text.
    """
    q = text.lower()
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


def _question_has_btc_keyword(question: str) -> bool:
    """Minimal BTC presence check used alongside canonical slug (Path A)."""
    q = question.lower()
    return "btc" in q or "bitcoin" in q


def _blocking_term_present(text: str) -> Optional[str]:
    """Return the first matching blocking term found in text, or None."""
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

    mapping_confidence: "exact" | "directional" | "partial" | "unknown"
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
    Classify a raw Polymarket market for BTC 5m Up/Down admission.

    Returns:
      {
        "admit": bool,
        "rejection_reason": str | None,
        "admission_path": "slug_primary" | "question_primary" | None,
        "details": { ... }
      }
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

    # ── 1. market_id required ─────────────────────────────────────────────────
    if not condition_id:
        details["reason_detail"] = "condition_id and market_id both absent or blank"
        return {
            "admit": False, "rejection_reason": "missing_market_id",
            "admission_path": None, "details": details,
        }

    # ── 2. Determine admission path ───────────────────────────────────────────
    slug_is_canonical = bool(slug) and _slug_is_btc_5m_updown(slug)

    # Slug present but not canonical family → hard reject
    if slug and not slug_is_canonical:
        details["reason_detail"] = (
            f"slug present but does not match btc-updown-5m pattern: '{slug}'"
        )
        return {
            "admit": False, "rejection_reason": "wrong_market_semantics",
            "admission_path": None, "details": details,
        }

    combined_text = question + " " + description

    if slug_is_canonical:
        # PATH A: canonical slug is primary structural proof
        # Question only needs to not explicitly contradict the family.
        # Blocking terms remain a hard gate.
        block = _blocking_term_present(combined_text)
        if block:
            details["reason_detail"] = (
                f"canonical slug but blocking term in question/description: '{block}'"
            )
            return {
                "admit": False, "rejection_reason": "wrong_market_semantics",
                "admission_path": "slug_primary", "details": details,
            }
        admission_path = "slug_primary"

    else:
        # PATH B: no canonical slug → question must prove the family
        if not _question_is_btc_5m_updown(combined_text):
            details["reason_detail"] = (
                "no canonical slug and question/description lack "
                "required BTC + directional-pair(up/down) + 5-minute pattern"
            )
            return {
                "admit": False, "rejection_reason": "wrong_market_semantics",
                "admission_path": "question_primary", "details": details,
            }
        block = _blocking_term_present(combined_text)
        if block:
            details["reason_detail"] = f"blocking term found: '{block}'"
            return {
                "admit": False, "rejection_reason": "wrong_market_semantics",
                "admission_path": "question_primary", "details": details,
            }
        admission_path = "question_primary"

    # ── 3. Outcome labels: non-empty, ≥2 non-blank ───────────────────────────
    yes_id, no_id, confidence, outcome_labels = _extract_tokens(raw)
    details["outcome_labels"] = outcome_labels
    details["token_mapping_confidence"] = confidence

    if not outcome_labels:
        details["reason_detail"] = "tokens list absent or empty"
        return {
            "admit": False, "rejection_reason": "invalid_outcomes",
            "admission_path": admission_path, "details": details,
        }

    non_blank = [lbl for lbl in outcome_labels if lbl.strip()]
    if len(non_blank) < 2:
        details["reason_detail"] = (
            f"fewer than 2 non-blank outcome labels: {outcome_labels}"
        )
        return {
            "admit": False, "rejection_reason": "invalid_outcomes",
            "admission_path": admission_path, "details": details,
        }

    # ── 4. Token mapping: exact or directional only ───────────────────────────
    if confidence == "unknown":
        details["reason_detail"] = (
            f"unrecognised outcome labels (not Up/Down or Yes/No): {outcome_labels}"
        )
        return {
            "admit": False, "rejection_reason": "unknown_token_mapping",
            "admission_path": admission_path, "details": details,
        }
    if confidence == "partial":
        details["reason_detail"] = (
            f"partial mapping: yes_id={yes_id} no_id={no_id}"
        )
        return {
            "admit": False, "rejection_reason": "unknown_token_mapping",
            "admission_path": admission_path, "details": details,
        }

    # ── 5. Timing resolution ──────────────────────────────────────────────────
    start_dt, end_dt, timing_source = _get_timing(raw, slug=slug)
    details["start_time"]    = start_dt.isoformat() if start_dt else None
    details["end_time"]      = end_dt.isoformat() if end_dt else None
    details["timing_source"] = timing_source

    if start_dt is None:
        details["reason_detail"] = (
            "start timestamp absent/unparseable in all known fields"
            + (" and slug-derived timing unavailable" if not slug_is_canonical else "")
        )
        return {
            "admit": False, "rejection_reason": "missing_timing",
            "admission_path": admission_path, "details": details,
        }
    if end_dt is None:
        details["reason_detail"] = (
            "end timestamp absent/unparseable in all known fields"
            + (" and slug-derived timing unavailable" if not slug_is_canonical else "")
        )
        return {
            "admit": False, "rejection_reason": "missing_timing",
            "admission_path": admission_path, "details": details,
        }

    # ── 6. Window validation ──────────────────────────────────────────────────
    # Slug-derived timing always yields exactly WINDOW_SECONDS → always passes.
    # API-field timing is validated against tolerance.
    window = _window_seconds(start_dt, end_dt)
    details["window_seconds"] = window

    if timing_source == "api_fields" and (
        window is None or abs(window - config.WINDOW_SECONDS) > config.WINDOW_TOLERANCE
    ):
        details["reason_detail"] = (
            f"window={window}s expected={config.WINDOW_SECONDS}s "
            f"tolerance=±{config.WINDOW_TOLERANCE}s (from API fields)"
        )
        return {
            "admit": False, "rejection_reason": "wrong_window",
            "admission_path": admission_path, "details": details,
        }

    # ── All checks passed ─────────────────────────────────────────────────────
    details["yes_token_id"]     = yes_id
    details["no_token_id"]      = no_id
    details["admission_reason"] = (
        f"path={admission_path} slug_canonical={slug_is_canonical} "
        f"confidence={confidence} window={window:.0f}s timing={timing_source}"
    )
    return {
        "admit": True, "rejection_reason": None,
        "admission_path": admission_path, "details": details,
    }


# ── Record flattener ───────────────────────────────────────────────────────────

def _flatten(raw: Dict, fetched_utc: str) -> Dict:
    """Flatten an admitted raw market object into the canonical session record."""
    slug     = (raw.get("market_slug") or raw.get("slug") or "").strip()
    yes_id, no_id, confidence, outcome_labels = _extract_tokens(raw)
    start_dt, end_dt, timing_source = _get_timing(raw, slug=slug)
    window = _window_seconds(start_dt, end_dt)

    log.info(
        "ADMITTED: market_id=%s | path=%s | timing=%s | outcomes=%s | "
        "window=%.0fs | q=%s",
        (raw.get("condition_id") or raw.get("market_id") or "")[:16],
        "slug_primary" if (_slug_is_btc_5m_updown(slug) if slug else False) else "question_primary",
        timing_source,
        outcome_labels,
        window or 0,
        (raw.get("question") or "")[:70],
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
        "timing_source":            timing_source,
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
      data/markets/markets_<session_id>.jsonl        – admitted records
      data/markets/markets_<session_id>.csv          – admitted records (no raw_json)
      data/markets/markets_raw_<session_id>.jsonl    – every raw market seen
      data/markets/discovery_audit_<session_id>.json – full admission/rejection audit
    """
    jsonl_path = config.MARKETS_DIR / f"markets_{session_id}.jsonl"
    csv_path   = config.MARKETS_DIR / f"markets_{session_id}.csv"
    raw_path   = config.MARKETS_DIR / f"markets_raw_{session_id}.jsonl"
    audit_path = config.MARKETS_DIR / f"discovery_audit_{session_id}.json"

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

                    if _is_btc_near_miss(raw):
                        log.warning(
                            "REJECTED near-miss: reason=%s | q=%s | detail=%s",
                            reason,
                            (raw.get("question") or "")[:80],
                            classification["details"].get("reason_detail", ""),
                        )

                    bucket = audit["rejection_buckets"].get(reason)
                    if bucket is not None and len(bucket) < _MAX_EXAMPLES_PER_BUCKET:
                        bucket.append({
                            "question":      (raw.get("question") or "")[:100],
                            "condition_id":  (raw.get("condition_id") or ""),
                            "slug":          (raw.get("market_slug") or ""),
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
            log.info("    %-32s %d examples", bucket, len(examples))
            for ex in examples:
                log.info(
                    "      slug=%-30s  q=%.60s  detail=%s",
                    ex.get("slug", ""), ex["question"], ex["reason_detail"],
                )
    log.info("=" * 70)

    with open(audit_path, "w") as af:
        json.dump(audit, af, indent=2)
    log.info("Saved discovery audit to %s", audit_path)

    if not admitted_markets:
        log.warning(
            "No BTC 5-minute markets admitted. "
            "Check discovery_audit_%s.json for rejection details.", session_id
        )
        return []

    # ── Persist admitted markets ───────────────────────────────────────────────
    with open(jsonl_path, "w") as jf:
        for rec in admitted_markets:
            jf.write(json.dumps(rec) + "\n")
    log.info("Saved %d admitted markets to %s", len(admitted_markets), jsonl_path)

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
            f"timing={m['timing_source']:18s}  "
            f"window={m['window_seconds']}s  "
            f"confidence={m['token_mapping_confidence']}  "
            f"q={m['question'][:55]}"
        )
    print(f"{'='*70}")
