"""Command-line entrypoint: `alerts run|serve|status|prune [--config PATH]`."""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import List, Optional

from alerts.config import load_config
from alerts.db import Database
from alerts.notifiers.registry import build_notifiers
from alerts.reporting import collect_status_rows, format_status_summary, format_status_table
from alerts.retention import prune
from alerts.scheduler import Scheduler, sync_monitors
from alerts.web.server import serve

logger = logging.getLogger(__name__)


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="alerts")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Load config and run the check scheduler.")
    run_parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")

    serve_parser = subparsers.add_parser("serve", help="Run the read-only web dashboard.")
    serve_parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")

    status_parser = subparsers.add_parser(
        "status", help="Print the current state of every monitor and exit."
    )
    status_parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    status_parser.add_argument(
        "--checks",
        type=int,
        default=50,
        help="How many recent checks per monitor the uptime figure covers (default: 50).",
    )
    status_parser.add_argument(
        "--exit-code",
        action="store_true",
        help="Exit 1 if any monitor is down, for use as a scripted check.",
    )

    prune_parser = subparsers.add_parser(
        "prune", help="Delete check and incident history past the retention window."
    )
    prune_parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    prune_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be deleted without deleting anything.",
    )

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
        maintenance_windows=config.maintenance,
    )
    await scheduler.run()


def _serve(config_path: str) -> None:
    config = load_config(config_path)
    database = Database(config.database.url)
    database.create_all()
    serve(database, config.dashboard.host, config.dashboard.port)


def _status(config_path: str, checks_window: int, use_exit_code: bool) -> int:
    """Print the status table, returning the process exit code.

    Reads the same database the scheduler writes to without starting one, so
    it's safe to run alongside a live `alerts run`. The non-zero exit is
    opt-in: a status report failing by default would surprise any script that
    just wants the table.
    """
    config = load_config(config_path)
    database = Database(config.database.url)
    database.create_all()

    with database.session() as session:
        rows = collect_status_rows(session, checks_window=checks_window)

    print(format_status_table(rows))
    print()
    print(format_status_summary(rows))

    return 1 if use_exit_code and any(row.is_down for row in rows) else 0


def _prune(config_path: str, dry_run: bool) -> None:
    """Apply the configured retention policy once and log what it removed.

    Deliberately a separate command rather than something `alerts run` does
    on a timer: pruning is a bulk write against the same database the
    scheduler is writing check results into, so when it happens should be the
    operator's call (a nightly cron entry, typically), not a side effect of
    the check loop.
    """
    config = load_config(config_path)
    database = Database(config.database.url)
    database.create_all()

    with database.session() as session:
        summary = prune(session, config.retention, dry_run=dry_run)

    logger.info("%s %s", "would prune" if dry_run else "pruned", summary.describe())


def main(argv: Optional[List[str]] = None) -> int:
    """Dispatch a subcommand, returning the process exit code.

    setuptools' console-script wrapper calls `sys.exit(main())`, so the
    return value is what `alerts status --exit-code` reports to the shell.
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = _parse_args(argv)
    if args.command == "run":
        try:
            asyncio.run(_run(args.config))
        except KeyboardInterrupt:
            pass
    elif args.command == "serve":
        _serve(args.config)
    elif args.command == "status":
        return _status(args.config, checks_window=args.checks, use_exit_code=args.exit_code)
    elif args.command == "prune":
        _prune(args.config, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
