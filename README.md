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

Run the checker scheduler:

```bash
alerts run --config config.yaml
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
- `/healthz` — liveness check for the dashboard process itself

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
  cli.py          # `alerts run` / `alerts serve` entrypoints
migrations/       # Alembic migrations
tests/
```

## License

MIT, see [LICENSE](LICENSE).
