# polybot_v2 – Phase 1

Hybrid paper trading system for Polymarket BTC 5-minute markets.

## What it does

Two lanes run concurrently on every tick (5s interval):

**Lane 1 – Selective Taker Paper Engine**
Computes a fair probability for the current BTC 5-minute market using a
closed-form normal CDF formula. Measures after-fee edge vs the implied
probability from Polymarket order book. If edge exceeds the configured
threshold and all risk checks pass, it opens a simulated (paper) trade.
No real order is ever placed.

**Lane 2 – Maker Shadow Probe**
Computes a theoretical post-only quote price on the side where we have
maker edge. Checks whether that quote would cross the book, whether a fill
would theoretically occur, and measures the adverse price move after a
hypothetical fill. Everything is logged; no order is placed.

## Phase 1 limits

- BTC 5-minute market only
- No live order placement of any kind
- No real funds at risk
- 30 USDC starting bankroll (tracked in paper)
- Stake policy is bankroll-linked and conservative

## Fair probability formula

```
delta_pct = (btc_mid - window_open) / window_open
tau_eff   = max(seconds_to_expiry / 300.0, tau_floor)
sigma_eff = max(realized_vol_60s, sigma_floor)
z         = clip(delta_pct / (sigma_eff * sqrt(tau_eff)), -8, +8)
fair_yes  = clip(Phi(z), 0.01, 0.99)
fair_no   = 1 - fair_yes
```

## How to run

```bash
cd polybot_v2
pip install -r requirements.txt
python src/main.py
```

To use a custom config:
```bash
POLYBOT_CONFIG=/path/to/config.yaml python src/main.py
```

## Logs

All logs land in `logs/` (relative to `config.yaml`):

| File | Contents |
|------|----------|
| `logs/signals.jsonl` | Every taker + shadow signal evaluation |
| `logs/paper_trades.jsonl` | Paper trade open/resolve events |
| `logs/shadow_quotes.jsonl` | Shadow quote events + adverse move |
| `logs/bankroll.jsonl` | Bankroll snapshots at each window end |
| `logs/state.json` | Persistent state (bankroll, cooldown, etc.) |

Console output uses human-readable format.

## Running tests

```bash
cd polybot_v2
pip install -r requirements.txt
pytest tests/ -v
```

## Configuration

All settings in `config.yaml`. Key sections:

- `app.mode`: `paper` or `paper_with_shadow_probe`
- `fair_prob`: sigma_floor, tau_floor, z-score clip, prob clip bounds
- `fees`: taker_fee_rate (2%), maker_rebate_rate (0 in Phase 1)
- `stake`: bankroll, scale_tiers (bankroll-linked sizing)
- `risk`: max actions/window, consecutive loss limit, cooldown

## File structure

```
polybot_v2/
  config.yaml          – single config file
  requirements.txt
  src/
    main.py            – entry point, main loop
    settings.py        – config loader + validation
    models.py          – all dataclasses
    logger.py          – console + JSONL logging
    binance_feed.py    – Binance WS mid-price feed
    polymarket_client.py – REST client for market data
    market_discovery.py  – finds active BTC 5m market
    fee_engine.py      – taker/maker fee math
    fair_prob_engine.py  – fair probability (normal CDF)
    edge_engine.py     – after-fee edge calculation
    stake_policy.py    – bankroll-linked sizing
    risk_manager.py    – stale/cooldown/loss guards
    signal_engine.py   – orchestrates both lanes
    paper_executor.py  – paper trade open/resolve
    maker_shadow_probe.py – shadow quote builder
    metrics.py         – session metric aggregation
    state_store.py     – JSON persistence
  tests/
    test_fee_engine.py
    test_fair_prob_engine.py
    test_stake_policy.py
    test_signal_engine.py
```
