# logger.py — JSONL writes for observations and daily summaries

import json
import logging
import os
from collections import defaultdict
from datetime import datetime, timezone

from config import OBS_LOG, DAILY_LOG, STARTUP_LOG, LOG_DIR

logger = logging.getLogger(__name__)


def _ensure_log_dir():
    os.makedirs(LOG_DIR, exist_ok=True)


# ── Startup log ────────────────────────────────────────────────────────────

def log_startup(message: str, level: str = "INFO"):
    """Append a line to startup.log."""
    _ensure_log_dir()
    ts = datetime.now(timezone.utc).isoformat()
    line = f"[{ts}] [{level}] {message}\n"
    with open(STARTUP_LOG, "a") as f:
        f.write(line)
    log_fn = getattr(logger, level.lower(), logger.info)
    log_fn("[startup] %s", message)


# ── Observation log ────────────────────────────────────────────────────────

def write_observation(record: dict):
    """
    Append one complete window record to observations.jsonl.
    Flushes immediately — no buffering.
    """
    _ensure_log_dir()
    line = json.dumps(record, separators=(",", ":")) + "\n"
    with open(OBS_LOG, "a") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())
    logger.info("Wrote observation: window_id=%s", record.get("window_id"))


def read_last_window_id() -> str | None:
    """
    Read the last logged window_id from observations.jsonl.
    Used on startup to skip already-logged windows.
    Returns None if file is missing or empty.
    """
    if not os.path.exists(OBS_LOG):
        return None
    last = None
    try:
        with open(OBS_LOG) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rec = json.loads(line)
                        last = rec.get("window_id")
                    except json.JSONDecodeError:
                        pass
    except OSError:
        pass
    return last


# ── Daily summary ──────────────────────────────────────────────────────────

def compute_and_write_daily_summary(date_str: str, records: list[dict]):
    """
    Compute daily summary from a list of observation records for date_str (YYYY-MM-DD).
    Appends one record to daily_summary.jsonl.
    """
    _ensure_log_dir()

    total_windows    = 288
    observed         = len(records)
    missing          = total_windows - observed

    snipe_triggered  = [r for r in records if r.get("paper_snipe", {}).get("triggered")]
    n_triggered      = len(snipe_triggered)
    n_correct        = sum(1 for r in snipe_triggered if r["paper_snipe"].get("paper_correct"))

    win_rate = round(n_correct / n_triggered, 4) if n_triggered else None

    pnl_total = round(
        sum(r["paper_snipe"].get("paper_pnl_per_share", 0) for r in snipe_triggered), 6
    ) if snipe_triggered else None

    avg_entry = None
    if snipe_triggered:
        avg_entry = round(
            sum(r["paper_snipe"]["entry_price"] for r in snipe_triggered) / n_triggered, 6
        )

    # Chainlink vs Binance delta
    deltas = [
        r["resolution"]["price_delta"]
        for r in records
        if r.get("resolution", {}).get("price_delta") is not None
    ]
    avg_delta = round(sum(deltas) / len(deltas), 4) if deltas else None

    # min_order_size from first record that has it
    min_order_confirmed = None
    for r in records:
        mos = r.get("min_order_size")
        if mos is not None:
            min_order_confirmed = mos
            break

    summary = {
        "summary_date":                 date_str,
        "windows_observed":             observed,
        "windows_missing":              missing,
        "snipe_setups_triggered":       n_triggered,
        "snipe_correct":                n_correct,
        "snipe_win_rate":               win_rate,
        "snipe_paper_pnl_total":        pnl_total,
        "avg_entry_price_when_triggered": avg_entry,
        "avg_chainlink_vs_binance_delta": avg_delta,
        "min_order_size_confirmed":     min_order_confirmed,
    }

    line = json.dumps(summary, separators=(",", ":")) + "\n"
    with open(DAILY_LOG, "a") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())
    logger.info("Wrote daily summary: date=%s windows=%d snipes=%d", date_str, observed, n_triggered)
    return summary


def load_today_records(date_str: str) -> list[dict]:
    """
    Load all observation records for a given UTC date from observations.jsonl.
    date_str format: YYYY-MM-DD
    """
    if not os.path.exists(OBS_LOG):
        return []
    records = []
    try:
        date_prefix = datetime.strptime(date_str, "%Y-%m-%d")
        day_start   = int(date_prefix.replace(tzinfo=timezone.utc).timestamp())
        day_end     = day_start + 86400

        with open(OBS_LOG) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    ots = rec.get("open_time_unix", 0)
                    if day_start <= ots < day_end:
                        records.append(rec)
                except json.JSONDecodeError:
                    pass
    except Exception as exc:
        logger.warning("load_today_records error: %s", exc)
    return records
