# Experiment Matrix — Config-Only Profiles

Date created: 2026-03-27
Fee regime: Current (pre-March 30 2026) — fee_rate=0.072, fee_exponent=1
Execution lane: taker_paper (all profiles)
Code changes: NONE

## Profiles

### 1. `config_5m_baseline_currentfee.json` — CONTROL

| Key | Value |
|-----|-------|
| market_type | 5m |
| target_sum_max | 0.95 |
| max_spread_pct | 2.0 |
| min_orderbook_depth | 50 |
| fee_rate | 0.072 |
| fee_exponent | 1 |

**Changed from production config.json:** Nothing (identical except `market_type_note`).

**Why it exists:** Control group. Establishes signal frequency, skip reason distribution, and trade count under current thresholds. All other profiles are compared against this.

**Confirms:** Baseline signal rate and skip-reason breakdown.
**Falsifies:** If baseline fires trades regularly, the dryness hypothesis was wrong and the problem is elsewhere.

---

### 2. `config_5m_explore_A_currentfee.json` — PAIR SUM GATE ONLY

| Key | Value | Changed? |
|-----|-------|----------|
| market_type | 5m | no |
| target_sum_max | **0.99** | **YES (was 0.95)** |
| max_spread_pct | 2.0 | no |
| min_orderbook_depth | 50 | no |
| fee_rate | 0.072 | no |
| fee_exponent | 1 | no |

**Changed from baseline:** `target_sum_max` 0.95 → 0.99

**Why it exists:** Isolates the pair_sum gate as the dominant blocker. If dryness is caused by target_sum_max=0.95, relaxing to 0.99 should produce dramatically more signals while keeping spread and depth filters unchanged.

**Confirms hypothesis if:** Signal count increases significantly vs baseline. Trades fire, and logs show pair_sum values in the 0.95–0.99 range that were previously blocked.
**Falsifies hypothesis if:** Signal count stays near zero — meaning spread or depth gates are the actual blockers, not pair_sum.

**Risk:** Trades at pair_sum 0.96–0.99 have gross edge of only 1–4 cents/share. After fees (~0.017/share at p=0.50), net edge is thin. This profile may show positive signal count but negative net PnL — that is expected and informative.

---

### 3. `config_5m_explore_B_currentfee.json` — PAIR SUM + SPREAD RELAXED

| Key | Value | Changed? |
|-----|-------|----------|
| market_type | 5m | no |
| target_sum_max | **0.99** | **YES (was 0.95)** |
| max_spread_pct | **2.5** | **YES (was 2.0)** |
| min_orderbook_depth | 50 | no |
| fee_rate | 0.072 | no |
| fee_exponent | 1 | no |

**Changed from baseline:** `target_sum_max` 0.95 → 0.99, `max_spread_pct` 2.0 → 2.5

**Why it exists:** If Explore A still shows dryness, spread may be the secondary blocker. This profile relaxes both pair_sum and spread to measure maximum possible signal surface under current depth requirements.

**Confirms hypothesis if:** Signal count increases vs Explore A — spread was a co-blocker.
**Falsifies hypothesis if:** Signal count identical to Explore A — spread was not a factor; depth or orderbook availability is the remaining gate.

**Risk:** Wider spread acceptance means worse execution quality in a real taker fill. This is observation-only — the 2.5% spread cap is intentionally loose to measure, not to trade live.

---

### 4. `config_15m_observe_currentfee.json` — 15m LANE OBSERVATION

| Key | Value | Changed? |
|-----|-------|----------|
| market_type | **15m** | **YES (was 5m)** |
| target_sum_max | 0.95 | no |
| max_spread_pct | 2.0 | no |
| min_orderbook_depth | 50 | no |
| fee_rate | 0.072 | no |
| fee_exponent | 1 | no |

**Changed from baseline:** `market_type` 5m → 15m

**Why it exists:** Measures whether the 15m lane has different market microstructure — deeper books, tighter pair_sum, more liquidity. Same thresholds as 5m baseline, so any difference in signal rate is attributable to the longer interval's market characteristics.

**Confirms hypothesis if:** 15m shows more signals or lower pair_sum than 5m baseline — longer intervals attract more liquidity and tighter pricing.
**Falsifies hypothesis if:** 15m is equally dry — the problem is market-wide, not interval-specific.

---

## Comparison Matrix

| Profile | target_sum_max | max_spread_pct | min_depth | interval | Purpose |
|---------|---------------|----------------|-----------|----------|---------|
| Baseline | 0.95 | 2.0 | 50 | 5m | Control |
| Explore A | **0.99** | 2.0 | 50 | 5m | Isolate pair_sum gate |
| Explore B | **0.99** | **2.5** | 50 | 5m | Isolate pair_sum + spread |
| 15m Observe | 0.95 | 2.0 | 50 | **15m** | Cross-interval comparison |

## How to Run

```bash
# Baseline
python main.py --config config_5m_baseline_currentfee.json

# Explore A
python main.py --config config_5m_explore_A_currentfee.json

# Explore B
python main.py --config config_5m_explore_B_currentfee.json

# 15m Observe
python main.py --config config_15m_observe_currentfee.json
```

## What Was NOT Changed (across all profiles)

- fee_rate (0.072) and fee_exponent (1) — current regime, no correction needed
- shares_per_side (5)
- bankroll (30.0)
- entry_window_start_ste (240) and entry_window_end_ste (10)
- min_orderbook_depth (50) — held constant to isolate other variables
- max_daily_trades (5), max_consecutive_losses (4), bankroll_floor_pct (0.70)
- resolve_confirm_secs (130)
- All API endpoints, WS URLs, credential fields
- No maker lane parameters — taker_paper only
- No code changes whatsoever

## What Remains Unproven

- Whether min_orderbook_depth=50 is itself a blocker (no profile isolates this alone)
- Whether PM WebSocket book delivery is reliable enough to avoid silent no_orderbook skips
- Whether fee regime changes after March 30 2026 will require new profiles
- Actual fill behavior in live taker — paper assumes 100% fill at ask
