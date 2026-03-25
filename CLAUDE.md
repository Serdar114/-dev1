# POLYMARKET BTC BOT — OPERATING DOCTRINE
## Scope
- Primary: Polymarket BTC 5m up/down only.
- Secondary measurement lane: BTC 15m only.
- Out of scope: 1h, daily, other assets, other venues.
## Bankroll truth
- Starting bankroll is real 30 USDC.
- Goal for the next 10 days is NOT income.
- Goal is to produce a live candidate or an honest kill verdict.
- Survival > growth. Selectivity > activity.
## Market truth
- Chainlink resolution truth is primary truth.
- Binance is signal/proxy only.
- Taker is benchmark lane, not default lane.
- Maker is only a candidate if fill toxicity is measured.
## Non-negotiables
- Never hardcode fee rate.
- Never hardcode tick size.
- Never hardcode min order size.
- Never treat a correct directional call as proof of edge.
- Never change strategy logic and measurement logic in the same task unless explicitly asked.
- Never claim "works" without:
  1) runtime logs,
  2) fill-conditioned evidence,
  3) fee-aware net result,
  4) what remains unproven.
## Critical files
Truth-critical:
- fee_engine.py
- market_discovery.py
- resolution_truth.py
- truth_logger.py
- analyzer.py
Execution-critical:
- signal_engine.py
- paper_trader.py
- execution_router.py
- risk_manager.py
- main.py
## Default behavior
For any non-trivial task:
1. read relevant files first
2. state plan
3. implement only the requested scope
4. run/describe verification
5. report risks and unknowns
6. say what is still NOT proven
## Output format for every task
- Problem
- Key facts
- Plan
- Files changed
- Verification
- Risks / unknowns
- Verdict
