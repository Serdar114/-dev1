"""
run.py — Entry point for the research bot.

Usage:
    python run.py
    python run.py --config config/settings.yaml
    python run.py --phase 0a      # override phase from command line

The bot will:
1. Load config from config/settings.yaml + config/kill_conditions.yaml
2. Start feed adapters (Polymarket RTDS)
3. Iterate over BTC 5m windows
4. Evaluate signals and simulate paper trades (phases 0c / 1)
5. Check kill conditions after every window
6. Print final summary on exit or kill

PAPER TRADING ONLY — no live orders are placed.
See README.md for paper-to-live gap warnings.
"""

import argparse
import asyncio
import sys

from bot import ResearchBot, SessionKillError, load_config, setup_logging


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="BTC 5m Research Bot (paper only)")
    p.add_argument(
        "--config",
        default="config",
        help="Path to config directory (default: config/)",
    )
    p.add_argument(
        "--phase",
        choices=["0a", "0b", "0c", "1"],
        default=None,
        help="Override phase from config (0a|0b|0c|1)",
    )
    return p.parse_args()


async def main() -> None:
    args = parse_args()

    config = load_config(args.config)

    if args.phase is not None:
        config["phase"] = args.phase

    setup_logging(config)

    import logging
    logger = logging.getLogger("run")
    logger.info(
        "Starting research bot — phase=%s", config.get("phase", "0c")
    )

    bot = ResearchBot(config)
    try:
        await bot.run()
    except SessionKillError as e:
        logger.critical("Session killed: %s", e)
        sys.exit(1)
    except KeyboardInterrupt:
        logger.info("Exiting (KeyboardInterrupt)")
        sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())
