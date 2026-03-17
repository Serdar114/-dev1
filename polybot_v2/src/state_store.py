"""
Lightweight JSON state store for polybot_v2.

Persists:
  - current bankroll
  - active cooldown
  - last window ts
  - session counters

Survives restarts. File written atomically via temp-rename.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_DEFAULT_STATE: dict[str, Any] = {
    "bankroll": None,          # None = use config default
    "peak_bankroll": None,
    "total_pnl": 0.0,
    "drawdown": 0.0,
    "cooldown_windows_remaining": 0,
    "consecutive_losses": 0,
    "last_window_ts": 0.0,
    "session_trade_count": 0,
    "session_start_ts": 0.0,
    "version": 1,
}


class StateStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._state: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                with open(self._path) as f:
                    loaded = json.load(f)
                self._state = {**_DEFAULT_STATE, **loaded}
                log.info("StateStore loaded from %s", self._path)
                return
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("StateStore load failed (%s), using defaults", exc)
        self._state = dict(_DEFAULT_STATE)
        self._state["session_start_ts"] = time.time()

    def save(self) -> None:
        """Atomically write state to disk."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(
                dir=self._path.parent, prefix=".state_tmp_"
            )
            with os.fdopen(fd, "w") as f:
                json.dump(self._state, f, indent=2)
            os.replace(tmp_path, self._path)
        except OSError as exc:
            log.error("StateStore save failed: %s", exc)

    # ------------------------------------------------------------------ #
    # Accessors
    # ------------------------------------------------------------------ #

    def get(self, key: str, default: Any = None) -> Any:
        return self._state.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._state[key] = value

    def update(self, data: dict[str, Any]) -> None:
        self._state.update(data)

    def as_dict(self) -> dict[str, Any]:
        return dict(self._state)

    # ------------------------------------------------------------------ #
    # Typed helpers
    # ------------------------------------------------------------------ #

    @property
    def bankroll(self) -> float | None:
        v = self._state.get("bankroll")
        return float(v) if v is not None else None

    @bankroll.setter
    def bankroll(self, value: float) -> None:
        self._state["bankroll"] = value

    @property
    def cooldown_windows_remaining(self) -> int:
        return int(self._state.get("cooldown_windows_remaining", 0))

    @cooldown_windows_remaining.setter
    def cooldown_windows_remaining(self, value: int) -> None:
        self._state["cooldown_windows_remaining"] = value

    @property
    def consecutive_losses(self) -> int:
        return int(self._state.get("consecutive_losses", 0))

    @consecutive_losses.setter
    def consecutive_losses(self, value: int) -> None:
        self._state["consecutive_losses"] = value

    @property
    def last_window_ts(self) -> float:
        return float(self._state.get("last_window_ts", 0.0))

    @last_window_ts.setter
    def last_window_ts(self, value: float) -> None:
        self._state["last_window_ts"] = value
