"""
verify_integrity.py — Post-run integrity checker for polybot JSONL logs.

Usage:
    python verify_integrity.py                    # auto-detect today's log
    python verify_integrity.py logs/polybot-2026-03-31.jsonl

Checks:
  1.  max 1 trade_opened per trade_id
  2.  max 1 trade_resolved per trade_id
  3.  no trade_id appears in trade_resolution_blocked AFTER it has been trade_resolved
  4.  run_id present in all key events
  5.  pid present in all key events
  6.  bot_start has fee_rate + fee_source + fee_exponent + fee_exponent_source
  7.  bot_start has tick_size + tick_size_source + min_order_size + min_order_size_source
  8.  trade_opened has fee_exponent_source, tick_size_source, min_order_size_source, window_ts
  9.  No integrity_violation events
  10. max 1 first_cross_detected per window_ts
  11. max 1 window_decision per window_ts
  12. No contradictory window_decision outcomes (opened + blocked same window_ts)
"""

import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


KEY_EVENTS = {
    "bot_start", "trade_opened", "trade_resolved",
    "trade_resolution_blocked", "first_cross_detected", "window_decision",
}

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

    opened   = [e for e in events if e.get("event") == "trade_opened"]
    resolved = [e for e in events if e.get("event") == "trade_resolved"]
    blocked  = [e for e in events if e.get("event") == "trade_resolution_blocked"]
    starts   = [e for e in events if e.get("event") == "bot_start"]
    violations = [e for e in events if e.get("event") == "integrity_violation"]
    key_events = [e for e in events if e.get("event") in KEY_EVENTS]
    first_crosses = [e for e in events if e.get("event") == "first_cross_detected"]
    decisions = [e for e in events if e.get("event") == "window_decision"]

    # ── 1. trade_opened uniqueness ─────────────────────────────────────────────
    print("\n=== 1. trade_opened uniqueness (max 1 per trade_id) ===")
    opens_by_id: dict = defaultdict(list)
    for e in opened:
        opens_by_id[e.get("trade_id", "MISSING")].append(e)
    dupes = {tid: v for tid, v in opens_by_id.items() if len(v) > 1}
    if not check("No duplicate trade_opened per trade_id", not dupes,
                 f"{len(dupes)} duplicate(s): {list(dupes.keys())[:3]}" if dupes else ""):
        failures += 1

    # ── 2. trade_resolved uniqueness ──────────────────────────────────────────
    print("\n=== 2. trade_resolved uniqueness (max 1 per trade_id) ===")
    resolved_by_id: dict = defaultdict(list)
    for e in resolved:
        resolved_by_id[e.get("trade_id", "MISSING")].append(e)
    dupes_r = {tid: v for tid, v in resolved_by_id.items() if len(v) > 1}
    if not check("No duplicate trade_resolved per trade_id", not dupes_r,
                 f"{len(dupes_r)} duplicate(s): {list(dupes_r.keys())[:3]}" if dupes_r else ""):
        failures += 1

    # ── 3. No trade_id blocked AFTER it was already resolved ──────────────────
    # A resolved trade_id must never appear in a trade_resolution_blocked event
    # with a later timestamp.  (opened→blocked→resolved is valid; resolved→blocked
    # is not, and would mean the _resolved_ids guard in paper_trader failed.)
    # NOTE: the old check (window_ts cross-comparison) was a false positive:
    # trade_opened.window_ts == trade_resolution_blocked.window is the *normal*
    # lifecycle when resolution retries fail — same window, different lifecycle phase.
    print("\n=== 3. No trade_id re-blocked after trade_resolved ===")
    resolved_ts_by_id: dict[str, str] = {}
    for e in resolved:
        tid = e.get("trade_id", "")
        ts = e.get("ts", "")
        if tid and ts:
            if tid not in resolved_ts_by_id or ts > resolved_ts_by_id[tid]:
                resolved_ts_by_id[tid] = ts

    post_resolve_blocks: list[str] = []
    for e in blocked:
        tid = e.get("trade_id", "")
        ts = e.get("ts", "")
        if tid and ts and tid in resolved_ts_by_id:
            if ts > resolved_ts_by_id[tid]:
                post_resolve_blocks.append(tid)

    if not check("No trade_id appears in trade_resolution_blocked after trade_resolved",
                 not post_resolve_blocks,
                 f"{len(post_resolve_blocks)} trade(s) re-blocked post-resolve: "
                 f"{post_resolve_blocks[:3]}" if post_resolve_blocks else ""):
        failures += 1

    # ── 4. run_id in all key events ────────────────────────────────────────────
    print("\n=== 4. run_id present in all key events ===")
    missing_run_id = [e for e in key_events if not e.get("run_id")]
    if not check("run_id present in all key events", not missing_run_id,
                 f"{len(missing_run_id)} event(s) missing run_id" if missing_run_id else ""):
        failures += 1

    # ── 5. pid in all key events ───────────────────────────────────────────────
    print("\n=== 5. pid present in all key events ===")
    missing_pid = [e for e in key_events if not e.get("pid")]
    if not check("pid present in all key events", not missing_pid,
                 f"{len(missing_pid)} event(s) missing pid" if missing_pid else ""):
        failures += 1

    # ── 6. bot_start fee provenance ────────────────────────────────────────────
    print("\n=== 6. bot_start has fee provenance fields ===")
    if not starts:
        print(f"  [{WARN}] No bot_start event found — skipping fee provenance checks")
    else:
        bs = starts[-1]
        for field in ["fee_rate", "fee_source", "fee_status", "fee_exponent", "fee_exponent_source"]:
            if not check(f"bot_start.{field} present", field in bs,
                         f"value={bs.get(field)!r}" if field in bs else "MISSING"):
                failures += 1

    # ── 7. bot_start tick/min_order provenance ─────────────────────────────────
    print("\n=== 7. bot_start has tick_size + min_order_size provenance ===")
    if starts:
        bs = starts[-1]
        for field in ["tick_size", "tick_size_source", "min_order_size", "min_order_size_source"]:
            if not check(f"bot_start.{field} present", field in bs,
                         f"value={bs.get(field)!r}" if field in bs else "MISSING"):
                failures += 1

    # ── 8. trade_opened source labels ─────────────────────────────────────────
    print("\n=== 8. trade_opened has source labels ===")
    if not opened:
        print(f"  [{WARN}] No trade_opened events — skipping source label checks")
    else:
        for field in ["fee_exponent_source", "tick_size_source", "min_order_size_source", "window_ts"]:
            missing = [e for e in opened if field not in e]
            if not check(f"trade_opened.{field} present in all opens", not missing,
                         f"{len(missing)} event(s) missing {field}" if missing else ""):
                failures += 1

    # ── 9. No integrity_violation events ──────────────────────────────────────
    print("\n=== 9. No integrity_violation events ===")
    if not check("No integrity_violation events fired", not violations,
                 f"{len(violations)} violation(s): {[v.get('violation') for v in violations]}" if violations else ""):
        failures += 1

    # ── 10. first_cross_detected uniqueness ────────────────────────────────────
    print("\n=== 10. first_cross_detected uniqueness (max 1 per window_ts) ===")
    fc_by_window: dict = defaultdict(list)
    for e in first_crosses:
        fc_by_window[e.get("window_ts")].append(e)
    fc_dupes = {wts: v for wts, v in fc_by_window.items() if len(v) > 1}
    if not check("No duplicate first_cross_detected per window_ts", not fc_dupes,
                 f"{len(fc_dupes)} duplicate window(s): {list(fc_dupes.keys())[:3]}" if fc_dupes else ""):
        failures += 1

    # ── 11. window_decision uniqueness ────────────────────────────────────────
    print("\n=== 11. window_decision uniqueness (max 1 per window_ts) ===")
    wd_by_window: dict = defaultdict(list)
    for e in decisions:
        wd_by_window[e.get("window_ts")].append(e)
    wd_dupes = {wts: v for wts, v in wd_by_window.items() if len(v) > 1}
    if not check("No duplicate window_decision per window_ts", not wd_dupes,
                 f"{len(wd_dupes)} duplicate window(s): {list(wd_dupes.keys())[:3]}" if wd_dupes else ""):
        failures += 1

    # ── 12. No contradictory window_decision outcomes ─────────────────────────
    print("\n=== 12. No contradictory window_decision outcomes ===")
    opened_wd  = {e.get("window_ts") for e in decisions if e.get("decision") == "opened"}
    blocked_wd = {e.get("window_ts") for e in decisions if e.get("decision") == "blocked"}
    wd_contradictions = opened_wd & blocked_wd
    if not check("No window_ts with both opened and blocked window_decision",
                 not wd_contradictions,
                 f"contradicting: {wd_contradictions}" if wd_contradictions else ""):
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
    logs = sorted(log_dir.glob("polybot-*.jsonl"), reverse=True)
    return logs[0] if logs else None


def main() -> None:
    if len(sys.argv) > 1:
        path = Path(sys.argv[1])
    else:
        path = auto_detect_log()

    if not path or not path.exists():
        print("No log file found. Run the bot first, then: python verify_integrity.py")
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
