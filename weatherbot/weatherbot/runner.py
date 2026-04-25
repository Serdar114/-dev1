"""
Main scan pipeline runner.

For each cycle:
  1. Discover active weather markets
  2. Parse market (combining question + title + description + rules)
  3. Map station
  4. Fetch weather forecast (using parsed_target_date, NOT close_time)
  5. Collect orderbook (side-specific depths)
  6. Calculate EV (with ensemble validation gating)
  7. Calculate settlement safety (paper/live/blacklist separation)
  8. Log observation (full schema)
  9. Log signal / ghost trade if candidate

One market failure never stops the full scan.
No live orders. No private keys.
"""
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, date, timezone
from typing import Any, Optional

import yaml

from .book_collector import fetch_orderbook
from .discovery import RawMarket, discover_all, discover_by_slug, is_weather_candidate
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
    GHOST_STATUS_FILLED,
    GHOST_STATUS_ORDER_PLACED,
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

# Token IDs that indicate a broken extraction — must never be sent to the CLOB.
_INVALID_TOKEN_IDS: frozenset[str] = frozenset([
    "[", "]", ",", "{", "}", "YES", "NO", "", "null", "None", "true", "false",
])

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


def _is_valid_token_id(token_id: Optional[str]) -> bool:
    """Return True only for token IDs that are safe to send to the CLOB API."""
    if not token_id:
        return False
    if token_id in _INVALID_TOKEN_IDS:
        return False
    # Polymarket CLOB asset IDs are large decimal integers (50+ digits) or
    # hex strings; anything shorter than 5 chars is almost certainly garbage.
    if len(token_id) < 5:
        return False
    return True


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
    return (dt - now).total_seconds() / 3600.0


@dataclass
class ScanStats:
    markets_discovered: int = 0
    markets_parsed_ok: int = 0
    markets_parse_failed: int = 0
    markets_blacklisted: int = 0
    markets_precipitation: int = 0
    markets_skipped_closed: int = 0
    markets_missing_target_date: int = 0
    markets_skipped_non_weather: int = 0
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
    Full pipeline for a single market. Returns result dict.
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
        # ── Parse (full raw text) ──────────────────────────────────────────────
        parsed = parse_market(
            market_id=raw.market_id,
            question=raw.question,
            outcomes=raw.outcomes,
            title=raw.title,
            description=raw.description,
            rules=raw.rules,
            resolution_text=raw.resolution_source,
            close_time=raw.close_time,
        )

        # ── Non-weather guard ─────────────────────────────────────────────────
        if not is_weather_candidate(raw.question, slug=raw.slug or ""):
            result["status"] = "skipped_non_weather"
            result["reason"] = "no_weather_signal"
            return result

        # ── Hours to close (lifecycle only) ───────────────────────────────────
        htc = _hours_to_close(raw.close_time)
        if htc is not None and htc < 0:
            result["status"] = "skipped_expired"
            result["reason"] = "market_already_closed"
            return result

        # ── Station map ───────────────────────────────────────────────────────
        station = map_parsed_market(parsed.city)

        if not station["station_known"] and not include_unknown_cities:
            result["status"] = "skipped_unknown_city"
            result["reason"] = "unknown_city_excluded"
            return result

        # ── Settlement safety ─────────────────────────────────────────────────
        safety = compute_safety(
            city=parsed.city,
            market_type=parsed.market_type,
            resolution_source_text=parsed.resolution_source_text,
            risk_keywords_found=parsed.risk_keywords_found,
            station_risk=station["risk"],
            manipulation_flag=parsed.manipulation_flag,
            is_precipitation=parsed.is_precipitation,
            parse_failed=parsed.parse_failed,
            station_known=station["station_known"],
            source_type=parsed.source_type,
            include_unknown=include_unknown_cities,
        )

        # Hard blacklist: skip entirely (no log)
        if safety.hard_blacklist:
            result["status"] = "hard_blacklist"
            result["reason"] = safety.reject_reason
            return result

        ev_settings = settings.get("ev", {})
        min_safety = ev_settings.get("min_settlement_safety_candidate", 0.65)

        # ── Forecast (use parsed_target_date, not close_time) ─────────────────
        lat = station.get("lat")
        lon = station.get("lon")
        target_date = parsed.parsed_target_date  # KEY: from question, not close_time

        forecast_result = None
        nowcast_result = None
        forecast_blocked_reason = parsed.forecast_blocked_reason  # e.g. missing_target_date

        if parsed.parse_failed:
            # Log the parse failure as an observation
            _log_obs_failed(raw, parsed, safety, htc, settings)
            result["status"] = "parse_failed"
            result["reason"] = parsed.parse_failure_reason
            return result

        if lat is not None and lon is not None and target_date is not None:
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
                # Use forecast's own blocked reason if it found one
                if forecast_result.probability_blocked_reason:
                    forecast_blocked_reason = forecast_result.probability_blocked_reason

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
        elif target_date is None and parsed.market_type in (MARKET_TYPE_DAILY_HIGH, MARKET_TYPE_DAILY_LOW):
            forecast_blocked_reason = "missing_target_date"
        elif lat is None or lon is None:
            forecast_blocked_reason = "no_coordinates"

        model_prob = forecast_result.model_probability if forecast_result else 0.5
        ensemble_agreement = forecast_result.ensemble_agreement if forecast_result else 0.0
        model_spread = forecast_result.model_spread if forecast_result else 0.0
        n_members = forecast_result.n_members if forecast_result else 0
        det_fallback = forecast_result.deterministic_fallback_used if forecast_result else False
        ensemble_keys = forecast_result.ensemble_keys_detected if forecast_result else 0
        model_used = forecast_result.model_used if forecast_result else None

        nowcast_prob = nowcast_result.nowcast_probability if nowcast_result else None
        nowcast_conf = nowcast_result.nowcast_confidence if nowcast_result else "none"
        current_temp = nowcast_result.current_temp if nowcast_result else None

        # ── Orderbook (side-specific depths) ─────────────────────────────────
        book_result = None
        best_bid = None
        best_ask = None
        bid_size_best = None
        ask_size_best = None
        spread = None
        bid_depth = 0.0
        ask_depth = 0.0
        book_state = "unknown"
        active_token_id = None
        token_id_valid = False

        if raw.token_ids:
            active_token_id = raw.token_ids[0]
            token_id_valid = _is_valid_token_id(active_token_id)
            if not token_id_valid:
                logger.warning(
                    "market %s has invalid token_id %r (token_mapping_failed=%s) — skipping CLOB fetch",
                    raw.market_id, active_token_id, raw.token_mapping_failed,
                )
            elif not raw.token_mapping_failed:
                book_result = fetch_orderbook(active_token_id)
                if book_result:
                    best_bid = book_result.best_bid
                    best_ask = book_result.best_ask
                    bid_size_best = book_result.bid_size_best
                    ask_size_best = book_result.ask_size_best
                    spread = book_result.spread
                    bid_depth = book_result.bid_depth_top_n
                    ask_depth = book_result.ask_depth_top_n
                    book_state = book_result.book_state

        top_book_depth = bid_depth + ask_depth  # combined for legacy log field

        # ── EV calculation ────────────────────────────────────────────────────
        sizing_cfg = settings.get("sizing", {})
        stake = sizing_cfg.get("default_stake_usdc", 2.0)
        max_stake = sizing_cfg.get("max_stake_usdc", 5.0)
        bankroll = sizing_cfg.get("bankroll_usdc", 30.0)
        kelly_frac = sizing_cfg.get("kelly_fraction", 0.25)

        ev = calculate_ev(
            model_probability=model_prob,
            nowcast_probability=nowcast_prob,
            nowcast_confidence=nowcast_conf,
            best_bid=best_bid,
            best_ask=best_ask,
            bid_size=bid_size_best,
            ask_size=ask_size_best,
            spread=spread,
            ask_depth_top_n=ask_depth,
            bid_depth_top_n=bid_depth,
            book_state=book_state,
            ensemble_agreement=ensemble_agreement,
            model_spread=model_spread,
            settlement_safety=safety.score,
            hours_to_close=htc or 999.0,
            n_members=n_members,
            deterministic_fallback_used=det_fallback,
            forecast_blocked_reason=forecast_blocked_reason,
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

        # Paper-ineligible markets: override action to SKIP (log only)
        if not safety.paper_eligible and ev.action in (ACTION_PAPER_MAKER, ACTION_PAPER_TAKER):
            ev.action = "SKIP"
            ev.reason = f"paper_ineligible:{safety.reject_reason}"

        reject_reason = None
        if ev.action == "SKIP":
            reject_reason = ev.reason
        if safety.reject_reason and not safety.live_eligible:
            reject_reason = reject_reason or safety.reject_reason

        # ── Log observation (always for non-blacklisted) ───────────────────────
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
            bid_size=bid_size_best,
            ask_size=ask_size_best,
            spread=spread,
            top_book_depth=top_book_depth,
            display_price_mode=book_state,
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
                "forecast_model": model_used,
                "safety_penalties": safety.penalties_applied,
            },
            extra={
                # Patch 1: target date
                "parsed_target_date": str(parsed.parsed_target_date) if parsed.parsed_target_date else None,
                "forecast_blocked_reason": ev.forecast_blocked_reason,
                # Patch 2: source fields
                "source_type": parsed.source_type,
                "parsed_source_confidence": parsed.parsed_source_confidence,
                "station_code_from_text": parsed.station_code_from_text,
                # Patch 3: eligibility
                "paper_eligible": safety.paper_eligible,
                "live_eligible": safety.live_eligible,
                "hard_blacklist": safety.hard_blacklist,
                # Patch 4: side-specific depths
                "bid_depth_top_n": bid_depth,
                "ask_depth_top_n": ask_depth,
                "entry_side_depth": ev.entry_side_depth,
                "exit_side_depth": ev.exit_side_depth,
                # Patch 6: ensemble validation
                "n_members": n_members,
                "ensemble_keys_detected": ensemble_keys,
                "deterministic_fallback_used": det_fallback,
                # Patch 7: microstructure
                "orderMinSize": raw.order_min_size,
                "orderPriceMinTickSize": raw.order_price_min_tick_size,
                "tick_size": raw.tick_size,
                "min_order_size": raw.min_order_size,
                "accepting_orders": raw.accepting_orders,
                # Patch 3 (token correctness)
                "raw_clobTokenIds_type": type(raw.raw.get("clobTokenIds") or raw.raw.get("clob_token_ids")).__name__,
                "token_ids_count": len(raw.token_ids),
                "outcomes_count": len(raw.outcomes),
                "token_mapping_failed": raw.token_mapping_failed,
                "active_token_id_preview": (active_token_id[:12] + "…") if active_token_id and len(active_token_id) > 12 else active_token_id,
                "active_token_id_valid": token_id_valid,
            },
        )

        # ── Signal + ghost trade ───────────────────────────────────────────────
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
                extra={
                    "parsed_target_date": str(parsed.parsed_target_date) if parsed.parsed_target_date else None,
                    "n_members": n_members,
                    "deterministic_fallback_used": det_fallback,
                    "forecast_blocked_reason": ev.forecast_blocked_reason,
                    "paper_eligible": safety.paper_eligible,
                    "live_eligible": safety.live_eligible,
                },
            )
            result["signal_logged"] = True

        if ev.action in (ACTION_PAPER_MAKER, ACTION_PAPER_TAKER) and safety.paper_eligible:
            open_count = count_open_ghost_trades()
            max_open = sizing_cfg.get("max_open_ghost_trades", 8)
            if open_count >= max_open:
                result["status"] = "skipped_max_ghost_trades"
                result["reason"] = f"open_ghost_trades_cap_{open_count}"
            else:
                entry_type = "taker" if ev.action == ACTION_PAPER_TAKER else "maker"
                fill_assumption = (
                    "immediate_taker_fill" if entry_type == "taker"
                    else "limit_order_pending_touch"
                )
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
                    extra={
                        "parsed_target_date": str(parsed.parsed_target_date) if parsed.parsed_target_date else None,
                        "live_eligible": safety.live_eligible,
                        "n_members": n_members,
                    },
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


def _log_obs_failed(
    raw: RawMarket,
    parsed: ParsedMarket,
    safety: Any,
    htc: Optional[float],
    settings: dict,
):
    """Log a minimal observation record for parse-failed markets."""
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
            hours_to_close=htc,
            resolution_source=parsed.resolution_source_text,
            settlement_safety_score=getattr(safety, "score", 0.0),
            blacklist_flag=parsed.manipulation_flag,
            best_bid=None, best_ask=None, bid_size=None, ask_size=None,
            spread=None, top_book_depth=0.0, display_price_mode="unknown",
            model_probability=0.5, ensemble_agreement=0.0, model_spread=0.0,
            nowcast_probability=None, current_temp=None,
            edge_gross=0.0, edge_net_maker=0.0, edge_net_taker=0.0,
            recommended_action="SKIP",
            recommended_price=None, recommended_size_usdc=0.0,
            reject_reason=f"parse_failed:{parsed.parse_failure_reason}",
            extra={
                "parsed_target_date": None,
                "source_type": parsed.source_type,
                "parsed_source_confidence": parsed.parsed_source_confidence,
                "paper_eligible": getattr(safety, "paper_eligible", True),
                "live_eligible": False,
                "hard_blacklist": False,
                "n_members": 0,
                "deterministic_fallback_used": False,
                "forecast_blocked_reason": parsed.forecast_blocked_reason,
            },
        )
    except Exception as exc:
        logger.error("Failed to log parse-failed observation for %s: %s", raw.market_id, exc)


def run_scan(
    max_markets: int = 200,
    include_unknown_cities: bool = True,
    once: bool = True,
    loop_interval_seconds: int = 60,
    paper_only: bool = True,
    slug: Optional[str] = None,
) -> ScanStats:
    settings = _load_settings()
    ev_cfg = settings.get("ev", {})
    sizing_cfg = settings.get("sizing", {})

    hours_reject = ev_cfg.get("hours_to_close_reject", 4.0)
    max_open = sizing_cfg.get("max_open_ghost_trades", 8)

    stats = ScanStats()

    logger.info(
        "Starting scan: max_markets=%d include_unknown=%s once=%s slug=%r",
        max_markets, include_unknown_cities, once, slug,
    )

    while True:
        cycle_start = time.monotonic()
        cycle_stats = _run_cycle(
            settings=settings,
            max_markets=max_markets,
            include_unknown_cities=include_unknown_cities,
            hours_to_close_reject=hours_reject,
            max_open_ghost_trades=max_open,
            slug=slug,
        )

        stats.markets_discovered += cycle_stats.markets_discovered
        stats.markets_parsed_ok += cycle_stats.markets_parsed_ok
        stats.markets_parse_failed += cycle_stats.markets_parse_failed
        stats.markets_blacklisted += cycle_stats.markets_blacklisted
        stats.markets_precipitation += cycle_stats.markets_precipitation
        stats.markets_skipped_closed += cycle_stats.markets_skipped_closed
        stats.markets_missing_target_date += cycle_stats.markets_missing_target_date
        stats.markets_skipped_non_weather += cycle_stats.markets_skipped_non_weather
        stats.markets_processed += cycle_stats.markets_processed
        stats.signals_generated += cycle_stats.signals_generated
        stats.ghost_trades_logged += cycle_stats.ghost_trades_logged

        elapsed = time.monotonic() - cycle_start
        logger.info(
            "Scan cycle complete in %.1fs: discovered=%d parsed_ok=%d "
            "missing_date=%d signals=%d ghost=%d",
            elapsed,
            cycle_stats.markets_discovered,
            cycle_stats.markets_parsed_ok,
            cycle_stats.markets_missing_target_date,
            cycle_stats.signals_generated,
            cycle_stats.ghost_trades_logged,
        )

        if once:
            break

        sleep_time = max(0, loop_interval_seconds - elapsed)
        logger.info("Next cycle in %.0fs", sleep_time)
        time.sleep(sleep_time)

    return stats


def _run_cycle(
    settings: dict,
    max_markets: int,
    include_unknown_cities: bool,
    hours_to_close_reject: float,
    max_open_ghost_trades: int,
    slug: Optional[str] = None,
) -> ScanStats:
    stats = ScanStats()
    if slug:
        raw_markets = discover_by_slug(slug)
    else:
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
        elif status == "skipped_non_weather":
            stats.markets_skipped_non_weather += 1
        elif status == "skipped_expired":
            stats.markets_skipped_closed += 1
        elif status == "hard_blacklist":
            stats.markets_blacklisted += 1
        elif status == "processed":
            stats.markets_parsed_ok += 1
            stats.markets_processed += 1
            if result.get("signal_logged"):
                stats.signals_generated += 1
            if result.get("ghost_trade_logged"):
                stats.ghost_trades_logged += 1
        elif status == "error":
            stats.errors.append(f"{raw.market_id}: {result.get('reason')}")

        # Count missing target dates (check parsed result)
        parsed_check = parse_market(raw.market_id, raw.question, raw.outcomes)
        if parsed_check.forecast_blocked_reason == "missing_target_date":
            stats.markets_missing_target_date += 1
        if parsed_check.is_precipitation:
            stats.markets_precipitation += 1

    return stats
