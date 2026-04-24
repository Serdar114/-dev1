# Polymarket Weather Temperature Edge Scanner — V1

**Paper/ghost mode only. No live orders. No wallet. No private keys.**

Scans active Polymarket daily temperature markets, computes model vs. market edge,
logs observations and ghost trades for fill-adjusted analysis.

---

## Setup

```bash
cd weatherbot
pip install -r requirements.txt
```

---

## Run

### Single scan cycle:
```bash
python tools/run_scan.py --once
```

### Continuous loop (60s interval):
```bash
python tools/run_scan.py --loop --interval 60
```

### Include markets with unknown city mappings:
```bash
python tools/run_scan.py --once --include-unknown-cities
```

### Limit markets scanned:
```bash
python tools/run_scan.py --once --max-markets 50
```

---

## Analyze results

```bash
python tools/analyze_signals.py
```

## Audit settlements

```bash
python tools/audit_settlements.py
```

---

## Run tests

```bash
cd weatherbot
pytest tests/ -v
```

---

## Log files

| File | Contents |
|------|----------|
| `logs/observations.jsonl` | Every scanned market per cycle |
| `logs/signals.jsonl` | Markets with edge signal (WATCH/PAPER_MAKER/PAPER_TAKER) |
| `logs/ghost_trades.jsonl` | Ghost (paper) trades logged |
| `logs/settlement_audit.jsonl` | Resolved market PnL audit |
| `logs/system.log` | System/error log |

---

## Sample observation log line

```json
{
  "timestamp_utc": "2025-04-24T14:32:01+00:00",
  "market_id": "0xabc123",
  "question": "Will the daily high temperature in Chicago be between 70°F and 79°F on April 25?",
  "city": "Chicago",
  "station_code": "KORD",
  "unit": "F",
  "market_type": "daily_high_temperature",
  "bucket_label": "70.0-79.0",
  "bucket_low": 70.0,
  "bucket_high": 79.0,
  "close_time_utc": "2025-04-25T23:59:00Z",
  "hours_to_close": 33.45,
  "settlement_safety_score": 0.87,
  "blacklist_flag": false,
  "best_bid": 0.55,
  "best_ask": 0.60,
  "spread": 0.05,
  "top_book_depth": 45.2,
  "display_price_mode": "real_midpoint",
  "model_probability": 0.74,
  "ensemble_agreement": 0.70,
  "model_spread": 3.2,
  "nowcast_probability": 0.68,
  "current_temp": 67.5,
  "edge_gross": 0.14,
  "edge_net_maker": 0.09,
  "edge_net_taker": 0.09,
  "recommended_action": "PAPER_MAKER",
  "recommended_price": 0.575,
  "recommended_size_usdc": 1.85,
  "reject_reason": null
}
```

---

## Module overview

| Module | Role |
|--------|------|
| `discovery.py` | Fetch active weather markets from Polymarket Gamma/CLOB APIs |
| `parser.py` | Parse market question into city, date, bucket, unit, source |
| `station_mapper.py` | Map city → ICAO station code, coordinates, risk |
| `weather_fetch.py` | Open-Meteo ensemble + METAR observation fetch |
| `forecast_engine.py` | Compute bucket probability from ensemble members |
| `nowcast_engine.py` | Current obs trajectory → nowcast probability nudge |
| `book_collector.py` | CLOB orderbook: bid/ask/depth (never frontend %) |
| `ev_calculator.py` | Gross/net edge → action recommendation |
| `settlement_safety.py` | Settlement risk score; hard-reject Paris/Wunderground/precip |
| `ghost_logger.py` | Append-only JSONL logging for all outputs |
| `settlement_audit.py` | Resolve ghost trades, compute fill-adjusted paper PnL |
| `runner.py` | Full pipeline orchestration per cycle |

---

## Known limitations (V1)

1. **Station coordinates**: Unknown cities have no lat/lon → forecast skipped, model_probability defaults to 0.5.
2. **Ensemble availability**: Open-Meteo free tier has rate limits. Retries with backoff but may fail under heavy load.
3. **METAR freshness**: AviationWeather.gov sometimes lags 30–60 min. Nowcast confidence marked LOW if time uncertain.
4. **Token ID mapping**: First token assumed = YES outcome. Multi-bucket markets may need richer outcome mapping in V2.
5. **Settlement source**: Many Polymarket markets use vague "credible source" language. These get safety score penalty.
6. **Bucket boundary ambiguity**: Parser is conservative — boundary-case numbers (e.g. exactly 80°F at the ≥80 boundary) depend on market rules which may not be in the question.
7. **No live order placement**: V1 is ghost/paper only. Filling at recommended_price is an assumption, not reality.

---

## Next steps (V2)

- [ ] Improve city extraction (NLP or fuzzy match on expanded city list)
- [ ] Multi-token per market: evaluate all outcome buckets simultaneously
- [ ] Historical model run comparison (stale-quote lag detection)
- [ ] Settlement source URL verification
- [ ] Fill simulator: model partial fills, queue position, maker probability
- [ ] Live CLOB order placement (after 30-day ghost run evaluation)
