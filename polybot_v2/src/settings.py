"""
Settings loader for polybot_v2.
Reads config.yaml, validates required fields, exposes typed accessors.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml


class ConfigError(Exception):
    pass


class Settings:
    def __init__(self, config_path: str | Path | None = None) -> None:
        if config_path is None:
            # default: config.yaml next to this file's parent (polybot_v2/)
            config_path = Path(__file__).parent.parent / "config.yaml"
        self._path = Path(config_path)
        self._raw: dict[str, Any] = self._load()
        self._validate()

    def _load(self) -> dict[str, Any]:
        if not self._path.exists():
            raise ConfigError(f"Config file not found: {self._path}")
        with open(self._path) as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            raise ConfigError("config.yaml must be a YAML mapping")
        return data

    def _validate(self) -> None:
        required_sections = [
            "app", "market", "feeds", "fair_prob", "fees",
            "zones", "stake", "risk", "edge", "maker_shadow", "logging",
        ]
        for section in required_sections:
            if section not in self._raw:
                raise ConfigError(f"Missing required config section: [{section}]")

        valid_modes = {"paper", "paper_with_shadow_probe"}
        mode = self._raw["app"].get("mode")
        if mode not in valid_modes:
            raise ConfigError(f"app.mode must be one of {valid_modes}, got: {mode!r}")

        if self._raw["stake"].get("bankroll", 0) <= 0:
            raise ConfigError("stake.bankroll must be positive")

    # ------------------------------------------------------------------ #
    # Convenient typed accessors
    # ------------------------------------------------------------------ #

    def get(self, *keys: str, default: Any = None) -> Any:
        """Dot-path accessor: get('fair_prob', 'sigma_floor')"""
        node = self._raw
        for k in keys:
            if not isinstance(node, dict) or k not in node:
                return default
            node = node[k]
        return node

    @property
    def mode(self) -> str:
        return self._raw["app"]["mode"]

    @property
    def log_level(self) -> str:
        return self._raw["app"].get("log_level", "INFO")

    @property
    def symbol(self) -> str:
        return self._raw["market"]["symbol"]

    @property
    def window_sec(self) -> int:
        return int(self._raw["market"]["window_sec"])

    @property
    def entry_start_sec(self) -> int:
        return int(self._raw["market"].get("entry_start_sec", 30))

    @property
    def entry_end_sec(self) -> int:
        return int(self._raw["market"].get("entry_end_sec", 240))

    @property
    def shadow_start_sec(self) -> int:
        return int(self._raw["market"].get("shadow_start_sec", 10))

    @property
    def shadow_end_sec(self) -> int:
        return int(self._raw["market"].get("shadow_end_sec", 270))

    @property
    def stale_binance_ms(self) -> int:
        return int(self._raw["feeds"]["stale_binance_ms"])

    @property
    def stale_polymarket_ms(self) -> int:
        return int(self._raw["feeds"]["stale_polymarket_ms"])

    @property
    def sigma_floor(self) -> float:
        return float(self._raw["fair_prob"]["sigma_floor"])

    @property
    def tau_floor(self) -> float:
        return float(self._raw["fair_prob"]["tau_floor"])

    @property
    def zscore_clip(self) -> float:
        return float(self._raw["fair_prob"]["zscore_clip"])

    @property
    def prob_clip_min(self) -> float:
        return float(self._raw["fair_prob"]["prob_clip_min"])

    @property
    def prob_clip_max(self) -> float:
        return float(self._raw["fair_prob"]["prob_clip_max"])

    @property
    def taker_fee_rate(self) -> float:
        return float(self._raw["fees"]["taker_fee_rate"])

    @property
    def maker_rebate_rate(self) -> float:
        return float(self._raw["fees"].get("maker_rebate_rate", 0.0))

    @property
    def taker_min_prob(self) -> float:
        return float(self._raw["zones"]["taker_min_prob"])

    @property
    def taker_max_prob(self) -> float:
        return float(self._raw["zones"]["taker_max_prob"])

    @property
    def extreme_upper(self) -> float:
        return float(self._raw["zones"]["extreme_upper"])

    @property
    def extreme_lower(self) -> float:
        return float(self._raw["zones"]["extreme_lower"])

    @property
    def bankroll(self) -> float:
        return float(self._raw["stake"]["bankroll"])

    @property
    def min_notional(self) -> float:
        return float(self._raw["stake"]["min_notional"])

    @property
    def max_risk_fraction(self) -> float:
        return float(self._raw["stake"]["max_risk_fraction"])

    @property
    def drawdown_reduce_factor(self) -> float:
        return float(self._raw["stake"].get("drawdown_reduce_factor", 0.5))

    @property
    def scale_tiers(self) -> list[dict]:
        return self._raw["stake"].get("scale_tiers", [])

    @property
    def max_actions_per_window(self) -> int:
        return int(self._raw["risk"]["max_actions_per_window"])

    @property
    def max_consecutive_losses(self) -> int:
        return int(self._raw["risk"]["max_consecutive_losses"])

    @property
    def cooldown_windows(self) -> int:
        return int(self._raw["risk"]["cooldown_windows"])

    @property
    def max_shadow_quotes_per_window(self) -> int:
        return int(self._raw["risk"]["max_shadow_quotes_per_window"])

    @property
    def min_after_fee_edge(self) -> float:
        return float(self._raw["edge"]["min_after_fee_edge"])

    @property
    def default_tick_size(self) -> float:
        return float(self._raw["maker_shadow"]["default_tick_size"])

    @property
    def extreme_tick_size(self) -> float:
        return float(self._raw["maker_shadow"]["extreme_tick_size"])

    @property
    def log_dir(self) -> Path:
        d = Path(self._raw["logging"]["directory"])
        if not d.is_absolute():
            d = self._path.parent / d
        return d

    @property
    def jsonl_enabled(self) -> bool:
        return bool(self._raw["logging"].get("jsonl_enabled", True))
