"""Command-line entrypoint.

`alerts run|serve|check|status|slo|deps|incidents|maintenance|prune`.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import Callable, List, Optional

from alerts.config import load_config
from alerts.db import Database
from alerts.notifiers.registry import build_notifiers
from alerts.probe import probe_monitors, select_monitors
from alerts.reporting import (
    collect_dependency_rows,
    collect_incident_rows,
    collect_maintenance_rows,
    collect_probe_rows,
    collect_slo_rows,
    collect_status_rows,
    format_dependency_summary,
    format_dependency_table,
    format_incident_summary,
    format_incident_table,
    format_maintenance_summary,
    format_maintenance_table,
    format_probe_summary,
    format_probe_table,
    format_slo_summary,
    format_slo_table,
    format_status_summary,
    format_status_table,
)
from alerts.retention import prune
from alerts.scheduler import Scheduler, sync_monitors
from alerts.web.queries import count_monitors
from alerts.web.server import serve

logger = logging.getLogger(__name__)

# Upper bounds for the numeric options, matching the ones `/api/incidents`
# enforces on the same figures so the two front ends can't disagree about
# what a valid request is. They are generous rather than tight: the point is
# to catch a number that means something other than what was typed, not to
# ration rows.
MAX_CHECKS_WINDOW = 10_000
MAX_INCIDENT_LIMIT = 500
MAX_REPORT_DAYS = 3650


def _bounded_int(minimum: int, maximum: int) -> Callable[[str], int]:
    """An argparse `type` that refuses a number outside its useful range.

    Every numeric option here ends up as a `LIMIT` or a date arithmetic
    operand, and both fail quietly rather than loudly when handed something
    they can't use. `--checks 0` limits the uptime window to nothing and
    reports a monitor checked seconds ago as never checked; `--checks -1`
    becomes `LIMIT -1`, which SQLite reads as no limit at all, so the window
    the operator asked to narrow silently covers the whole retained history.
    A negative `--days` puts the cutoff in the future.

    None of those are answers an operator could tell apart from the truth,
    which is the same reason the dashboard's `/api/incidents` validates its
    query parameters instead of clamping them. Refusing at parse time makes
    argparse print the range and exit 2 before any database is opened.
    """

    def parse(raw: str) -> int:
        try:
            value = int(raw)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{raw!r} is not an integer") from None
        if not minimum <= value <= maximum:
            raise argparse.ArgumentTypeError(
                f"{value} is out of range, expected {minimum} to {maximum}"
            )
        return value

    return parse


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="alerts")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Load config and run the check scheduler.")
    run_parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")

    serve_parser = subparsers.add_parser("serve", help="Run the read-only web dashboard.")
    serve_parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")

    check_parser = subparsers.add_parser(
        "check", help="Check every configured target once and print what came back."
    )
    check_parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    check_parser.add_argument("--monitor", help="Only check this monitor.")
    check_parser.add_argument("--tag", help="Only check monitors carrying this tag.")
    check_parser.add_argument(
        "--exit-code",
        action="store_true",
        help="Exit 1 if any check failed, for gating a deploy or a config change.",
    )

    status_parser = subparsers.add_parser(
        "status", help="Print the current state of every monitor and exit."
    )
    status_parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    status_parser.add_argument(
        "--checks",
        type=_bounded_int(1, MAX_CHECKS_WINDOW),
        default=50,
        help="How many recent checks per monitor the uptime figure covers (default: 50).",
    )
    status_parser.add_argument("--monitor", help="Only this monitor.")
    status_parser.add_argument(
        "--tag",
        help="Only monitors carrying this tag.",
    )
    status_parser.add_argument(
        "--exit-code",
        action="store_true",
        help="Exit 1 if any monitor is down, for use as a scripted check.",
    )

    slo_parser = subparsers.add_parser(
        "slo", help="Report each monitor's availability against its SLO target."
    )
    slo_parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    slo_parser.add_argument("--monitor", help="Only this monitor.")
    slo_parser.add_argument("--tag", help="Only monitors carrying this tag.")
    slo_parser.add_argument(
        "--exit-code",
        action="store_true",
        help="Exit 1 if any monitor is missing its target, for gating a release.",
    )

    deps_parser = subparsers.add_parser(
        "deps", help="Report what each monitor depends on, and what is being held back."
    )
    deps_parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    deps_parser.add_argument(
        "--monitor",
        help="Only this monitor, what it depends on, and what depends on it.",
    )
    deps_parser.add_argument(
        "--tag",
        help="Only monitors carrying this tag, what they depend on, and what depends on them.",
    )
    deps_parser.add_argument(
        "--exit-code",
        action="store_true",
        help="Exit 1 if any monitor's alerts are currently held back by an upstream.",
    )

    incidents_parser = subparsers.add_parser(
        "incidents", help="Report recent incidents, their durations, and mean recovery."
    )
    incidents_parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    incidents_parser.add_argument("--monitor", help="Only incidents for this monitor.")
    incidents_parser.add_argument("--tag", help="Only incidents for monitors carrying this tag.")
    incidents_parser.add_argument(
        "--days",
        type=_bounded_int(0, MAX_REPORT_DAYS),
        default=7,
        help="How far back to report (default: 7). 0 reports everything still kept.",
    )
    incidents_parser.add_argument(
        "--limit",
        type=_bounded_int(1, MAX_INCIDENT_LIMIT),
        default=50,
        help="Most incidents to list (default: 50). The summary covers the listed ones.",
    )
    incidents_parser.add_argument(
        "--open",
        dest="open_only",
        action="store_true",
        help="Only incidents that are still open.",
    )
    incidents_parser.add_argument(
        "--exit-code",
        action="store_true",
        help="Exit 1 if any reported incident is still open.",
    )

    maintenance_parser = subparsers.add_parser(
        "maintenance", help="List maintenance windows and which are active now."
    )
    maintenance_parser.add_argument(
        "--config", default="config.yaml", help="Path to config.yaml"
    )
    maintenance_parser.add_argument(
        "--monitor",
        help="Only windows covering this monitor, including estate-wide ones.",
    )
    maintenance_parser.add_argument(
        "--tag",
        help="Only windows covering a monitor carrying this tag, including estate-wide ones.",
    )
    maintenance_parser.add_argument(
        "--exit-code",
        action="store_true",
        help="Exit 1 if no window is currently active, for gating a deploy script.",
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
        dependencies=config.dependency_graph(),
    )
    await scheduler.run()


def _serve(config_path: str) -> None:
    config = load_config(config_path)
    database = Database(config.database.url)
    database.create_all()
    serve(database, config.dashboard.host, config.dashboard.port)


def _describe_selection(monitor: Optional[str], tag: Optional[str]) -> str:
    """How a filtered-to-nothing run describes what it was asked for."""
    parts = []
    if monitor is not None:
        parts.append(f"named {monitor!r}")
    if tag is not None:
        parts.append(f"tagged {tag!r}")
    return " and ".join(parts)


def _check(
    config_path: str, monitor: Optional[str], tag: Optional[str], use_exit_code: bool
) -> int:
    """Probe the configured targets once and print what came back.

    The only subcommand that opens no database at all. It runs the checks the
    scheduler would run, against the config as written, and reports what
    happened -- without recording a result, moving a monitor's state, opening
    an incident, or notifying anyone. That is what makes it safe to run
    against a live install, repeatedly, from a laptop, while `alerts run` is
    running somewhere else.

    Loading the config is itself half the check: an unknown channel
    reference, a body assertion on a HEAD request, or a `${VAR}` naming a
    variable that isn't set all fail here, before a single request goes out.
    """
    config = load_config(config_path)
    monitors = select_monitors(config.monitors, monitor=monitor, tag=tag)
    if (monitor is not None or tag is not None) and not monitors:
        # Reported rather than rendered as an empty table, and failed under
        # --exit-code: the flag is there to gate a deploy on a service being
        # healthy, and a typo in a name or a tag passing silently is the one
        # way it could fail that nobody would notice.
        print(f"No monitor {_describe_selection(monitor, tag)} in {config_path}.")
        return 1 if use_exit_code else 0

    results = asyncio.run(
        probe_monitors(monitors, max_concurrent=config.scheduler.max_concurrent_checks)
    )
    rows = collect_probe_rows(results)

    print(format_probe_table(rows))
    print()
    print(format_probe_summary(rows))

    return 1 if use_exit_code and any(row.failed for row in rows) else 0


def _status(
    config_path: str,
    checks_window: int,
    monitor: Optional[str],
    tag: Optional[str],
    use_exit_code: bool,
) -> int:
    """Print the status table, returning the process exit code.

    Reads the same database the scheduler writes to without starting one, so
    it's safe to run alongside a live `alerts run`. The non-zero exit is
    opt-in: a status report failing by default would surprise any script that
    just wants the table.

    `monitor` and `tag` narrow it the way they do everywhere else, and both
    narrow rather than override -- which is what makes the table readable on
    an estate large enough to need either.
    """
    config = load_config(config_path)
    database = Database(config.database.url)
    database.create_all()

    with database.session() as session:
        rows = collect_status_rows(session, checks_window=checks_window, monitor=monitor, tag=tag)

    if (monitor is not None or tag is not None) and not rows:
        # "No monitors configured." would be a lie -- there are monitors,
        # just none matching the filter. Failed under --exit-code for the
        # same reason `alerts check` fails: a script asking whether anything
        # named or tagged this is down must not report success because the
        # name or tag was mistyped.
        print(f"No monitor {_describe_selection(monitor, tag)}.")
        return 1 if use_exit_code else 0

    print(format_status_table(rows))
    print()
    print(format_status_summary(rows))

    return 1 if use_exit_code and any(row.is_down for row in rows) else 0


def _slo(
    config_path: str, monitor: Optional[str], tag: Optional[str], use_exit_code: bool
) -> int:
    """Print the error budget table, returning the process exit code.

    `alerts status` answers what is broken now and `alerts incidents` what
    has been breaking; this answers whether what has been breaking is more
    than was promised, which is the question that decides whether the next
    risky change ships.

    Each monitor is measured over its own `slo_window_days`, out of the same
    database the scheduler writes to, so it can be run alongside a live
    `alerts run`. `--exit-code` is what makes it usable as a release gate:
    it fails when a monitor has already missed its target, not when the
    budget is merely running low, because an at-risk service is exactly the
    one a deploy might be needed to fix.
    """
    config = load_config(config_path)
    database = Database(config.database.url)
    database.create_all()

    with database.session() as session:
        rows = collect_slo_rows(session, monitor=monitor, tag=tag)
        known_monitors = count_monitors(session)

    if not rows and known_monitors == 0 and any(m.slo_target for m in config.monitors):
        # The config declares targets and the database has never seen a
        # monitor, so this is an estate that hasn't been synced rather than
        # one with nothing to report. "No monitors have an SLO target" is a
        # flatly false thing to say about the file that was just read, and
        # it points at the wrong fix -- someone would go and check the
        # target they had only just written.
        print(
            f"No monitors in the database yet, though {config_path} declares "
            "SLO targets. Run `alerts run` to sync the config."
        )
        return 0

    if (monitor is not None or tag is not None) and not rows:
        # Distinguished from "nothing has an objective", which is what the
        # empty table would otherwise say about a filter that matched
        # nothing -- and failed under --exit-code for the same reason
        # `alerts check` fails on one: a gate that passes because a name was
        # mistyped is the one way it could fail unnoticed.
        print(
            f"No monitor {_describe_selection(monitor, tag)} has an SLO target "
            f"in {config_path}."
        )
        return 1 if use_exit_code else 0

    print(format_slo_table(rows))
    print()
    print(format_slo_summary(rows))

    return 1 if use_exit_code and any(row.is_breaching for row in rows) else 0


def _deps(
    config_path: str, monitor: Optional[str], tag: Optional[str], use_exit_code: bool
) -> int:
    """Print the dependency table, returning the process exit code.

    Read out of the same database the scheduler writes to, not out of the
    config: the config says what should depend on what, and only the
    database knows which of those things is down right now. That also
    means the report answers for the estate as it is actually running,
    including a monitor whose dependency was removed from the file but
    whose row hasn't been synced yet.

    The question it exists to answer is the one asked during an incident,
    when a service is failing and nobody has been paged: what is upstream
    of it, and is that why it is quiet? `--exit-code` is the scripted form
    of the same question, for a deploy gate that shouldn't run while the
    estate is being kept quiet by something underneath it.

    `--tag` narrows it to the neighbourhood of every monitor carrying that
    tag at once, merged and de-duplicated the same way `--tag` is resolved
    for `alerts maintenance`: against the config, since that is where tags
    live, with each matching monitor's upstreams and downstreams folded into
    one table rather than printed once per monitor. Combines with
    `--monitor` rather than overriding it.
    """
    config = load_config(config_path)
    database = Database(config.database.url)
    database.create_all()

    with database.session() as session:
        if tag is None:
            rows = collect_dependency_rows(session, monitor=monitor)
        else:
            subjects = select_monitors(config.monitors, monitor=monitor, tag=tag)
            if not subjects:
                print(f"No monitor {_describe_selection(monitor, tag)} in {config_path}.")
                return 1 if use_exit_code else 0
            rows = []
            seen = set()
            for subject in subjects:
                for row in collect_dependency_rows(session, monitor=subject.name):
                    if row.name not in seen:
                        seen.add(row.name)
                        rows.append(row)
        known_monitors = count_monitors(session)

    if not rows and known_monitors == 0 and any(m.depends_on for m in config.monitors):
        # The config declares dependencies and the database has never seen a
        # monitor, so this is an estate that hasn't been synced rather than
        # one with no edges. "No monitor declares a dependency" is a flatly
        # false thing to say about the file that was just read, and it
        # points at the wrong fix.
        print(
            f"No monitors in the database yet, though {config_path} declares "
            "dependencies. Run `alerts run` to sync the config."
        )
        return 0

    if monitor is not None and tag is None and not rows:
        # Distinguished from "nothing declares a dependency", which is what
        # the empty table would otherwise say about a filter that matched
        # nothing -- and failed under --exit-code for the same reason every
        # other filter here fails on one: a gate that passes because a name
        # was mistyped is the one way it could fail unnoticed.
        print(f"No monitor named {monitor!r} is part of the dependency graph.")
        return 1 if use_exit_code else 0

    print(format_dependency_table(rows))
    print()
    print(format_dependency_summary(rows))

    return 1 if use_exit_code and any(row.alerts_held_back for row in rows) else 0


def _incidents(
    config_path: str,
    *,
    monitor: Optional[str],
    tag: Optional[str],
    days: int,
    limit: int,
    open_only: bool,
    use_exit_code: bool,
) -> int:
    """Print the incident report, returning the process exit code.

    `alerts status` answers what is broken now; this answers what has been
    breaking, which is the question asked in a Monday morning review rather
    than during an outage. Both read the same live database without starting
    a scheduler.

    `--tag` narrows it to every monitor carrying that tag, the same way it
    narrows `alerts status`, and combines with `--monitor` rather than
    overriding it. A `--monitor` or `--tag` matching nothing reports zero
    incidents rather than erroring, the same treatment an unknown monitor
    name already got: the database, not the config, is what incidents are
    read from, and a group that is clean looks identical to one that was
    mistyped either way.

    `--days 0` is the escape hatch for "everything": the window becomes
    unbounded and the report reaches as far back as retention has kept,
    which is the only honest limit there is.
    """
    config = load_config(config_path)
    database = Database(config.database.url)
    database.create_all()

    with database.session() as session:
        rows = collect_incident_rows(
            session,
            monitor=monitor,
            tag=tag,
            since_days=days if days > 0 else None,
            open_only=open_only,
            limit=limit,
        )

    print(format_incident_table(rows))
    print()
    print(format_incident_summary(rows))

    return 1 if use_exit_code and any(row.is_open for row in rows) else 0


def _maintenance(
    config_path: str, monitor: Optional[str], tag: Optional[str], use_exit_code: bool
) -> int:
    """Print the maintenance window table, returning the process exit code.

    Reads only the config -- no database is opened, since a window is a
    declaration rather than something the scheduler records. The `--exit-code`
    form is meant for gating: `alerts maintenance --monitor api --exit-code`
    in front of a deploy script fails unless that service is actually covered
    right now, which catches the window that was configured for the wrong
    weekday before the pages start rather than after.

    `--tag` narrows it the same way `--monitor` does, but to every monitor
    carrying that tag at once -- an estate-wide window still counts, since it
    covers them whether or not it names any monitor. Both narrow together, as
    everywhere else a `--monitor` and a `--tag` can be given side by side.
    """
    config = load_config(config_path)

    if monitor is None and tag is None:
        rows = collect_maintenance_rows(config.maintenance)
    else:
        subjects = select_monitors(config.monitors, monitor=monitor, tag=tag)
        if not subjects:
            print(f"No monitor {_describe_selection(monitor, tag)} in {config_path}.")
            return 1 if use_exit_code else 0
        rows = []
        seen = set()
        for subject in subjects:
            for row in collect_maintenance_rows(config.maintenance, monitor=subject.name):
                if row.name not in seen:
                    seen.add(row.name)
                    rows.append(row)

    print(format_maintenance_table(rows))
    print()
    print(format_maintenance_summary(rows))

    return 1 if use_exit_code and not any(row.is_active for row in rows) else 0


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


def _configure_logging() -> None:
    """Set up logging for a command-line run.

    httpx logs a line per request at INFO, which is the wrong level for this
    process: a request is not an event here, it is the unit of work. Left
    alone, `alerts run` writes a line per monitor per interval forever -- a
    hundred thousand lines a day for a modest estate, burying the transitions
    and notification failures that are the only things worth reading -- and
    `alerts check` interleaves them with the table it is printing.

    Lowered rather than silenced: a warning from the transport is still worth
    seeing, and the checkers already report the failures that matter as check
    outcomes, with the target named.
    """
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main(argv: Optional[List[str]] = None) -> int:
    """Dispatch a subcommand, returning the process exit code.

    setuptools' console-script wrapper calls `sys.exit(main())`, so the
    return value is what `alerts status --exit-code` reports to the shell.
    """
    _configure_logging()
    args = _parse_args(argv)
    if args.command == "run":
        try:
            asyncio.run(_run(args.config))
        except KeyboardInterrupt:
            pass
    elif args.command == "serve":
        _serve(args.config)
    elif args.command == "check":
        return _check(
            args.config, monitor=args.monitor, tag=args.tag, use_exit_code=args.exit_code
        )
    elif args.command == "status":
        return _status(
            args.config,
            checks_window=args.checks,
            monitor=args.monitor,
            tag=args.tag,
            use_exit_code=args.exit_code,
        )
    elif args.command == "slo":
        return _slo(
            args.config, monitor=args.monitor, tag=args.tag, use_exit_code=args.exit_code
        )
    elif args.command == "deps":
        return _deps(
            args.config, monitor=args.monitor, tag=args.tag, use_exit_code=args.exit_code
        )
    elif args.command == "incidents":
        return _incidents(
            args.config,
            monitor=args.monitor,
            tag=args.tag,
            days=args.days,
            limit=args.limit,
            open_only=args.open_only,
            use_exit_code=args.exit_code,
        )
    elif args.command == "maintenance":
        return _maintenance(
            args.config, monitor=args.monitor, tag=args.tag, use_exit_code=args.exit_code
        )
    elif args.command == "prune":
        _prune(args.config, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
