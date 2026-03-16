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
        # signal section is optional (has defaults)
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
    def fee_rate_base(self) -> float:
        return float(self._raw["fees"]["fee_rate_base"])

    @property
    def fee_exponent(self) -> float:
        return float(self._raw["fees"]["fee_exponent"])

    @property
    def maker_rebate_share(self) -> float:
        return float(self._raw["fees"].get("maker_rebate_share", 0.0))

    # Zones — spread sanity
    @property
    def max_spread_warn(self) -> float:
        return float(self._raw["zones"].get("max_spread_warn", 0.05))

    @property
    def max_complement_skew(self) -> float:
        return float(self._raw["zones"].get("max_complement_skew", 0.05))

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
    def max_open_paper_trades_per_window(self) -> int:
        return int(self._raw["risk"].get("max_open_paper_trades_per_window", 1))

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
    def quote_ttl_sec(self) -> float:
        return float(self._raw["maker_shadow"].get("quote_ttl_sec", 20.0))

    # Phase 2 maker evaluation guards
    @property
    def min_passive_edge(self) -> float:
        return float(self._raw["maker_shadow"].get("min_passive_edge", 0.02))

    @property
    def max_spread_maker(self) -> float:
        return float(self._raw["maker_shadow"].get("max_spread_maker", 0.06))

    @property
    def maker_allowed_ste_min(self) -> float:
        return float(self._raw["maker_shadow"].get("allowed_ste_min", 15.0))

    @property
    def maker_intended_notional(self) -> float:
        return float(self._raw["maker_shadow"].get("intended_notional", 1.0))

    # Signal thresholds (regime/pattern classifiers)
    def _sig(self, key: str, default: float) -> float:
        return float(self._raw.get("signal", {}).get(key, default))

    @property
    def trending_abs_delta(self) -> float:
        return self._sig("trending_abs_delta", 0.003)

    @property
    def quiet_abs_delta(self) -> float:
        return self._sig("quiet_abs_delta", 0.0005)

    @property
    def high_vol_sigma_multiple(self) -> float:
        return self._sig("high_vol_sigma_multiple", 3.0)

    @property
    def sustained_move_abs_delta(self) -> float:
        return self._sig("sustained_move_abs_delta", 0.004)

    @property
    def sustained_move_vol_ratio(self) -> float:
        return self._sig("sustained_move_vol_ratio", 2.0)

    @property
    def burst_abs_delta(self) -> float:
        return self._sig("burst_abs_delta", 0.002)

    @property
    def burst_vol_ratio(self) -> float:
        return self._sig("burst_vol_ratio", 4.0)

    @property
    def fade_abs_delta(self) -> float:
        return self._sig("fade_abs_delta", 0.002)

    @property
    def fade_vol_ratio_max(self) -> float:
        return self._sig("fade_vol_ratio_max", 1.5)

    # Taker conviction guards
    @property
    def min_abs_delta_for_taker(self) -> float:
        return self._sig("min_abs_delta_for_taker", 0.0005)

    @property
    def min_confidence_for_taker(self) -> float:
        return self._sig("min_confidence_for_taker", 0.20)

    @property
    def allow_quiet_noise_trades(self) -> bool:
        return bool(self._raw.get("signal", {}).get("allow_quiet_noise_trades", False))

    @property
    def neutral_prob_band_low(self) -> float:
        return self._sig("neutral_prob_band_low", 0.47)

    @property
    def neutral_prob_band_high(self) -> float:
        return self._sig("neutral_prob_band_high", 0.53)

    @property
    def neutral_band_min_delta(self) -> float:
        return self._sig("neutral_band_min_delta", 0.001)

    @property
    def neutral_band_min_confidence(self) -> float:
        return self._sig("neutral_band_min_confidence", 0.30)

    # Adaptive Polymarket refresh cadence
    @property
    def refresh_active_sec(self) -> float:
        return float(self._raw["market"].get("refresh_active_sec", 2.0))

    @property
    def refresh_inactive_sec(self) -> float:
        return float(self._raw["market"].get("refresh_inactive_sec", 15.0))

    # UI
    def _ui(self, key: str, default):
        return self._raw.get("ui", {}).get(key, default)

    @property
    def ui_enabled(self) -> bool:
        return bool(self._ui("enabled", False))

    @property
    def ui_refresh_sec(self) -> float:
        return float(self._ui("refresh_sec", 1.0))

    @property
    def ui_show_log_lines(self) -> int:
        return int(self._ui("show_log_lines", 20))

    @property
    def ui_theme(self) -> str:
        return str(self._ui("theme", "sniper_red"))

    @property
    def ui_compact_mode(self) -> bool:
        return bool(self._ui("compact_mode", False))

    @property
    def session_subdirs(self) -> bool:
        return bool(self._raw["logging"].get("session_subdirs", False))

    @property
    def log_dir(self) -> Path:
        d = Path(self._raw["logging"]["directory"])
        if not d.is_absolute():
            d = self._path.parent / d
        return d

    def log_dir_for_session(self, session_ts: float) -> Path:
        """Return per-session log directory if session_subdirs is enabled."""
        import datetime
        base = self.log_dir
        if self.session_subdirs:
            ts_str = datetime.datetime.utcfromtimestamp(session_ts).strftime("%Y%m%d_%H%M%S")
            return base / ts_str
        return base

    @property
    def jsonl_enabled(self) -> bool:
        return bool(self._raw["logging"].get("jsonl_enabled", True))
