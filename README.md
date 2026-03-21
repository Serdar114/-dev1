# BTC 5m Research Bot — Clean-Room Design

> **PAPER TRADING ONLY.**
> This codebase contains **no live trading code**.
> Paper profitability does **not** imply live profitability.
> Read the [Execution Realism Risks](#execution-realism-risks) section before drawing any conclusions.

---

## Overview

A deterministic, auditable research bot for evaluating a maker-first thesis on
Polymarket BTC 5-minute up/down markets.

The bot is structured around **truthful measurement**: every signal gate,
every fee calculation, every kill condition, and every feed observation is
explicitly logged and traceable.

---

## Architecture

```
run.py                     <- entry point
bot.py                     <- orchestrator / phase control
|
+-- feeds/
|   +-- base.py            <- BaseFeedAdapter (gap detection, stale flags)
|   +-- fast_feed.py       <- FastFeedAdapter     (RTDS: crypto_prices)
|   +-- chainlink_feed.py  <- ChainlinkFeedAdapter (RTDS: crypto_prices_chainlink)
|
+-- discovery/
|   +-- market.py          <- Deterministic slug generation + Gamma API lookup
|
+-- signal/
|   +-- engine.py          <- Stateless, fully auditable signal engine
|
+-- execution/
|   +-- fees.py            <- Fee model (auditable; only file with fee logic)
|   +-- maker_lane.py      <- Maker paper lane (PRIMARY evaluation)
|   +-- taker_lane.py      <- Taker paper lane (benchmark / control)
|
+-- risk/
|   +-- sizing.py          <- 5-share fixed sizing + bankroll tracking
|
+-- validator/
|   +-- kill_conditions.py <- Kill condition framework (all logic here only)
|
+-- logger/
|   +-- summary.py         <- Session summaries + per-window JSONL logs
|
+-- config/
    +-- settings.yaml       <- Runtime config (feeds, signals, fees, sizing)
    +-- kill_conditions.yaml <- All kill conditions (thresholds + actions)
```

---

## Dual Feed (Mandatory)

Both feeds are subscribed and logged every active window.

| Feed | RTDS Channel | Analogy | Role |
|------|-------------|---------|------|
| Fast | `crypto_prices` | Binance spot | Intra-window price signal |
| Reference | `crypto_prices_chainlink` | Chainlink oracle | Settlement reference |

### Logged per window

- `fast_price`, `fast_feed_gap_seconds`, `fast_feed_stale`
- `chainlink_price`, `chainlink_gap_seconds`, `chainlink_feed_stale`
- `basis_bps` (signed: fast minus chainlink, in basis points)
- `basis_mismatch_bps` (absolute value)
- `basis_mismatch` (boolean flag: `|basis_bps| > threshold`)

---

## Sizing

**v1 hard constraint: all paper orders use exactly 5 shares.**

No 1-share or 2-share examples exist in this codebase.

Every execution log record includes `bankroll_fraction` — the fraction of the
current paper bankroll consumed by the 5-share entry at the given price.

---

## Market Discovery

Slugs are generated deterministically:

```
btc-updown-5m-{window_boundary}
```

where `window_boundary` is the Unix timestamp (integer seconds) of the window
open, snapped to the 5-minute grid.

Example: `btc-updown-5m-1736946300`

Token IDs are **never hardcoded**. They are resolved dynamically via the
Gamma events API on every new window. See `discovery/market.py`.

---

## Fee Model

Full derivation: `execution/fees.py`.

| Lane | Fee |
|------|-----|
| Maker | **0** (passive limit order, zero maker fee) |
| Taker | `C x 0.25 x (p x (1 - p))^2` |

Where:
- `p` = execution price of the YES share
- `C` = fee constant (default `0.02`, from Polymarket CLOB documented taker rate)
- Override `C` via `config/settings.yaml` -> `fees.taker_fee_C`

All fee logic is confined to `execution/fees.py`. No fee computation occurs
anywhere else.

---

## Quote Buckets

Every maker quote is classified:

| Bucket | Price Range |
|--------|-------------|
| B1 | 0.83 to 0.86 |
| B2 | 0.87 to 0.90 |
| B3 | 0.91 to 0.92 |

Quotes outside these ranges are marked `INELIGIBLE` and not filled.

Per-window maker logs include: `quote_bucket`, `break_even_wr_estimate`,
`win_if_correct`, `loss_if_wrong`.

---

## Gated Validation Phases

| Phase | Label | Description |
|-------|-------|-------------|
| `0a` | Infrastructure validation | Feed connectivity + discovery health. No signals. |
| `0b` | Signal validation | Signal engine active. No fill simulation. |
| `0c` | Paper trading validation | Full dual-lane paper execution. |
| `1` | Live-candidate readiness | Paper only. Tighter kill thresholds. |

Select phase in `config/settings.yaml` -> `phase`, or override via `--phase` flag.

**This bot does not contain live trading code.**

---

## Kill Conditions

All conditions live in `config/kill_conditions.yaml`.
All evaluation logic lives in `validator/kill_conditions.py`.
No kill logic is embedded elsewhere.

| Condition | Default Threshold | Action |
|-----------|------------------|--------|
| raw_directional_accuracy | < 45% (n>=20) | kill |
| filtered_directional_accuracy | < 50% (n>=15) | kill |
| fill_conditioned_win_rate | < 52% (n>=10) | kill |
| fill_rate | < 5% (n>=20) | warn |
| adverse_fill_underperformance | < -3% bankroll (n>=10) | kill |
| avg_fill_price_ceiling | > 0.93 (n>=10) | tighten |
| basis_mismatch_ceiling | > 20% freq (n>=10) | tighten |
| chainlink_gap_frequency_ceiling | > 20% windows (n>=10) | kill |
| fast_feed_gap_frequency_ceiling | > 15% windows (n>=10) | kill |
| discovery_failure_rate_ceiling | > 10% (n>=10) | kill |
| open_price_capture_delay_ceiling | > 3.0s avg (n>=10) | tighten |
| consecutive_loss_limit | 8 in a row | kill |
| paper_bankroll_floor | < $700 | kill |

Actions: `kill` -> stop session | `tighten` -> log warning | `warn` -> log only.

---

## Session Summary Fields

Every summary block (interim and final) reports:

```
windows_observed
candidate_windows
maker_candidate_count
taker_candidate_count
maker_fills
taker_fills
maker_fill_rate
taker_execution_rate
maker_avg_quote_bucket
filtered_directional_accuracy    (n=)
fill_conditioned_WR              (n=)
avg_fill_price
basis_mismatch_frequency
chainlink_gap_frequency
fast_feed_gap_frequency
paper_bankroll
consecutive_losses
kill_condition_status
recommended_action
VERDICT: continue / tighten / kill
```

---

## Logs

All logs are written to `logs/`:

- `bot_{ts}.log` — structured text log (all levels)
- `windows_{ts}.jsonl` — one JSON record per window (full audit trail)
- `summary_{ts}.txt` — interim + final summary blocks

---

## Running

```bash
pip install -r requirements.txt

# Infrastructure validation (feeds + discovery only)
python run.py --phase 0a

# Signal validation (no fills)
python run.py --phase 0b

# Paper trading (full dual-lane)
python run.py --phase 0c

# Live-candidate readiness (paper, tighter thresholds)
python run.py --phase 1
```

---

## Known TODOs / Documented Gaps

The following integration details are uncertain and marked with `TODO` in
the source code. They use documented adapter stubs rather than invented
endpoints:

1. **RTDS WebSocket subscription format** (`feeds/base.py`): Subscribe
   message payload schema needs confirmation from Polymarket RTDS docs.

2. **`crypto_prices` message schema** (`feeds/fast_feed.py`): Field names
   and nesting confirmed against CLOB naming conventions; update if live
   schema differs.

3. **`crypto_prices_chainlink` message schema** (`feeds/chainlink_feed.py`):
   Same as above. `round_id` field logged if present.

4. **Gamma API endpoint** (`discovery/market.py`): Best-known pattern is
   `GET /markets?slug={slug}`. Confirm path + response schema against live docs.

5. **YES bid/ask** (`bot.py`): CLOB order book feed not yet plumbed. Spread
   quality gate will fail until this is connected.

6. **1m candle tracker** (`bot.py`): `candles_same_direction` hardcoded to 0.
   Momentum persistence gate will fail until candle data is available.

7. **Intra-window price extremes** (`bot.py`): Maker fill simulation uses
   open price as proxy for both low and high. Real fill simulation needs
   intra-window OHLC.

---

## Execution Realism Risks

> **Paper profitability does NOT imply live profitability.**

The following gaps exist between this paper simulation and live execution.
These are not edge cases — they are structural differences that will affect
results in ways that are difficult to quantify without a live run.

### Fill simulation is optimistic
Maker fills are simulated using a simple price-crossing heuristic. In
reality, queue position, competing liquidity providers, and partial fills
would reduce fill rate significantly. A maker quote that appears to have
been filled in simulation may never have been reached in practice.

### Taker fills assume open-price execution
Taker fills are simulated at the window-open price. In live trading, any
delay between the window open and order submission — even milliseconds —
exposes the order to price movement. Slippage, spread costs, and market
impact worsen effective fill prices in ways this model does not capture.

### Fee model is approximate
The quadratic taker fee formula (`C x 0.25 x (p(1-p))^2`) is the model
specified by this design. It is **not** a direct transcription of
Polymarket's published fee schedule. Actual fees may differ materially.
Maker fee is modelled as zero; verify this against the current fee
structure before drawing live-trading conclusions.

### Latency and infrastructure
This simulation assumes near-instantaneous feed delivery and signal
computation. In live conditions, network latency, API rate limits, order
queuing, WebSocket reconnection delays, and process scheduling introduce
timing uncertainty that is entirely absent from this model.

### Market microstructure
Spread, depth, and fill probability depend on real-time order book state
that this simulation does not model. Adverse selection — where fills
disproportionately occur just before price moves against our position —
is a known risk in limit-order strategies. It is not captured here.

### Settlement risk
Paper trades assume YES/NO settlement based solely on price direction
(close vs. open). Actual Polymarket settlement depends on oracle results,
dispute windows, and market-specific rules that may produce outcomes
inconsistent with a simple price comparison.

### Overfitting and regime sensitivity
Signal parameters, kill thresholds, and quote buckets were set manually.
Paper results should not be interpreted as evidence of an edge unless they
hold across varied market conditions, including low-liquidity periods,
high-volatility regimes, and exchange-level events.

### Do not initiate live trading based on paper results alone.

---

## Signal Engine Features

| Feature | Gate | Purpose |
|---------|------|---------|
| endcycle_timing_quality | Pass if seconds_to_close >= 45s | Ensures quote can be posted |
| basis_mismatch | Logged; flag only | Feed reliability indicator |
| spread_quality | Pass if spread >= 5 bps | Ensures tradeable spread |
| extreme_zone_eligible | Pass if 0.10 < price < 0.90 | Avoids near-certain outcomes |
| momentum_persistence | Pass if candles_same_dir >= 2 | Confirms direction |
| open_price_integrity | Pass if open prices agree | Detects feed divergence at open |
| feed_freshness | Pass if both gaps <= 8s | Rejects stale-feed signals |

All gate results are logged in `WindowLog.gate_summary` for full auditability.

---

## Dual-Lane Architecture

The bot maintains **both** lanes from the same signal:

```
Signal  ->  MakerLane  (primary:   limit order sim, fee=0)
        ->  TakerLane  (benchmark: market order sim, taker fee applied)
```

The maker lane is the primary evaluation vehicle.
The taker lane is the apples-to-apples benchmark for fee-impact quantification.
The architecture will not be collapsed to maker-only.
