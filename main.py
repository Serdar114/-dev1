"""
main.py — polybot_zero entry point.

Usage:
    python main.py                          # measurement mode (default)
    python main.py --mode measurement       # explicit measurement mode
    python main.py --mode paper             # paper mode (requires paper.enabled=true in config)
    python main.py --no-ui                  # run without curses UI (logs only)
    python main.py --report                 # print edge report from existing log
    python main.py --verdict                # print verdict from existing log
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

import yaml

# Ensure repo root is on PYTHONPATH
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def load_config(path: str = "config.yaml") -> dict:
    with open(path) as fh:
        return yaml.safe_load(fh)


def setup_logging(log_dir: str) -> None:
    os.makedirs(log_dir, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(os.path.join(log_dir, "system.log")),
            logging.StreamHandler(sys.stderr),
        ],
    )
    # Suppress chatty third-party internals — keep WARNING+ only
    for noisy in (
        "websocket",
        "websocket.client",
        "urllib3",
        "urllib3.connectionpool",
        "requests.packages.urllib3",
        "web3",
        "web3.providers",
        "web3.providers.rpc",
        "web3.middleware",
        "web3.RequestManager",
        "asyncio",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main() -> None:
    parser = argparse.ArgumentParser(description="polybot_zero — BTC 5m measurement")
    parser.add_argument("--mode", default=None, choices=["measurement", "paper"])
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--no-ui", action="store_true", help="Skip curses UI")
    parser.add_argument("--report", action="store_true", help="Print edge report and exit")
    parser.add_argument("--verdict", action="store_true", help="Print verdict and exit")
    args = parser.parse_args()

    config = load_config(args.config)
    setup_logging(config["measurement"]["log_dir"])

    # Override mode from CLI
    if args.mode:
        config["modes"]["default"] = args.mode

    mode = config.get("modes", {}).get("default", "measurement")

    # --- Report-only modes ---
    if args.report:
        from analytics.edge_report import print_report
        print_report(config["measurement"]["log_file"])
        return

    if args.verdict:
        from analytics.verdict import print_verdict
        print_verdict(config["measurement"]["log_file"])
        return

    # --- Paper mode guard ---
    if mode == "paper":
        if not config.get("paper", {}).get("enabled", False):
            print(
                "ERROR: paper mode requested but config.paper.enabled=false.\n"
                "Set paper.enabled: true in config.yaml to activate paper mode.\n"
                "Only do this after successful measurement-only phase."
            )
            sys.exit(1)

    # --- Run ---
    from app.runner import Runner

    logging.getLogger().info("Starting polybot_zero mode=%s", mode)
    runner = Runner(config)

    try:
        runner.start(run_ui=not args.no_ui)
    except KeyboardInterrupt:
        logging.getLogger().info("Interrupted by user")
    finally:
        runner.stop()
        logging.getLogger().info("Shutdown complete")


if __name__ == "__main__":
    main()
