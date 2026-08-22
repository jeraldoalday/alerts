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
- [x] Latency percentiles (p50/p95/p99) everywhere latency is reported
- [x] Request method and headers for HTTP monitors, with `${VAR}` secrets
- [x] `alerts incidents` and `/api/incidents` for incident history and MTTR
- [x] `alerts check` for probing the configured targets on demand
- [x] Tags for grouping monitors, and filtering every report by one

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

Check every configured target once, right now, and print what came back:

```bash
alerts check --config config.yaml
```

```
MONITOR           TYPE  TARGET                           RESULT  LATENCY  DETAIL
main-site         http  https://example.com              ok      118ms
api-gateway       http  https://api.example.com/healthz  slow    1624ms   slower than the 1500ms budget
legacy-db         tcp   db.example.com:5432              FAIL    -        [Errno 61] Connection refused

3 monitor(s) checked, 1 ok, 1 slow, 1 failed
```

This is the only subcommand that opens no database at all. It runs the checks
the scheduler would run, against the config as written, and records nothing:
no check results, no state transitions, no incidents, and nobody notified. So
it's safe to run repeatedly against a live install while `alerts run` is
running elsewhere, and it works against a config whose monitors have never
been synced into a database -- which is the state a new config is in.

Loading the config is half the check on its own. An unknown channel
reference, a body assertion on a HEAD request, or a `${VAR}` naming a
variable that isn't set all fail before a request goes out. The rest is what
the targets actually say, which is the part no amount of validation can tell
you.

`RESULT` has three values, not two. A check that passes at or above its
`degraded_latency_ms` budget reads `slow`: it succeeded, so calling it a
failure would be wrong, but the scheduler seeing that same reading would mark
the monitor degraded, so hiding it would mean hiding a budget that is about
to start firing.

`--monitor NAME` and `--tag NAME` narrow the run, and both narrow rather than
override -- `--monitor api --tag prod` checks `api` only if it's tagged
`prod`. `--exit-code` exits 1 if anything failed, for gating a deploy. A
`--monitor` or `--tag` matching nothing fails under that flag rather than
reporting an empty success: a typo passing silently is the one way a gate
could fail unnoticed.

Print the current state of every monitor and exit. It reads the same database
the scheduler writes to, so it's safe to run against a live install:

```bash
alerts status --config config.yaml
```

```
MONITOR      TYPE  TAGS           STATE     UPTIME  LATENCY  P95     LAST CHECK  NOTE
api-gateway  http  prod,payments  up        100.0%  87ms     104ms   12s ago
main-site    http  prod,web       degraded  99.8%   1624ms   3980ms  8s ago
legacy-db    tcp   prod           down      41.0%   -        -       30s ago     open incident, 2h 5m

3 monitor(s): 1 up, 1 degraded, 1 down
```

`--exit-code` makes it exit 1 when anything is down, for use as a scripted
check; without it the command always exits 0. `--tag NAME` narrows it to one
group -- see [Tags](#tags).

Report the incidents behind that status -- what has been breaking, for how
long, and how quickly it came back:

```bash
alerts incidents --config config.yaml
```

```
MONITOR      STARTED           DURATION      STATE     CAUSE
main-site    2026-08-21 09:55  2h 5m so far  open      timed out after 10s
api-gateway  2026-08-21 07:00  42m           resolved  expected status 200, got 502

2 incident(s), 1 still open, 2h 47m total downtime, mean recovery 42m
```

The window is the last 7 days by default; `--days 30` widens it and `--days 0`
reaches as far back as retention has kept. `--monitor NAME` narrows it to one
service, `--open` lists only what hasn't recovered, and `--exit-code` exits 1
while any reported incident is open, so a deploy script can refuse to ship
into an ongoing outage.

Mean recovery covers the resolved incidents only. An open one hasn't
recovered, and folding its running duration into the average would make an
outage appear to improve the longer it went on. Total downtime does include
it, because that time is being lost right now. An incident that is still open
is reported however old it is, even when it started before the window --
losing the one outage that is still happening is the one thing a report of
outages must not do.

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

- `/` — all monitors, current state, uptime %, and average latency. `?tag=`
  narrows it to one group, and the summary tiles count the filtered set
- `/monitors/<name>` — one monitor's recent checks and incident history
- `/api/monitors` — JSON summary of all monitors; `?tag=` narrows it and the
  response echoes the tag it filtered on
- `/api/monitors/<name>` — JSON detail for one monitor, including recent
  checks and incidents
- `/api/summary` — JSON state tally and open-incident count; a constant-size
  response whatever the monitor count, for a wallboard or a polling check.
  `?tag=` narrows the state counts to one group
- `/api/incidents` — JSON incident feed across every monitor, the counterpart
  of `alerts incidents`. Takes `?monitor=`, `?open`, `?days=`, and `?limit=`,
  and gives each incident a server-computed `duration_seconds` so no consumer
  has to guess what the server's clock says. A parameter that is out of range
  or isn't a number is a 400 rather than a silently shortened list
- `/metrics` — Prometheus exposition of the same picture (see below)
- `/healthz` — liveness check for the dashboard process itself

Every route answers `HEAD` with the headers a `GET` would return and no body,
so the cheapest possible uptime check works against the dashboard too --
including from an `alerts` monitor of its own.

## Tags

A monitor can carry free-form tags, which exist purely for grouping:

```yaml
monitors:
  - name: api-gateway
    type: http
    target: https://api.example.com/healthz
    tags: [prod, payments]
```

They change nothing about how a monitor is checked, alerted, or escalated.
What they change is how much of the estate you have to read to answer a
question about part of it:

```bash
alerts status --config config.yaml --tag prod
alerts check --config config.yaml --tag payments --exit-code
```

The same filter works on the dashboard (`/?tag=prod`, and each tag on the
index is a link to it), on `/api/monitors?tag=prod`, and on
`/api/summary?tag=prod` -- the last of which makes a per-team wallboard
possible, since it counts only that team's monitors and won't turn red for a
service nobody on it owns. Both JSON routes echo back the tag they filtered
on, so a client polling several tags into one view doesn't have to remember
which response is which.

Tags are folded to lowercase and stripped, so `Prod` and `prod` are the same
tag: a monitor missing from a report because of how its tag was capitalised
is the failure mode where someone concludes a service is fine having never
seen it. Commas and whitespace are refused, since tags render as a
comma-joined list.

In `/metrics` each tag is its own series rather than a label on the existing
ones -- a monitor has any number of tags and a Prometheus label holds one
value. It joins the way any info-style series does:

```promql
alerts_monitor_state{state="down"} * on(monitor) group_left alerts_monitor_tag{tag="prod"}
```

One thing is deliberately *not* filtered: the open-incident count, in both
`/api/summary` and the dashboard's tiles, stays estate-wide. Narrowing it
would need a join from incidents back to tags, for the one figure whose job
is to mean the same thing whichever view you are looking at.

## Latency percentiles

A mean latency hides exactly the requests people complain about: nineteen
checks at 90ms and one at four seconds average out to a number that looks
healthy. Everywhere a mean is reported, p50, p95, and p99 over the same
window are reported next to it -- the index table, a monitor's detail page,
both `/api/monitors` routes (as a `latency_percentiles` object), `/metrics`,
and the `alerts status` table.

They are nearest-rank percentiles, so every figure is a latency some check
actually recorded rather than an interpolation between two that never
happened. p95 is also the figure a `degraded_latency_ms` budget is best set
against.

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
| `alerts_monitor_latency_quantile_ms{monitor,type,quantile}` | Latency percentile over the recent checks |
| `alerts_monitor_checks{monitor,type}` | How many checks those two figures cover |
| `alerts_monitor_latency_budget_ms{monitor,type}` | The monitor's `degraded_latency_ms`, if set |
| `alerts_monitor_tag{monitor,type,tag}` | 1 for each tag a monitor carries, for joining on |

The quantile label carries the 0-1 fraction other exporters use
(`quantile="0.95"`), so a dashboard panel written against one of them works
unchanged. It stays a gauge rather than becoming a `summary`: a summary's
quantiles cover the measured process's own lifetime, while these cover a
fixed window of recent checks.

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

## HTTP request options

An HTTP monitor controls how the request is made, not just what is expected
back:

```yaml
monitors:
  - name: api-gateway
    type: http
    target: https://api.example.com/healthz
    headers:
      Authorization: "Bearer ${API_TOKEN}"
      Accept: application/json

  - name: dashboard-itself
    type: http
    target: http://127.0.0.1:8080/healthz
    method: HEAD
```

`method` is `GET`, `HEAD`, or `OPTIONS` — all of them request methods with no
body, since there is nowhere in a monitor's config to put one. `HEAD` is the
cheap option for a target whose status code is the whole signal; it can't be
combined with `expected_body_contains`, which is rejected at load time rather
than failing every check.

Header values may contain `${VAR}` placeholders resolved from the
environment, so a token stays out of the committed config *and* out of the
database — the placeholder is what gets stored, and it is expanded fresh on
each check. `load_config` refuses to start if a placeholder names a variable
that isn't set, and a variable that disappears later is recorded as a failed
check naming it rather than as an unexplained 401.

The same placeholders work in a `webhook` channel's `url` and `headers`, for
receivers that want their token in the path rather than in a header.

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
  envsubst.py     # ${VAR} expansion for config values with a secret inside
  probe.py        # One-shot checks of the configured monitors, writing nothing
  reporting.py    # Plain-text tables for `status`, `check`, `incidents`, `maintenance`
  retention.py    # Deletes history past the configured retention window
  timeutil.py     # Shared UTC helpers (everything stored is UTC, naive on read)
  cli.py          # `alerts run` / `serve` / `check` / `status` / `incidents` /
                  #   `maintenance` / `prune`
migrations/       # Alembic migrations
tests/
```

## License

MIT, see [LICENSE](LICENSE).
