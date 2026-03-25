"""JSONL logger — one JSON object per line, one file per day."""

import json
import asyncio
from datetime import datetime, timezone
from pathlib import Path


LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

_lock = asyncio.Lock()


def _log_path() -> Path:
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return LOG_DIR / f"polybot-{date_str}.jsonl"


async def log(event: str, data: dict) -> None:
    """Append a JSONL entry asynchronously."""
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        **data,
    }
    line = json.dumps(entry, default=str) + "\n"
    async with _lock:
        with open(_log_path(), "a", encoding="utf-8") as f:
            f.write(line)


def log_sync(event: str, data: dict) -> None:
    """Synchronous fallback for startup/shutdown contexts."""
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        **data,
    }
    line = json.dumps(entry, default=str) + "\n"
    with open(_log_path(), "a", encoding="utf-8") as f:
        f.write(line)
