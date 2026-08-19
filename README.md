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
- `/healthz` — liveness check for the dashboard process itself

## Monitor states

A monitor is `up`, `degraded`, or `down` (`unknown` until its first checks
land):

- `down` after `failure_threshold` consecutive failed checks, which opens an
  incident and notifies the monitor's channels
- `up` after `recovery_threshold` consecutive successes, which resolves the
  open incident
- `degraded` when checks succeed but come back at or above the monitor's
  optional `degraded_latency_ms` budget. It slides `up -> degraded` on a
  single slow check, but takes the full `recovery_threshold` to come back, so
  a target hovering on its budget doesn't flap. A recovery check that's slow
  still resolves the incident -- the monitor is serving again.

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
  web/            # Read-only dashboard: WSGI app, HTML rendering, queries
  reporting.py    # Plain-text status table for `alerts status`
  retention.py    # Deletes history past the configured retention window
  timeutil.py     # Shared UTC helpers (everything stored is UTC, naive on read)
  cli.py          # `alerts run` / `serve` / `status` / `prune` entrypoints
migrations/       # Alembic migrations
tests/
```

## License

MIT, see [LICENSE](LICENSE).
