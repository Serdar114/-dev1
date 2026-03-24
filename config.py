"""
config.py - Shared configuration, constants, and path management
for the Polymarket BTC 5-minute measurement harness.

No hand-wavy assumptions. Every value here is a measured or documented constant.
"""

import os
from pathlib import Path
from datetime import timezone

# ── Project root ──────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.resolve()
DATA_DIR = ROOT / "data"

# Sub-directories per module
MARKETS_DIR  = DATA_DIR / "markets"
BOOKS_DIR    = DATA_DIR / "books"
REFS_DIR     = DATA_DIR / "references"
RUNTIME_DIR  = DATA_DIR / "runtime"
SHADOWS_DIR  = DATA_DIR / "shadows"
REPORTS_DIR  = DATA_DIR / "reports"

for _d in [MARKETS_DIR, BOOKS_DIR, REFS_DIR, RUNTIME_DIR, SHADOWS_DIR, REPORTS_DIR]:
    _d.mkdir(parents=True, exist_ok=True)

# ── Timezone ──────────────────────────────────────────────────────────────────
UTC = timezone.utc

# ── Bankroll ──────────────────────────────────────────────────────────────────
BANKROLL_USDC = 30.0          # hard cap – never exceeded
MAX_POSITION_USDC = 5.0       # single-leg max for any shadow or live order

# ── Polymarket CLOB REST API ──────────────────────────────────────────────────
CLOB_BASE_URL   = "https://clob.polymarket.com"
CLOB_MARKETS    = f"{CLOB_BASE_URL}/markets"
CLOB_BOOK       = f"{CLOB_BASE_URL}/book"          # ?token_id=<id>
CLOB_TRADES     = f"{CLOB_BASE_URL}/trades"        # ?token_id=<id>
CLOB_LAST_PRICE = f"{CLOB_BASE_URL}/last-trade-price"  # ?token_id=<id>
CLOB_MID_PRICE  = f"{CLOB_BASE_URL}/midpoint"     # ?token_id=<id>

# ── Polymarket WebSocket ──────────────────────────────────────────────────────
CLOB_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# ── Polymarket fee constants ──────────────────────────────────────────────────
# Source: Polymarket CLOB documentation
TAKER_FEE_RATE   = 0.02   # 2% on order notional (USDC cost basis)
MAKER_FEE_RATE   = 0.00   # makers pay no fee
MIN_TICK         = 0.001  # $0.001 minimum price increment
# Minimum order size varies per market; default assumption before discovery
DEFAULT_MIN_SHARES = 5.0  # shares (will be overridden per market)

# ── BTC market detection heuristics ──────────────────────────────────────────
# Used in market_discovery.py to identify 5-minute BTC up/down markets
BTC_KEYWORDS     = ["btc", "bitcoin"]
FIVEMIN_KEYWORDS = ["5-minute", "5 minute", "5min", "5m"]
WINDOW_SECONDS   = 300    # 5 minutes in seconds
WINDOW_TOLERANCE = 60     # accept markets within ±60s of 300s duration

# ── Reference price snapshot offsets (seconds before close) ──────────────────
# T-N means N seconds before market settlement
REF_OFFSETS_SECONDS = [60, 30, 15, 10, 5, 1]

# ── Binance REST + WebSocket ──────────────────────────────────────────────────
BINANCE_REST_TICKER = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
BINANCE_REST_KLINE  = "https://api.binance.com/api/v3/klines"
BINANCE_WS_TRADE    = "wss://stream.binance.com:9443/ws/btcusdt@aggTrade"

# ── Book recording ────────────────────────────────────────────────────────────
BOOK_SNAPSHOT_INTERVAL_MS    = 500    # snapshot every 500ms
BOOK_LAST60_INTERVAL_MS      = 200    # denser in last 60 seconds
BOOK_TOP_N_LEVELS            = 5      # ladder depth to record

# ── Runtime monitoring ────────────────────────────────────────────────────────
WS_HEARTBEAT_TIMEOUT_S       = 10     # flag as gap if no message for >10s
RTT_SAMPLE_INTERVAL_S        = 5      # ping/pong RTT every 5 seconds
ORDER_LATENCY_WARN_MS        = 500    # warn if order RTT exceeds this

# ── Taker shadow thresholds ───────────────────────────────────────────────────
# Conditions required to even log a shadow taker candidate
TAKER_MAX_SPREAD_PCT         = 0.03   # spread must be < 3% of mid
TAKER_MIN_SECONDS_TO_CLOSE   = 5     # must be within last N seconds
TAKER_MAX_SECONDS_TO_CLOSE   = 30    # but no earlier than this
TAKER_MIN_EDGE_AFTER_FEE     = 0.01  # net expected value must exceed 1¢

# ── Maker shadow thresholds ───────────────────────────────────────────────────
MAKER_QUOTE_SPREAD_TARGET    = 0.02   # target 2% spread to mid
MAKER_MIN_DEPTH_USDC         = 10.0  # only quote if top-of-book depth >= $10
MAKER_DRIFT_WINDOW_S         = 30    # measure fair-price drift over 30s post fill

# ── HTTP request settings ─────────────────────────────────────────────────────
HTTP_TIMEOUT_S               = 5
HTTP_MAX_RETRIES             = 3
HTTP_RETRY_BACKOFF_S         = 1.0

# ── Logging format ────────────────────────────────────────────────────────────
LOG_FORMAT = "%(asctime)s.%(msecs)03d [%(name)s] %(levelname)s %(message)s"
LOG_DATEFMT = "%Y-%m-%dT%H:%M:%S"
