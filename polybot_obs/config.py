# config.py — constants, thresholds, URLs

# ── Binance ────────────────────────────────────────────────────────────────
BINANCE_WS = "wss://stream.binance.com:9443/ws/btcusdt@aggTrade"

# Reconnect backoff: 1 2 4 8 16 30 30 ... (seconds)
BINANCE_BACKOFF_BASE = 1
BINANCE_BACKOFF_MAX = 30

# ── Polymarket ─────────────────────────────────────────────────────────────
GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE  = "https://clob.polymarket.com"

MARKET_URL    = GAMMA_BASE + "/markets"
ORDERBOOK_URL = CLOB_BASE  + "/book"

# ── Window ─────────────────────────────────────────────────────────────────
WINDOW_SECONDS = 300          # 5-minute windows
SLUG_PREFIX    = "btc-updown-5m-"

# Snapshot offsets (seconds before window close)
SNAPSHOT_OFFSETS = [60, 30, 15, 10, 5, 1]   # T60 … T1

# Polling interval during window (seconds)
SNAPSHOT_POLL_INTERVAL = 2

# ── Paper snipe thresholds ─────────────────────────────────────────────────
SNIPE_BTC_DELTA_PCT  = 0.10   # minimum |Δ%| to trigger
SNIPE_MAX_ASK        = 0.93   # only enter if YES ask < this
SNIPE_TIME_MIN       = 2      # seconds remaining (exclusive)
SNIPE_TIME_MAX       = 15     # seconds remaining (exclusive)

# ── Resolution polling ─────────────────────────────────────────────────────
RESOLUTION_POLL_INTERVAL = 30   # seconds between polls
RESOLUTION_TIMEOUT       = 600  # 10 minutes → log UNRESOLVED

# ── min_order_size warning threshold ──────────────────────────────────────
MIN_ORDER_SIZE_WARN = 10

# ── Log paths ──────────────────────────────────────────────────────────────
import os
LOG_DIR             = os.path.join(os.path.dirname(__file__), "logs")
OBS_LOG             = os.path.join(LOG_DIR, "observations.jsonl")
DAILY_LOG           = os.path.join(LOG_DIR, "daily_summary.jsonl")
STARTUP_LOG         = os.path.join(LOG_DIR, "startup.log")

# ── HTTP timeouts (seconds) ────────────────────────────────────────────────
HTTP_TIMEOUT = 10
