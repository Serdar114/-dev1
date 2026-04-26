#!/usr/bin/env python3
"""
CLI entry point for the Polymarket Weather Temperature Edge Scanner.

Usage:
  python tools/run_scan.py --once
  python tools/run_scan.py --loop --interval 60
  python tools/run_scan.py --paper-only
  python tools/run_scan.py --include-unknown-cities
  python tools/run_scan.py --max-markets 100
  python tools/run_scan.py --slug highest-temperature-in-seoul-on-april-27-2026
  python tools/run_scan.py --url https://polymarket.com/event/highest-temperature-in-seoul-on-april-27-2026
  python tools/run_scan.py --discover-weather --max-events 100
  python tools/run_scan.py --discover-weather-only --max-events 100
"""
import argparse
import logging
import os
import sys

# Allow importing from parent
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from weatherbot.runner import run_scan, run_discover_weather_only


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
    parser.add_argument("--max-events", type=int, default=100,
                        help="Max events for broad discovery (default: 100)")

    args = parser.parse_args()

    setup_logging(args.log_level)
    logger = logging.getLogger("run_scan")

    # Resolve slug from --url if provided
    slug = args.slug
    if args.url and not slug:
        # Extract slug from https://polymarket.com/event/<slug>
        url = args.url.rstrip("/")
        if "/event/" in url:
            slug = url.split("/event/")[-1]
        else:
            slug = url.split("/")[-1]

    # --discover-weather-only: summary table only, no full pipeline
    if args.discover_weather_only:
        logger.info("Mode: discover-weather-only (max_events=%d)", args.max_events)
        summaries = run_discover_weather_only(max_events=args.max_events)
        print(f"\n=== WEATHER EVENTS DISCOVERED ({len(summaries)}) ===")
        print(f"{'#':<4} {'Title':<50} {'City':<18} {'Date':<12} {'Mkts':<5} {'Close'}")
        print("-" * 110)
        for i, s in enumerate(summaries, 1):
            title = (s.title or "")[:48]
            city = (s.parsed_city or "?")[:16]
            date_str = (s.parsed_date or "?")[:10]
            close = (s.close_time or "?")[:20]
            print(f"{i:<4} {title:<50} {city:<18} {date_str:<12} {s.n_markets:<5} {close}")
        print(f"\nTotal: {len(summaries)} weather events")
        return

    if not args.once and not args.loop:
        # Default to --once
        args.once = True

    once = args.once and not args.loop

    broad_weather = args.discover_weather

    logger.info("=" * 60)
    logger.info("Polymarket Weather Temperature Edge Scanner V1")
    logger.info("Mode: paper/ghost only — NO live orders")
    logger.info("once=%s loop=%s interval=%ds max_markets=%d include_unknown=%s slug=%r broad_weather=%s",
                args.once, args.loop, args.interval, args.max_markets, args.include_unknown_cities, slug,
                broad_weather)
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
