"""
Truth Logger — measurement-focused JSONL observation stream.

Ayrı dosya: logs/truth-YYYY-MM-DD.jsonl
Amaç: Analiz edilebilir, dürüst observation katmanı.

Her event minimum şu alanları taşır:
  ts, event, market_slug, interval, execution_lane

Opsiyonel alanlar event tipine göre doldurulur:
  pair_sum, up_bid, up_ask, down_bid, down_ask,
  spread_up, spread_down, btc_mid_binance,
  fee_source, fee_status, fee_rate,
  resolution_truth_status, winner_source, etc.

Event tipleri:
  quote_snapshot  — pencere boyunca periyodik orderbook snapshot
  trade_opened    — pozisyon açıldığında
  trade_resolved  — pozisyon çözümlendiğinde
  trade_resolution_blocked — resolve başarısız olduğunda
"""

import json
import asyncio
from datetime import datetime, timezone
from pathlib import Path


LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

_lock = asyncio.Lock()


def _truth_path() -> Path:
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return LOG_DIR / f"truth-{date_str}.jsonl"


def _base_entry(event: str, data: dict) -> dict:
    """Her truth event'te bulunması gereken minimum alanlar."""
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "market_slug": data.get("market_slug", ""),
        "interval": data.get("interval", ""),
        "execution_lane": data.get("execution_lane", ""),
        **data,
    }


async def observe(event: str, data: dict) -> None:
    """Async truth observation — measurement JSONL'e yaz."""
    entry = _base_entry(event, data)
    line = json.dumps(entry, default=str) + "\n"
    async with _lock:
        with open(_truth_path(), "a", encoding="utf-8") as f:
            f.write(line)


def observe_sync(event: str, data: dict) -> None:
    """Sync fallback — startup/shutdown contexts."""
    entry = _base_entry(event, data)
    line = json.dumps(entry, default=str) + "\n"
    with open(_truth_path(), "a", encoding="utf-8") as f:
        f.write(line)
