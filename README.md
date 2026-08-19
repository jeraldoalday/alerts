# alerts

A self-hosted uptime and service monitoring tool. It periodically checks
HTTP endpoints, TCP ports, DNS records, and SSL certificate expiry, tracks
each monitor's state (up / degraded / down), and notifies Slack, email, or
a generic webhook when something changes. A read-only web dashboard shows
current status, uptime, and incident history.

## Status

Early development. Current state:

- [x] Config loading and validation (`config.yaml` + env-resolved secrets)
- [x] Database models: monitors, check results, incidents, notification log
- [x] Alembic migrations
- [x] HTTP checker + scheduler
- [x] TCP / DNS / SSL checkers
- [x] Incident lifecycle (open/resolve on top of monitor state)
- [x] Notification engine (dedup, cooldown, escalation)
- [x] Slack / email / webhook notifiers
- [x] Web dashboard
- [x] Latency budgets driving the degraded state
- [x] History retention + `alerts prune`
- [x] `alerts status` for a terminal status report
- [x] Response body assertions for HTTP monitors
- [x] Maintenance windows suppressing alerts during planned work
- [x] Prometheus `/metrics` endpoint

See open issues and commit history for day-to-day progress.

## Development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cp config.example.yaml config.yaml
cp .env.example .env

ruff check .
mypy src
pytest -q
alembic upgrade head
```

## Commands

Run the checker scheduler:

```bash
alerts run --config config.yaml
```

Print the current state of every monitor and exit. It reads the same database
the scheduler writes to, so it's safe to run against a live install:

```bash
alerts status --config config.yaml
```

```
MONITOR      TYPE  STATE     UPTIME  LATENCY  LAST CHECK  NOTE
api-gateway  http  up        100.0%  87ms     12s ago
main-site    http  degraded  99.8%   1624ms   8s ago
legacy-db    tcp   down      41.0%   -        30s ago     open incident, 2h 5m

3 monitor(s): 1 up, 1 degraded, 1 down
```

`--exit-code` makes it exit 1 when anything is down, for use as a scripted
check; without it the command always exits 0.

Delete check and incident history that has aged past the `retention` policy.
There's no automatic pruning -- it's a bulk write against the database the
scheduler is using, so run it from cron when it suits you:

```bash
alerts prune --config config.yaml            # or --dry-run to just count
```

List the configured maintenance windows and which are running right now:

```bash
alerts maintenance --config config.yaml
```

```
WINDOW             SCHEDULE                                  MONITORS     STATUS
nightly-db-patch   sun at 02:00 UTC for 90m                  main-site    in 2d 14h
freeze             daily at 00:00 UTC for 1440m              all          ACTIVE, 12h 0m left
gateway-migration  2026-09-01T01:00 to 2026-09-01T05:00 UTC  api-gateway  in 11d 13h

3 window(s), 1 active
```

`--monitor NAME` narrows it to the windows covering one service (estate-wide
windows included), and `--exit-code` exits 1 when nothing is active, so a
deploy script can gate on being inside a window:

```bash
alerts maintenance --config config.yaml --monitor api-gateway --exit-code
```

Run the read-only web dashboard (separate process, same database):

```bash
alerts serve --config config.yaml
```

The dashboard listens on `dashboard.host:dashboard.port` from the config
(default `127.0.0.1:8080`) and serves:

- `/` — all monitors, current state, uptime %, and average latency
- `/monitors/<name>` — one monitor's recent checks and incident history
- `/api/monitors` — JSON summary of all monitors
- `/api/monitors/<name>` — JSON detail for one monitor, including recent
  checks and incidents
- `/api/summary` — JSON state tally and open-incident count; a constant-size
  response whatever the monitor count, for a wallboard or a polling check
- `/metrics` — Prometheus exposition of the same picture (see below)
- `/healthz` — liveness check for the dashboard process itself

## Metrics

`/metrics` serves the current state in the Prometheus text format, built
from the same queries the dashboard renders, so a scraped series and the
dashboard never disagree:

| Metric | Description |
| --- | --- |
| `alerts_monitors` | Monitors being checked |
| `alerts_open_incidents` | Incidents currently open |
| `alerts_monitor_state{monitor,type,state}` | 1 for the state a monitor is in, 0 for the others |
| `alerts_monitor_uptime_ratio{monitor,type}` | Successful fraction of the recent checks |
| `alerts_monitor_latency_ms{monitor,type}` | Mean latency over the recent checks |
| `alerts_monitor_checks{monitor,type}` | How many checks those two figures cover |
| `alerts_monitor_latency_budget_ms{monitor,type}` | The monitor's `degraded_latency_ms`, if set |

The state gauge is exported once per state so an alert rule can be written
as `alerts_monitor_state{state="down"} == 1` without knowing any numeric
encoding. Paused monitors (below) are left out entirely — a scraper can't
tell a frozen reading from a live one.

## Maintenance windows

The `maintenance:` config section declares spans during which a monitor's
channels are not notified. Checks still run and results and incidents are
still recorded — a window suppresses alerting, not monitoring, so you can
see afterwards what the change did to the service.

Windows come in two forms, and a window uses exactly one of them:

```yaml
maintenance:
  # Recurring: UTC start time plus a duration, optionally limited to
  # certain weekdays. May run past midnight, in which case it belongs to
  # the day it starts on.
  - name: nightly-db-patch
    monitors: [main-site]     # omit to cover every monitor
    days: [sun]               # omit for daily
    start: "02:00"
    duration_minutes: 90

  # One-off: an exact span.
  - name: gateway-migration
    monitors: [api-gateway]
    starts_at: 2026-09-01T01:00:00Z
    ends_at: 2026-09-01T05:00:00Z
```

Nothing is written to the notification log while a window is active, so an
incident that outlives its window is announced the moment the window ends.
One that opens and clears inside the window stays quiet on both edges: a
channel is only told an incident resolved if it was told the incident
opened.

## Paused monitors

Removing a monitor from `monitors:` doesn't delete its row — that would
cascade-delete its check and incident history. It's marked *paused*
instead: the scheduler stops checking it, any incident still open is
resolved, and the dashboard, `/api/*`, and `alerts status` all label it as
paused rather than presenting its last known state as current. Putting the
entry back in the config re-enables it with its history intact.

## Monitor states

A monitor is `up`, `degraded`, or `down` (`unknown` until its first checks
land):

- `down` after `failure_threshold` consecutive failed checks, which opens an
  incident and notifies the monitor's channels
- `up` after `recovery_threshold` consecutive successes, which resolves the
  open incident
- `degraded` when checks succeed but come back at or above the monitor's
  optional `degraded_latency_ms` budget. It slides `up -> degraded` on a
  single slow check, but coming back to `up` takes `recovery_threshold`
  checks that are actually *inside* the budget, so a target hovering on its
  budget doesn't flap. A recovery check that's slow still resolves the
  incident -- the monitor is serving again.

An HTTP monitor can also require a substring in the response body via
`expected_body_contains`, which catches the health endpoint that answers
200 with a body naming the dependency that's broken.

## Project layout

```
src/alerts/
  config.py       # YAML config loading + validation
  db.py           # SQLAlchemy engine/session setup
  models/         # ORM models: Monitor, CheckResult, Incident, NotificationLog
  checkers/       # Per-protocol check implementations (HTTP, TCP, DNS, SSL)
  scheduler.py    # Per-monitor interval loop, state transitions, notification dispatch
  incidents.py    # Opens/resolves Incident records on DOWN/UP transitions
  notifications.py  # Dedup/cooldown/escalation decisions for open incidents
  notifiers/      # Per-channel senders (Slack, webhook, email) + registry
  web/            # Read-only dashboard: WSGI app, HTML rendering, queries, /metrics
  maintenance.py  # Maintenance window schedules and whether one is active
  reporting.py    # Plain-text tables for `alerts status` and `alerts maintenance`
  retention.py    # Deletes history past the configured retention window
  timeutil.py     # Shared UTC helpers (everything stored is UTC, naive on read)
  cli.py          # `alerts run` / `serve` / `status` / `maintenance` / `prune`
migrations/       # Alembic migrations
tests/
```

## License

MIT, see [LICENSE](LICENSE).
