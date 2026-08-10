# alerts

A self-hosted uptime and service monitoring tool. It periodically checks
HTTP endpoints, TCP ports, DNS records, and SSL certificate expiry, tracks
each monitor's state (up / degraded / down), and notifies Slack, email, or
a generic webhook when something changes.

## Status

Early development. Current state:

- [x] Config loading and validation (`config.yaml` + env-resolved secrets)
- [x] Database models: monitors, check results, incidents, notification log
- [x] Alembic migrations
- [ ] HTTP checker + scheduler
- [ ] TCP / DNS / SSL checkers
- [ ] Monitor state machine + incident lifecycle
- [ ] Notification engine (dedup, cooldown, escalation)
- [ ] Slack / email / webhook notifiers
- [ ] Web dashboard

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

## Project layout

```
src/alerts/
  config.py       # YAML config loading + validation
  db.py           # SQLAlchemy engine/session setup
  models/         # ORM models: Monitor, CheckResult, Incident, NotificationLog
migrations/       # Alembic migrations
tests/
```

## License

MIT, see [LICENSE](LICENSE).
