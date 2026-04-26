#!/usr/bin/env python3
"""
CLI entry point for the Polymarket Weather Temperature Edge Scanner.

Usage:
  python tools/run_scan.py --once
  python tools/run_scan.py --loop --interval 60
  python tools/run_scan.py --max-markets 100
  python tools/run_scan.py --slug highest-temperature-in-seoul-on-april-27-2026
  python tools/run_scan.py --url https://polymarket.com/event/highest-temperature-in-seoul-on-april-27-2026
  python tools/run_scan.py --discover-weather --max-events 100
  python tools/run_scan.py --discover-weather-only --max-events 100
  python tools/run_scan.py --discover-temperature --max-events 100 --max-pages 20
  python tools/run_scan.py --discover-temperature-only --max-events 100 --max-pages 20
"""
import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from weatherbot.runner import run_scan, run_discover_weather_only, run_discover_temperature_only


def setup_logging(log_level: str = "INFO"):
    log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
    os.makedirs(log_dir, exist_ok=True)

    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(log_dir, "system.log"), encoding="utf-8"),
    ]
    logging.basicConfig(level=getattr(logging, log_level.upper(), logging.INFO),
                        format=fmt, handlers=handlers)


def _print_event_table(heading: str, summaries) -> None:
    if not summaries:
        print(f"\n=== {heading} (none) ===")
        return
    print(f"\n=== {heading} ({len(summaries)}) ===")
    print(f"{'#':<4} {'Slug/Title':<55} {'City':<16} {'Date':<12} {'Mkts':<5} {'Close'}")
    print("-" * 115)
    for i, s in enumerate(summaries, 1):
        label = (s.slug or s.title or "")[:53]
        city = (s.parsed_city or "?")[:14]
        date_str = (s.parsed_date or "?")[:10]
        close = (s.close_time or "?")[:18]
        print(f"{i:<4} {label:<55} {city:<16} {date_str:<12} {s.n_markets:<5} {close}")


def main():
    parser = argparse.ArgumentParser(
        description="Polymarket Weather Temperature Edge Scanner (paper mode)"
    )
    parser.add_argument("--once", action="store_true", default=False,
                        help="Run a single scan cycle and exit")
    parser.add_argument("--loop", action="store_true", default=False,
                        help="Run continuously in a loop")
    parser.add_argument("--interval", type=int, default=60,
                        help="Loop interval in seconds (default: 60)")
    parser.add_argument("--paper-only", action="store_true", default=True,
                        help="Paper/ghost mode only (default: True)")
    parser.add_argument("--include-unknown-cities", action="store_true", default=False,
                        help="Include markets with unknown city mappings")
    parser.add_argument("--max-markets", type=int, default=200,
                        help="Maximum number of markets to scan per cycle")
    parser.add_argument("--log-level", default="INFO",
                        help="Logging level (DEBUG/INFO/WARNING/ERROR)")
    parser.add_argument("--slug", default=None,
                        help="Polymarket event slug for targeted single-market scan")
    parser.add_argument("--url", default=None,
                        help="Polymarket event URL (slug extracted automatically)")
    parser.add_argument("--discover-weather", action="store_true", default=False,
                        help="Broad weather discovery (no tag filter) then full pipeline")
    parser.add_argument("--discover-weather-only", action="store_true", default=False,
                        help="Broad weather discovery — summary output only, no orderbook/model")
    parser.add_argument("--discover-temperature", action="store_true", default=False,
                        help="Strict daily city temperature discovery then full pipeline")
    parser.add_argument("--discover-temperature-only", action="store_true", default=False,
                        help="Strict daily temperature discovery — summary only, no pipeline")
    parser.add_argument("--max-events", type=int, default=100,
                        help="Max events for discovery (default: 100)")
    parser.add_argument("--max-pages", type=int, default=20,
                        help="Max pages to paginate for temperature discovery (default: 20)")
    parser.add_argument("--include-future", action="store_true", default=False,
                        help="Include events beyond the 2-day window in temperature discovery")

    args = parser.parse_args()

    setup_logging(args.log_level)
    logger = logging.getLogger("run_scan")

    # Resolve slug from --url if provided
    slug = args.slug
    if args.url and not slug:
        url = args.url.rstrip("/")
        if "/event/" in url:
            slug = url.split("/event/")[-1]
        else:
            slug = url.split("/")[-1]

    # ── --discover-temperature-only ───────────────────────────────────────────
    if args.discover_temperature_only:
        logger.info(
            "Mode: discover-temperature-only (max_events=%d max_pages=%d include_future=%s)",
            args.max_events, args.max_pages, args.include_future,
        )
        disc = run_discover_temperature_only(
            max_events=args.max_events,
            max_pages=args.max_pages,
            include_future=args.include_future,
        )
        print(f"\n=== DAILY TEMPERATURE DISCOVERY ===")
        print(f"Raw events seen:              {disc.raw_events_seen}")
        print(f"Raw markets seen:             {disc.raw_markets_seen}")
        print(f"Daily temperature events:     {disc.daily_temperature_events_found}")
        print(f"  Active (within 2 days):     {len(disc.summaries)}")
        print(f"  Future watchlist:           {len(disc.future_watchlist)}")
        print(f"Filtered (other weather):     {disc.filtered_other_weather}")
        print(f"Filtered (non-temperature):   {disc.filtered_non_temperature}")

        _print_event_table("ACTIVE TEMPERATURE EVENTS", disc.summaries[:50])
        if disc.future_watchlist:
            _print_event_table("FUTURE WATCHLIST", disc.future_watchlist[:20])

        if disc.daily_temperature_events_found == 0:
            print("\n=== DEBUG: FIRST 30 RAW EVENT SLUGS SEEN ===")
            for i, s in enumerate(disc.first30_raw_event_slugs, 1):
                print(f"  {i:>2}. {s}")
            print("\n=== DEBUG: FIRST 30 RAW MARKET SLUGS SEEN ===")
            for i, s in enumerate(disc.first30_raw_market_slugs, 1):
                print(f"  {i:>2}. {s}")
        return

    # ── --discover-weather-only ───────────────────────────────────────────────
    if args.discover_weather_only:
        logger.info("Mode: discover-weather-only (max_events=%d)", args.max_events)
        summaries = run_discover_weather_only(max_events=args.max_events)
        _print_event_table("WEATHER EVENTS DISCOVERED", summaries)
        print(f"\nTotal: {len(summaries)} weather events")
        return

    if not args.once and not args.loop:
        args.once = True

    once = args.once and not args.loop
    broad_weather = args.discover_weather
    temperature_mode = args.discover_temperature

    logger.info("=" * 60)
    logger.info("Polymarket Weather Temperature Edge Scanner V1")
    logger.info("Mode: paper/ghost only — NO live orders")
    logger.info(
        "once=%s loop=%s interval=%ds max_markets=%d include_unknown=%s "
        "slug=%r broad_weather=%s temperature_mode=%s max_pages=%d",
        args.once, args.loop, args.interval, args.max_markets,
        args.include_unknown_cities, slug, broad_weather, temperature_mode,
        args.max_pages,
    )
    logger.info("=" * 60)

    stats = run_scan(
        max_markets=args.max_markets,
        include_unknown_cities=args.include_unknown_cities,
        once=once,
        loop_interval_seconds=args.interval,
        paper_only=args.paper_only,
        slug=slug,
        broad_weather=broad_weather,
        max_events=args.max_events,
        temperature_mode=temperature_mode,
        max_pages=args.max_pages,
        include_future=args.include_future,
    )

    print("\n=== SCAN COMPLETE ===")
    print(f"Markets discovered:    {stats.markets_discovered}")
    print(f"Markets parsed OK:     {stats.markets_parsed_ok}")
    print(f"Markets parse failed:  {stats.markets_parse_failed}")
    print(f"Markets blacklisted:   {stats.markets_blacklisted}")
    print(f"Markets precipitation: {stats.markets_precipitation}")
    print(f"Markets skipped/closed:{stats.markets_skipped_closed}")
    print(f"Markets non-weather:   {stats.markets_skipped_non_weather}")
    print(f"Markets processed:     {stats.markets_processed}")
    print(f"Signals generated:     {stats.signals_generated}")
    print(f"Ghost trades logged:   {stats.ghost_trades_logged}")
    if stats.errors:
        print(f"Errors ({len(stats.errors)}):")
        for e in stats.errors[:10]:
            print(f"  {e}")


if __name__ == "__main__":
    main()
