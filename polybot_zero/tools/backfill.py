#!/usr/bin/env python3
"""
backfill.py — Post-session settlement resolver for BTC 5m backlog.

Run this AFTER the live collector session, once markets have had time to resolve.
Reads backlog.jsonl written by the runner, queries Polymarket Gamma for the
canonical winner of each window, writes settlement results to settlements.jsonl.

Usage:
  python tools/backfill.py
  python tools/backfill.py --backlog logs/backlog.jsonl --output logs/settlements.jsonl
  python tools/backfill.py --backlog logs/backlog.jsonl --min-age 120

Options:
  --backlog   Path to backlog.jsonl from live run  (default: logs/backlog.jsonl)
  --output    Path for settlement results           (default: logs/settlements.jsonl)
  --min-age   Skip records closed less than N seconds ago (default: 60)
              Polymarket typically resolves within 1-5 min of close.

Winner detection (Gamma API field priority):
  1. item["winner"]              — token_id or outcome label
  2. item["tokens"][i]["winner"] — per-token winner flag
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import time

# Allow running from inside polybot_zero/ or from its parent
_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_here))   # parent of tools/

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("backfill")

GAMMA_BASE      = "https://gamma-api.polymarket.com"
DEFAULT_BACKLOG = "logs/backlog.jsonl"
DEFAULT_OUTPUT  = "logs/settlements.jsonl"
DEFAULT_MIN_AGE = 60      # seconds since window_end before we attempt resolution
POLL_INTERVAL   = 5.0     # seconds between retries per record
MAX_POLL        = 90.0    # seconds to wait per record before giving up


# ─── Gamma resolution logic ───────────────────────────────────────────────────

async def fetch_market(session, gamma_base: str, slug: str | None, condition_id: str):
    """Fetch Gamma market item by slug (fast) or conditionId (fallback)."""
    import aiohttp
    url    = f"{gamma_base}/markets"
    params = {"slug": slug} if slug else {"conditionId": condition_id}
    async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
        if resp.status != 200:
            return None
        data = await resp.json()
    if isinstance(data, list):
        return data[0] if data else None
    if isinstance(data, dict):
        items = data.get("data") or data.get("markets")
        if isinstance(items, list):
            return items[0] if items else None
        return data
    return None


def extract_outcome(item: dict, up_token_id: str, down_token_id: str) -> str | None:
    """
    Try to extract UP/DOWN from a closed Gamma item.
    Returns None if not yet resolved / winner field absent.
    """
    if not (item.get("closed") or item.get("resolved")):
        return None

    # Method 1: direct winner field
    winner_raw = item.get("winner")
    if winner_raw is not None and str(winner_raw).strip():
        mapped = map_winner(str(winner_raw), up_token_id, down_token_id)
        if mapped:
            return mapped

    # Method 2: tokens array
    tokens_raw = item.get("tokens") or []
    if isinstance(tokens_raw, str):
        try:
            tokens_raw = json.loads(tokens_raw)
        except Exception:
            tokens_raw = []
    for tok in tokens_raw:
        if not isinstance(tok, dict):
            continue
        if tok.get("winner"):
            tok_id = str(tok.get("token_id") or tok.get("id") or "")
            if tok_id == up_token_id:
                return "UP"
            if tok_id == down_token_id:
                return "DOWN"

    return None   # closed but winner field not yet posted


def map_winner(raw: str, up_token_id: str, down_token_id: str) -> str | None:
    if raw == up_token_id:
        return "UP"
    if raw == down_token_id:
        return "DOWN"
    w = raw.lower().strip()
    if w in ("up", "yes", "1", "true"):
        return "UP"
    if w in ("down", "no", "0", "false"):
        return "DOWN"
    return None


async def resolve_one(
    session,
    rec: dict,
    gamma_base: str,
    poll_interval: float,
    max_poll: float,
) -> dict:
    """
    Resolve one backlog record. Polls until winner found or timeout.
    Returns the record dict augmented with outcome/status/settled_at.
    """
    cid          = rec["condition_id"]
    up_token_id  = rec["up_token_id"]
    down_token_id = rec["down_token_id"]
    slug         = rec.get("slug")

    deadline = time.time() + max_poll
    attempt  = 0

    while time.time() < deadline:
        attempt += 1
        try:
            item = await fetch_market(session, gamma_base, slug, cid)
            if item is not None:
                outcome = extract_outcome(item, up_token_id, down_token_id)
                if outcome is not None:
                    logger.info(
                        "[%s] outcome=%-4s slug=%s  (attempt %d)",
                        cid[:16], outcome, slug, attempt,
                    )
                    return {**rec, "status": "settled", "outcome": outcome, "settled_at": time.time()}
        except Exception as exc:
            logger.debug("[%s] Poll error (attempt %d): %s", cid[:16], attempt, exc)

        await asyncio.sleep(poll_interval)

    logger.warning("[%s] Timed out after %.0fs — UNRESOLVED", cid[:16], max_poll)
    return {**rec, "status": "unresolved", "outcome": "UNRESOLVED", "settled_at": time.time()}


# ─── Main ─────────────────────────────────────────────────────────────────────

async def run(backlog_path: str, output_path: str, min_age_secs: float) -> None:
    try:
        import aiohttp
    except ImportError:
        logger.error("aiohttp is required: pip install aiohttp")
        return

    # Read backlog
    if not os.path.exists(backlog_path):
        logger.error("Backlog not found: %s", backlog_path)
        return

    records: list[dict] = []
    with open(backlog_path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    logger.warning("Skipping bad line: %s", exc)

    pending = [r for r in records if r.get("status") == "awaiting_backfill"]
    logger.info("Backlog: %d total  %d awaiting_backfill", len(records), len(pending))

    if not pending:
        logger.info("Nothing to resolve.")
        return

    # Skip markets whose window ended too recently (Polymarket may not have settled yet)
    now = time.time()
    ready, too_fresh = [], []
    for r in pending:
        age = now - float(r.get("window_end_ts", now))
        if age >= min_age_secs:
            ready.append(r)
        else:
            too_fresh.append(r)

    if too_fresh:
        logger.info(
            "%d record(s) skipped — window ended < %.0fs ago (rerun later)",
            len(too_fresh), min_age_secs,
        )
    logger.info("Resolving %d record(s)...", len(ready))

    # Resolve all
    settled: list[dict] = []
    async with aiohttp.ClientSession() as session:
        for rec in ready:
            result = await resolve_one(
                session, rec, GAMMA_BASE, POLL_INTERVAL, MAX_POLL
            )
            settled.append(result)

    # Append to output
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "a") as f:
        for s in settled:
            f.write(json.dumps(s) + "\n")

    n_ok = sum(1 for s in settled if s["outcome"] not in ("UNRESOLVED",))
    logger.info(
        "Done: %d settled  %d resolved  %d unresolved  → %s",
        len(settled), n_ok, len(settled) - n_ok, output_path,
    )


def main() -> None:
    p = argparse.ArgumentParser(description="Backfill BTC 5m settlement from Polymarket")
    p.add_argument("--backlog",  default=DEFAULT_BACKLOG, help="Input backlog jsonl")
    p.add_argument("--output",   default=DEFAULT_OUTPUT,  help="Output settlements jsonl")
    p.add_argument("--min-age",  default=DEFAULT_MIN_AGE, type=float,
                   help="Min seconds since window_end before resolving (default: 60)")
    args = p.parse_args()
    asyncio.run(run(args.backlog, args.output, args.min_age))


if __name__ == "__main__":
    main()
