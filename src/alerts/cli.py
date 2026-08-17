"""Command-line entrypoint: `alerts run|serve [--config PATH]`."""
from __future__ import annotations

import argparse
import asyncio
import logging
from typing import List, Optional

from alerts.config import load_config
from alerts.db import Database
from alerts.notifiers.registry import build_notifiers
from alerts.scheduler import Scheduler, sync_monitors
from alerts.web.server import serve


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="alerts")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Load config and run the check scheduler.")
    run_parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")

    serve_parser = subparsers.add_parser("serve", help="Run the read-only web dashboard.")
    serve_parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")

    return parser.parse_args(argv)


async def _run(config_path: str) -> None:
    config = load_config(config_path)
    database = Database(config.database.url)
    database.create_all()

    with database.session() as session:
        sync_monitors(session, config.monitors)

    notifiers = build_notifiers(config.channels)
    scheduler = Scheduler(
        database,
        max_concurrent_checks=config.scheduler.max_concurrent_checks,
        notifiers=notifiers,
        cooldown_seconds=config.notifications.cooldown_seconds,
    )
    await scheduler.run()


def _serve(config_path: str) -> None:
    config = load_config(config_path)
    database = Database(config.database.url)
    database.create_all()
    serve(database, config.dashboard.host, config.dashboard.port)


def main(argv: Optional[List[str]] = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = _parse_args(argv)
    if args.command == "run":
        try:
            asyncio.run(_run(args.config))
        except KeyboardInterrupt:
            pass
    elif args.command == "serve":
        _serve(args.config)


if __name__ == "__main__":
    main()
