"""
main.py — Entry point for polybot_zero.

Usage:
    python main.py [--config config.yaml] [--mode measurement_only|selective_paper]

Modes:
    measurement_only  (default) — observe and record only, no positions
    selective_paper             — paper trade on proven buckets only

IMPORTANT: Run measurement_only first. Do not switch to selective_paper
without reviewing the verdict output with ChatGPT.
"""

from __future__ import annotations
import argparse
import asyncio
import logging
import sys
from pathlib import Path

import yaml


def load_config(path: str) -> dict:
    cfg_path = Path(path)
    if not cfg_path.exists():
        print(f"ERROR: Config file not found: {path}", file=sys.stderr)
        sys.exit(1)
    with open(cfg_path) as f:
        return yaml.safe_load(f)


def setup_logging(config: dict) -> None:
    log_cfg = config.get("logging", {})
    level = getattr(logging, log_cfg.get("level", "INFO").upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="polybot_zero — BTC 5m measurement engine")
    p.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    p.add_argument(
        "--mode",
        choices=["measurement_only", "selective_paper"],
        default=None,
        help="Override config mode",
    )
    return p.parse_args()


async def main() -> None:
    args = parse_args()
    config = load_config(args.config)

    # CLI mode override
    if args.mode:
        config["mode"] = args.mode

    setup_logging(config)
    logger = logging.getLogger("polybot.main")
    logger.info("polybot_zero starting — mode=%s", config.get("mode"))
    logger.warning(
        "IMPORTANT: This is a measurement system. "
        "Do NOT switch to selective_paper mode without ChatGPT verdict review."
    )

    from app.runner import Runner
    runner = Runner(config)
    await runner.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
