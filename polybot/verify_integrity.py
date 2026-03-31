"""
verify_integrity.py — Post-run integrity checker for polybot JSONL logs.

Usage:
    python verify_integrity.py                    # auto-detect today's log
    python verify_integrity.py logs/polybot-2026-03-31.jsonl

Checks all non-negotiable invariants:
  1. max 1 trade_opened per trade_id
  2. max 1 trade_resolved per trade_id
  3. same window_ts cannot have both trade_opened and trade_resolution_blocked
  4. run_id present in all key events
  5. pid present in all key events
  6. bot_start has fee_rate + fee_source + fee_exponent + fee_exponent_source
  7. bot_start has tick_size + tick_size_source + min_order_size + min_order_size_source
  8. trade_opened has fee_exponent_source, tick_size_source, min_order_size_source
  9. No integrity_violation events present
"""

import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


KEY_EVENTS = {"bot_start", "trade_opened", "trade_resolved", "trade_resolution_blocked"}

FAIL = "\033[91mFAIL\033[0m"
PASS = "\033[92mPASS\033[0m"
WARN = "\033[93mWARN\033[0m"


def load_log(path: Path) -> list[dict]:
    events = []
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError as e:
                print(f"  [line {i}] JSON parse error: {e}")
    return events


def check(label: str, passed: bool, detail: str = "") -> bool:
    status = PASS if passed else FAIL
    print(f"  [{status}] {label}" + (f" — {detail}" if detail else ""))
    return passed


def run_checks(events: list[dict]) -> int:
    """Returns number of failed checks."""
    failures = 0

    # Partition by event type
    opened   = [e for e in events if e.get("event") == "trade_opened"]
    resolved = [e for e in events if e.get("event") == "trade_resolved"]
    blocked  = [e for e in events if e.get("event") == "trade_resolution_blocked"]
    starts   = [e for e in events if e.get("event") == "bot_start"]
    violations = [e for e in events if e.get("event") == "integrity_violation"]
    key_events = [e for e in events if e.get("event") in KEY_EVENTS]

    print("\n=== 1. trade_opened uniqueness (max 1 per trade_id) ===")
    trade_id_opens: dict[str, list] = defaultdict(list)
    for e in opened:
        tid = e.get("trade_id", "MISSING")
        trade_id_opens[tid].append(e)
    dupes = {tid: evts for tid, evts in trade_id_opens.items() if len(evts) > 1}
    if not check("No duplicate trade_opened per trade_id", not dupes,
                 f"{len(dupes)} duplicate(s): {list(dupes.keys())[:3]}" if dupes else ""):
        failures += 1

    print("\n=== 2. trade_resolved uniqueness (max 1 per trade_id) ===")
    trade_id_resolved: dict[str, list] = defaultdict(list)
    for e in resolved:
        tid = e.get("trade_id", "MISSING")
        trade_id_resolved[tid].append(e)
    dupes_r = {tid: evts for tid, evts in trade_id_resolved.items() if len(evts) > 1}
    if not check("No duplicate trade_resolved per trade_id", not dupes_r,
                 f"{len(dupes_r)} duplicate(s): {list(dupes_r.keys())[:3]}" if dupes_r else ""):
        failures += 1

    print("\n=== 3. No blocked+opened contradiction per window_ts ===")
    opened_windows  = {e.get("window_ts") for e in opened}
    blocked_windows = {e.get("window") for e in blocked}  # note: key is "window" in blocked
    contradictions = opened_windows & blocked_windows
    if not check("No window_ts in both trade_opened and trade_resolution_blocked",
                 not contradictions,
                 f"contradicting windows: {contradictions}" if contradictions else ""):
        failures += 1

    print("\n=== 4. run_id present in all key events ===")
    missing_run_id = [e for e in key_events if not e.get("run_id")]
    if not check("run_id present in all key events", not missing_run_id,
                 f"{len(missing_run_id)} event(s) missing run_id" if missing_run_id else ""):
        failures += 1

    print("\n=== 5. pid present in all key events ===")
    missing_pid = [e for e in key_events if not e.get("pid")]
    if not check("pid present in all key events", not missing_pid,
                 f"{len(missing_pid)} event(s) missing pid" if missing_pid else ""):
        failures += 1

    print("\n=== 6. bot_start has fee provenance fields ===")
    if not starts:
        print(f"  [{WARN}] No bot_start event found — skipping fee provenance checks")
    else:
        bs = starts[-1]  # most recent
        fee_fields = ["fee_rate", "fee_source", "fee_status", "fee_exponent", "fee_exponent_source"]
        for field in fee_fields:
            if not check(f"bot_start.{field} present", field in bs,
                         f"value={bs.get(field)!r}" if field in bs else "MISSING"):
                failures += 1

    print("\n=== 7. bot_start has tick_size + min_order_size provenance ===")
    if starts:
        bs = starts[-1]
        market_fields = ["tick_size", "tick_size_source", "min_order_size", "min_order_size_source"]
        for field in market_fields:
            if not check(f"bot_start.{field} present", field in bs,
                         f"value={bs.get(field)!r}" if field in bs else "MISSING"):
                failures += 1

    print("\n=== 8. trade_opened has source labels ===")
    if not opened:
        print(f"  [{WARN}] No trade_opened events — skipping source label checks")
    else:
        source_fields = ["fee_exponent_source", "tick_size_source", "min_order_size_source", "window_ts"]
        for field in source_fields:
            missing = [e for e in opened if field not in e]
            if not check(f"trade_opened.{field} present in all opens", not missing,
                         f"{len(missing)} event(s) missing {field}" if missing else ""):
                failures += 1

    print("\n=== 9. No integrity_violation events ===")
    if not check("No integrity_violation events fired", not violations,
                 f"{len(violations)} violation(s): {[v.get('violation') for v in violations]}" if violations else ""):
        failures += 1

    return failures


def auto_detect_log() -> Path | None:
    log_dir = Path(__file__).parent / "logs"
    if not log_dir.exists():
        return None
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    candidate = log_dir / f"polybot-{date_str}.jsonl"
    if candidate.exists():
        return candidate
    # Fall back to most recent polybot log
    logs = sorted(log_dir.glob("polybot-*.jsonl"), reverse=True)
    return logs[0] if logs else None


def main() -> None:
    if len(sys.argv) > 1:
        path = Path(sys.argv[1])
    else:
        path = auto_detect_log()

    if not path or not path.exists():
        print(f"No log file found. Run the bot first, then: python verify_integrity.py")
        sys.exit(1)

    print(f"verify_integrity.py — checking: {path}")
    events = load_log(path)
    print(f"Loaded {len(events)} events.")

    failures = run_checks(events)

    print(f"\n{'='*50}")
    if failures == 0:
        print(f"  [{PASS}] ALL CHECKS PASSED ({failures} failures)")
        sys.exit(0)
    else:
        print(f"  [{FAIL}] {failures} CHECK(S) FAILED")
        sys.exit(1)


if __name__ == "__main__":
    main()
