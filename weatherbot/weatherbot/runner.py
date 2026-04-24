"""
Main scan pipeline runner.

For each cycle:
  1. Discover active weather markets
  2. Parse each market
  3. Map station
  4. Fetch weather forecast + nowcast
  5. Collect orderbook
  6. Calculate EV
  7. Calculate settlement safety
  8. Log observation
  9. Log signal / ghost trade if candidate

One market failure never stops the full scan.
"""
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, date, timezone, timedelta
from typing import Any, Optional

import yaml

from .book_collector import fetch_orderbook, fetch_orderbooks_for_market
from .discovery import RawMarket, discover_all
from .ev_calculator import (
    ACTION_PAPER_MAKER,
    ACTION_PAPER_TAKER,
    ACTION_EXIT_WATCH,
    ACTION_WATCH,
    calculate_ev,
)
from .forecast_engine import compute_forecast
from .ghost_logger import (
    count_open_ghost_trades,
    log_ghost_trade,
    log_observation,
    log_signal,
)
from .nowcast_engine import compute_nowcast
from .parser import (
    MARKET_TYPE_DAILY_HIGH,
    MARKET_TYPE_DAILY_LOW,
    ParsedMarket,
    parse_market,
)
from .settlement_safety import compute_safety, is_candidate_eligible
from .station_mapper import map_parsed_market

logger = logging.getLogger(__name__)

_SETTINGS_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "settings.yaml")

_settings_cache: Optional[dict] = None


def _load_settings() -> dict:
    global _settings_cache
    if _settings_cache is not None:
        return _settings_cache

    path = os.path.abspath(_SETTINGS_PATH)
    if not os.path.exists(path):
        logger.warning("settings.yaml not found, using defaults")
        _settings_cache = {}
        return _settings_cache

    with open(path, "r") as f:
        _settings_cache = yaml.safe_load(f) or {}
    return _settings_cache


def _parse_close_time(close_time_str: Optional[str]) -> Optional[datetime]:
    if not close_time_str:
        return None
    for fmt in (
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(close_time_str, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def _hours_to_close(close_time_str: Optional[str]) -> Optional[float]:
    dt = _parse_close_time(close_time_str)
    if dt is None:
        return None
    now = datetime.now(timezone.utc)
    delta = (dt - now).total_seconds() / 3600.0
    return delta


def _get_target_date(close_time_str: Optional[str]) -> date:
    dt = _parse_close_time(close_time_str)
    if dt:
        # Use the close date as the target day
        return dt.date()
    return datetime.now(timezone.utc).date()


@dataclass
class ScanStats:
    markets_discovered: int = 0
    markets_parsed_ok: int = 0
    markets_parse_failed: int = 0
    markets_blacklisted: int = 0
    markets_precipitation: int = 0
    markets_skipped_closed: int = 0
    markets_processed: int = 0
    signals_generated: int = 0
    ghost_trades_logged: int = 0
    errors: list[str] = field(default_factory=list)


def process_market(
    raw: RawMarket,
    settings: dict,
    include_unknown_cities: bool = True,
    hours_to_close_reject: float = 4.0,
    max_open_ghost_trades: int = 8,
) -> dict:
    """
    Full pipeline for a single market. Returns result dict with outcome info.
    Never raises.
    """
    result = {
        "market_id": raw.market_id,
        "status": "unknown",
        "action": "SKIP",
        "reason": None,
        "signal_logged": False,
        "ghost_trade_logged": False,
    }

    try:
        # --- Parse ---
        parsed = parse_market(raw.market_id, raw.question, raw.outcomes)

        if parsed.parse_failed:
            result["status"] = "parse_failed"
            result["reason"] = parsed.parse_failure_reason
            _log_obs_minimal(raw, parsed, "parse_failed", parsed.parse_failure_reason, settings)
            return result

        # --- Station map ---
        station = map_parsed_market(parsed.city)

        # Unknown city: allow in paper mode if configured
        if not station["station_known"] and not include_unknown_cities:
            result["status"] = "skipped_unknown_city"
            result["reason"] = "unknown_city_excluded"
            return result

        # --- Check hours to close ---
        htc = _hours_to_close(raw.close_time)
        if htc is not None and htc < 0:
            result["status"] = "skipped_expired"
            result["reason"] = "market_already_closed"
            return result

        target_date = _get_target_date(raw.close_time)

        # --- Settlement safety ---
        safety = compute_safety(
            city=parsed.city,
            market_type=parsed.market_type,
            resolution_source_text=parsed.resolution_source_text,
            risk_keywords_found=parsed.risk_keywords_found,
            station_risk=station["risk"],
            manipulation_flag=parsed.manipulation_flag,
            is_precipitation=parsed.is_precipitation,
            parse_failed=False,
            station_known=station["station_known"],
        )

        ev_settings = settings.get("ev", {})
        min_safety = ev_settings.get("min_settlement_safety_candidate", 0.65)

        # --- Fetch weather (only if station has coordinates) ---
        lat = station.get("lat")
        lon = station.get("lon")

        forecast_result = None
        nowcast_result = None

        if lat is not None and lon is not None:
            if parsed.market_type in (MARKET_TYPE_DAILY_HIGH, MARKET_TYPE_DAILY_LOW):
                bias_config = settings.get("bias_correction", {})
                forecast_result = compute_forecast(
                    lat=lat,
                    lon=lon,
                    target_date=target_date,
                    market_type=parsed.market_type,
                    unit=parsed.unit or station.get("unit") or "C",
                    bucket_low=parsed.bucket_low,
                    bucket_high=parsed.bucket_high,
                    open_ended_low=parsed.open_ended_low,
                    open_ended_high=parsed.open_ended_high,
                    icao=station.get("icao"),
                    bias_config=bias_config,
                )

                nowcast_result = compute_nowcast(
                    icao=station.get("icao"),
                    market_type=parsed.market_type,
                    unit=parsed.unit or station.get("unit") or "C",
                    bucket_low=parsed.bucket_low,
                    bucket_high=parsed.bucket_high,
                    open_ended_low=parsed.open_ended_low,
                    open_ended_high=parsed.open_ended_high,
                    timezone_name=station.get("timezone"),
                )

        model_prob = forecast_result.model_probability if forecast_result else 0.5
        ensemble_agreement = forecast_result.ensemble_agreement if forecast_result else 0.0
        model_spread = forecast_result.model_spread if forecast_result else 0.0

        nowcast_prob = nowcast_result.nowcast_probability if nowcast_result else None
        nowcast_conf = nowcast_result.nowcast_confidence if nowcast_result else "none"
        current_temp = nowcast_result.current_temp if nowcast_result else None

        # --- Orderbook ---
        book_result = None
        best_bid = None
        best_ask = None
        bid_size = None
        ask_size = None
        spread = None
        top_book_depth = 0.0
        display_mode = "unknown"
        active_token_id = None

        if raw.token_ids:
            # Use first token as primary (typically "Yes" outcome)
            active_token_id = raw.token_ids[0]
            book_result = fetch_orderbook(active_token_id)
            if book_result:
                best_bid = book_result.best_bid
                best_ask = book_result.best_ask
                bid_size = book_result.bid_size
                ask_size = book_result.ask_size
                spread = book_result.spread
                top_book_depth = book_result.top_book_depth
                display_mode = book_result.display_price_mode

        # --- EV calculation ---
        sizing_cfg = settings.get("sizing", {})
        stake = sizing_cfg.get("default_stake_usdc", 2.0)
        max_stake = sizing_cfg.get("max_stake_usdc", 5.0)
        bankroll = sizing_cfg.get("bankroll_usdc", 30.0)
        kelly_frac = sizing_cfg.get("kelly_fraction", 0.25)
        max_open = sizing_cfg.get("max_open_ghost_trades", 8)

        ev = calculate_ev(
            model_probability=model_prob,
            nowcast_probability=nowcast_prob,
            nowcast_confidence=nowcast_conf,
            best_bid=best_bid,
            best_ask=best_ask,
            bid_size=bid_size,
            ask_size=ask_size,
            spread=spread,
            top_book_depth=top_book_depth,
            display_price_mode=display_mode,
            ensemble_agreement=ensemble_agreement,
            model_spread=model_spread,
            settlement_safety=safety.score,
            hours_to_close=htc or 999.0,
            stake_usdc=stake,
            max_stake_usdc=max_stake,
            bankroll=bankroll,
            kelly_fraction=kelly_frac,
            fee_buffer=ev_settings.get("fee_buffer", 0.02),
            slippage_buffer=ev_settings.get("slippage_buffer", 0.01),
            confidence_buffer=ev_settings.get("confidence_buffer", 0.02),
            maker_edge_threshold=ev_settings.get("maker_edge_threshold", 0.08),
            taker_edge_threshold=ev_settings.get("taker_edge_threshold", 0.12),
            maker_strong_threshold=ev_settings.get("maker_strong_threshold", 0.15),
            taker_strong_threshold=ev_settings.get("taker_strong_threshold", 0.18),
            max_spread=ev_settings.get("max_spread_candidate", 0.10),
            min_depth_multiplier=ev_settings.get("min_book_depth_multiplier", 2.0),
            min_safety=min_safety,
            close_hours_reject=ev_settings.get("hours_to_close_reject", 4.0),
            taker_requires_stale=ev_settings.get("taker_requires_stale_flag", True),
        )

        # Hard-reject safety check overrides EV action
        if safety.hard_reject and ev.action in (ACTION_PAPER_MAKER, ACTION_PAPER_TAKER):
            ev.action = "SKIP"
            ev.reason = f"safety_hard_reject:{safety.hard_reject_reason}"

        reject_reason = None
        if ev.action == "SKIP":
            reject_reason = ev.reason
        if safety.hard_reject:
            reject_reason = f"hard_reject:{safety.hard_reject_reason}"
        if parsed.parse_failed:
            reject_reason = f"parse_failed:{parsed.parse_failure_reason}"

        # --- Log observation (always) ---
        log_observation(
            market_id=raw.market_id,
            event_id=raw.event_id,
            slug=raw.slug,
            question=raw.question,
            city=parsed.city,
            station_code=station.get("icao"),
            unit=parsed.unit or station.get("unit"),
            market_type=parsed.market_type,
            bucket_label=parsed.bucket_label,
            bucket_low=parsed.bucket_low,
            bucket_high=parsed.bucket_high,
            close_time_utc=raw.close_time,
            hours_to_close=htc,
            resolution_source=parsed.resolution_source_text,
            settlement_safety_score=safety.score,
            blacklist_flag=safety.is_blacklisted,
            best_bid=best_bid,
            best_ask=best_ask,
            bid_size=bid_size,
            ask_size=ask_size,
            spread=spread,
            top_book_depth=top_book_depth,
            display_price_mode=display_mode,
            model_probability=model_prob,
            ensemble_agreement=ensemble_agreement,
            model_spread=model_spread,
            nowcast_probability=nowcast_prob,
            current_temp=current_temp,
            edge_gross=ev.edge_gross,
            edge_net_maker=ev.edge_net_maker,
            edge_net_taker=ev.edge_net_taker,
            recommended_action=ev.action,
            recommended_price=ev.recommended_price,
            recommended_size_usdc=ev.recommended_size_usdc,
            reject_reason=reject_reason,
            raw_refs={
                "market_id": raw.market_id,
                "token_ids": raw.token_ids,
                "forecast_model": forecast_result.model_used if forecast_result else None,
                "safety_penalties": safety.penalties_applied,
            },
        )

        # --- Log signal / ghost trade if candidate ---
        if ev.action in (ACTION_PAPER_MAKER, ACTION_PAPER_TAKER, ACTION_EXIT_WATCH, ACTION_WATCH):
            log_signal(
                market_id=raw.market_id,
                token_id=active_token_id,
                city=parsed.city,
                bucket_label=parsed.bucket_label,
                signal_type=ev.signal_type or "unknown",
                action=ev.action,
                model_probability=model_prob,
                blended_probability=ev.blended_probability,
                nowcast_probability=nowcast_prob,
                nowcast_confidence=nowcast_conf,
                best_ask=best_ask,
                best_bid=best_bid,
                spread=spread,
                edge_net_maker=ev.edge_net_maker,
                edge_net_taker=ev.edge_net_taker,
                recommended_price=ev.recommended_price,
                recommended_size_usdc=ev.recommended_size_usdc,
                settlement_safety_score=safety.score,
                stale_flag=ev.stale_flag,
                hours_to_close=htc,
                reason=ev.reason,
            )
            result["signal_logged"] = True

        if ev.action in (ACTION_PAPER_MAKER, ACTION_PAPER_TAKER):
            # Check open trade cap
            open_count = count_open_ghost_trades()
            if open_count >= max_open_ghost_trades:
                result["status"] = "skipped_max_ghost_trades"
                result["reason"] = f"open_ghost_trades_cap_{open_count}"
            else:
                entry_type = "taker" if ev.action == ACTION_PAPER_TAKER else "maker"
                fill_assumption = "immediate_fill" if entry_type == "taker" else "limit_order_fill"

                log_ghost_trade(
                    market_id=raw.market_id,
                    token_id=active_token_id,
                    city=parsed.city,
                    bucket_label=parsed.bucket_label,
                    side="buy",
                    ghost_entry_type=entry_type,
                    ghost_price=ev.recommended_price or best_ask or 0.5,
                    ghost_size_usdc=ev.recommended_size_usdc,
                    signal_type=ev.signal_type or "unknown",
                    edge_net=ev.edge_net_taker if entry_type == "taker" else ev.edge_net_maker,
                    fill_assumption=fill_assumption,
                    status="open",
                )
                result["ghost_trade_logged"] = True

        result["status"] = "processed"
        result["action"] = ev.action
        result["reason"] = ev.reason

    except Exception as exc:
        logger.exception("Error processing market %s: %s", raw.market_id, exc)
        result["status"] = "error"
        result["reason"] = str(exc)

    return result


def _log_obs_minimal(raw: RawMarket, parsed: ParsedMarket, status: str, reason: Optional[str], settings: dict):
    """Log a minimal observation for failed/skipped markets."""
    try:
        log_observation(
            market_id=raw.market_id,
            event_id=raw.event_id,
            slug=raw.slug,
            question=raw.question,
            city=parsed.city,
            station_code=None,
            unit=parsed.unit,
            market_type=parsed.market_type,
            bucket_label=parsed.bucket_label,
            bucket_low=parsed.bucket_low,
            bucket_high=parsed.bucket_high,
            close_time_utc=raw.close_time,
            hours_to_close=_hours_to_close(raw.close_time),
            resolution_source=parsed.resolution_source_text,
            settlement_safety_score=0.0,
            blacklist_flag=parsed.manipulation_flag,
            best_bid=None,
            best_ask=None,
            bid_size=None,
            ask_size=None,
            spread=None,
            top_book_depth=0.0,
            display_price_mode="unknown",
            model_probability=0.5,
            ensemble_agreement=0.0,
            model_spread=0.0,
            nowcast_probability=None,
            current_temp=None,
            edge_gross=0.0,
            edge_net_maker=0.0,
            edge_net_taker=0.0,
            recommended_action="SKIP",
            recommended_price=None,
            recommended_size_usdc=0.0,
            reject_reason=reason,
        )
    except Exception as exc:
        logger.error("Failed to log minimal observation for %s: %s", raw.market_id, exc)


def run_scan(
    max_markets: int = 200,
    include_unknown_cities: bool = True,
    once: bool = True,
    loop_interval_seconds: int = 60,
    paper_only: bool = True,
) -> ScanStats:
    """
    Run one (or infinite) scan cycle(s).
    """
    settings = _load_settings()
    ev_cfg = settings.get("ev", {})
    sizing_cfg = settings.get("sizing", {})

    hours_reject = ev_cfg.get("hours_to_close_reject", 4.0)
    max_open = sizing_cfg.get("max_open_ghost_trades", 8)

    stats = ScanStats()

    logger.info("Starting scan: max_markets=%d include_unknown=%s once=%s",
                max_markets, include_unknown_cities, once)

    while True:
        cycle_start = time.monotonic()
        cycle_stats = _run_cycle(
            settings=settings,
            max_markets=max_markets,
            include_unknown_cities=include_unknown_cities,
            hours_to_close_reject=hours_reject,
            max_open_ghost_trades=max_open,
        )

        # Accumulate stats
        stats.markets_discovered += cycle_stats.markets_discovered
        stats.markets_parsed_ok += cycle_stats.markets_parsed_ok
        stats.markets_parse_failed += cycle_stats.markets_parse_failed
        stats.markets_blacklisted += cycle_stats.markets_blacklisted
        stats.markets_precipitation += cycle_stats.markets_precipitation
        stats.markets_skipped_closed += cycle_stats.markets_skipped_closed
        stats.markets_processed += cycle_stats.markets_processed
        stats.signals_generated += cycle_stats.signals_generated
        stats.ghost_trades_logged += cycle_stats.ghost_trades_logged

        cycle_elapsed = time.monotonic() - cycle_start
        logger.info(
            "Scan cycle complete in %.1fs: discovered=%d parsed_ok=%d signals=%d ghost_trades=%d",
            cycle_elapsed,
            cycle_stats.markets_discovered,
            cycle_stats.markets_parsed_ok,
            cycle_stats.signals_generated,
            cycle_stats.ghost_trades_logged,
        )

        if once:
            break

        sleep_time = max(0, loop_interval_seconds - cycle_elapsed)
        logger.info("Next cycle in %.0fs", sleep_time)
        time.sleep(sleep_time)

    return stats


def _run_cycle(
    settings: dict,
    max_markets: int,
    include_unknown_cities: bool,
    hours_to_close_reject: float,
    max_open_ghost_trades: int,
) -> ScanStats:
    stats = ScanStats()

    raw_markets = discover_all(max_markets=max_markets)
    stats.markets_discovered = len(raw_markets)

    for raw in raw_markets:
        result = process_market(
            raw=raw,
            settings=settings,
            include_unknown_cities=include_unknown_cities,
            hours_to_close_reject=hours_to_close_reject,
            max_open_ghost_trades=max_open_ghost_trades,
        )

        status = result.get("status", "unknown")

        if status == "parse_failed":
            stats.markets_parse_failed += 1
        elif status == "skipped_expired":
            stats.markets_skipped_closed += 1
        elif status == "processed":
            stats.markets_parsed_ok += 1
            stats.markets_processed += 1
            if result.get("signal_logged"):
                stats.signals_generated += 1
            if result.get("ghost_trade_logged"):
                stats.ghost_trades_logged += 1
        elif status in ("error",):
            stats.errors.append(f"{raw.market_id}: {result.get('reason')}")

        # Track specific categories
        parsed_check = parse_market(raw.market_id, raw.question, raw.outcomes)
        if parsed_check.is_precipitation:
            stats.markets_precipitation += 1
        if parsed_check.manipulation_flag:
            stats.markets_blacklisted += 1

    return stats
