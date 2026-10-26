# alerts

A self-hosted uptime and service monitoring tool. It periodically checks
HTTP endpoints, TCP ports, DNS records, and SSL certificate expiry, tracks
each monitor's state (up / degraded / down), and notifies Slack, email, or
a generic webhook when something changes. A read-only web dashboard shows
current status, uptime, and incident history.

> **How this project is built:** I develop this tool with AI coding agents. I decide how monitoring and alerting should behave, review each change and its tests, and merge what holds up.

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
- [x] Uptime SLO targets, error budgets, and `alerts slo`
- [x] Monitor dependencies, so one outage pages once

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
api-gateway  http  prod,payments  down      41.0%   -        -       12s ago     open incident, 2h 4m; alerts held back by legacy-db
main-site    http  prod,web       degraded  99.8%   1624ms   3980ms  8s ago
legacy-db    tcp   prod           down      41.0%   -        -       30s ago     open incident, 2h 5m

3 monitor(s): 1 degraded, 2 down
```

The NOTE column answers two questions during an outage: how long this has
been broken, and why nobody was paged about it -- see
[Dependencies](#dependencies). A held-back monitor still fails
`--exit-code`, since a script asking whether anything is broken is asking
about the estate rather than about who got told.

`--exit-code` makes it exit 1 when anything is down, for use as a scripted
check; without it the command always exits 0. `--monitor NAME` and `--tag
NAME` narrow it to one service or one group, and narrow together rather than
override -- see [Tags](#tags).

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
service, `--tag NAME` narrows it to every monitor carrying that tag, and the
two narrow together when both are given. `--open` lists only what hasn't
recovered, and `--exit-code` exits 1 while any reported incident is open, so
a deploy script can refuse to ship into an ongoing outage.

Mean recovery covers the resolved incidents only. An open one hasn't
recovered, and folding its running duration into the average would make an
outage appear to improve the longer it went on. Total downtime does include
it, because that time is being lost right now. An incident that is still open
is reported however old it is, even when it started before the window --
losing the one outage that is still happening is the one thing a report of
outages must not do.

Report each monitor against the availability it was promised to hold, and
how much of its error budget is left:

```bash
alerts slo --config config.yaml
```

```
MONITOR      TAGS      TARGET  WINDOW  ACTUAL  CHECKS  BUDGET    STATUS    NOTE
api-gateway  prod,api  99.95%  7d      99.21%  40320   spent     BREACHED  0s of 5m left
legacy-db    prod      99.00%  30d     99.40%  86400   40% left  ok        2h 52m of 7h 12m left
main-site    prod,web  99.90%  30d     99.93%  86400   28% left  ok        12m of 43m left

3 monitor(s) with an objective: 2 ok, 1 breached
```

`--monitor NAME` and `--tag NAME` narrow it, and both narrow rather than
override. `--exit-code` exits 1 when a monitor has already *missed* its
target -- not when the budget is merely running low, since an at-risk service
is exactly the one a deploy might be needed to fix, and a gate that blocked
the fix would be working against you.

Monitors with no `slo_target` are left out rather than listed with empty
columns; `alerts status` already lists everything. A paused monitor is shown
and marked, but never fails the gate: nothing is checking it, so its window
slides forward over a history that stopped growing and the figure decays
toward "no data" whatever the service is really doing.

See [SLO targets and error budgets](#slo-targets-and-error-budgets) for what
the numbers mean.

Show what needs what, and whose alerts are being held back because of it:

```bash
alerts deps --config config.yaml
```

```
MONITOR      STATE  DEPENDS ON   UPSTREAM DOWN  NOTE
primary-db   down   -            -              root cause, 2 dependent(s) held back
api-gateway  down   primary-db   primary-db     alerts held back
main-site    down   api-gateway  primary-db     alerts held back

3 monitor(s) in the dependency graph, 2 held back, 1 root cause(s)
```

`--monitor NAME` narrows it to that monitor's neighbourhood, `--tag TAG`
narrows it to the merged neighbourhood of every monitor carrying that tag,
and `--exit-code` exits 1 while any alert is being held back. See
[Dependencies](#dependencies) for what the columns mean and what suppression
does and doesn't do.

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
windows included), `--tag NAME` does the same for every monitor carrying
that tag, and `--exit-code` exits 1 when nothing is active, so a deploy
script can gate on being inside a window:

```bash
alerts maintenance --config config.yaml --monitor api-gateway --exit-code
```

Run the read-only web dashboard (separate process, same database):

```bash
alerts serve --config config.yaml
```

The dashboard listens on `dashboard.host:dashboard.port` from the config
(default `127.0.0.1:8080`) and serves:

- `/` — all monitors, current state, uptime %, average latency, and each
  monitor's SLO standing. `?tag=` narrows it to one group, and the summary
  tiles — including how many objectives are at risk or breached — count the
  filtered set
- `/monitors/<name>` — one monitor's recent checks and incident history
- `/api/monitors` — JSON summary of all monitors; `?tag=` narrows it and the
  response echoes the tag it filtered on
- `/api/monitors/<name>` — JSON detail for one monitor, including recent
  checks, incidents, and its `slo` object
- `/api/summary` — JSON state tally, open-incident count, a `dependencies`
  tally of how many monitors have something down upstream and how many are
  having alerts held back, and an `slo` tally of how many objectives are
  being met; a constant-size response whatever
  the monitor count, for a wallboard or a polling check. `?tag=` narrows the
  state counts to one group
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

## Dependencies

A monitor can declare the monitors it needs in order to be healthy:

```yaml
monitors:
  - name: main-site
    type: http
    target: https://example.com
    depends_on: [api-gateway]

  - name: api-gateway
    type: http
    target: https://api.example.com/healthz
    depends_on: [primary-db]

  - name: primary-db
    type: tcp
    target: db.example.com:5432
```

When the database goes down, all three monitors fail. Without `depends_on`
all three page, and the one naming the cause arrives somewhere in the middle
of them. With it, only `primary-db` pages: while a monitor has something
down upstream, its own alerts are held back.

Held back, not turned off. The check still runs, the result is still
recorded, the incident is still opened, and nothing is written to the
notification log — which is what makes the suppression safe. If `main-site`
is still down once the database recovers, the page goes out then, about a
service that really is still broken on its own.

The relationship is followed all the way up, not one hop. Thresholds and
intervals differ, so `api-gateway` is regularly still reading `up` for a
minute after the database has gone; suppressing only its direct dependents
would page for `main-site` during exactly that window.

Three states of an upstream deliberately do *not* suppress anything:

- `degraded` — the upstream is still serving, so a dependent failing under
  it is its own outage
- `unknown` — nothing has checked it yet, and no evidence is not a reason
  for silence
- paused — its reading is frozen at the moment the config stopped declaring
  it, so it would hold back a live service's alerts forever

Two things are refused at load time. A `depends_on` naming a monitor that
doesn't exist, because it looks configured and suppresses nothing — the
outage it was set up for still pages everything. And a cycle, because every
monitor in one is upstream of every other, so the first to go down holds
back the alerts for all of them and the estate goes quiet at the moment it
should be loudest. Names are matched exactly, with none of the case folding
tags get: a dependency names one specific monitor, and `DB` resolving to
`db` would silently watch the wrong row.

Report the graph and what it is currently keeping quiet:

```bash
alerts deps --config config.yaml
```

```
MONITOR      STATE  DEPENDS ON  UPSTREAM DOWN  NOTE
primary-db   down   -           -              root cause, 2 dependent(s) held back
api-gateway  down   primary-db  primary-db     alerts held back
main-site    down   api-gateway primary-db     alerts held back

3 monitor(s) in the dependency graph, 2 held back, 1 root cause(s)
```

Rows come out upstreams-first, so the chain reads down the page in the
direction the failure travels, and monitors with no edges at all are left
out — an estate of forty services with three dependencies between them
prints four rows.

`UPSTREAM DOWN` and the `NOTE` answer two different questions, and the
report keeps them apart. A *healthy* monitor above a broken database has a
down upstream and nothing being suppressed, because there is no page to
hold back; only a monitor that is failing itself is having an alert kept
from you. `--exit-code` fails on the second, not the first. `--monitor NAME`
narrows the report to that monitor's neighbourhood — itself, what it needs,
and what needs it — which covers both "why is this quiet" and "what does
this take with it".

The same facts appear on the dashboard (a note under the state badge, a
Dependencies block on each monitor's page naming both directions, and a
tile counting what is held back), as `depends_on`, `blocked_by`, and
`alerts_held_back` on both `/api/monitors` routes, as a `dependencies` tally
on `/api/summary`, and in `/metrics`.

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

## SLO targets and error budgets

A monitor can be given an availability target and a rolling window to hold
it over:

```yaml
monitors:
  - name: api-gateway
    type: http
    target: https://api.example.com/healthz
    slo_target: 99.9        # percent
    slo_window_days: 30     # the default
```

Like tags, a target changes nothing about how the monitor is checked or
alerted. What it adds is a number to measure against, and the useful part of
that number is the *budget* it implies: 0.1% of 30 days is 43 minutes of
downtime you are allowed to spend, and knowing how much of it is left is
what turns "we had an outage" into a decision about shipping the next risky
change.

Availability is measured in checks, not in seconds. The scheduler records
one row per check, and a check is the only direct evidence this tool has
that a service was serving. Every duration reported alongside — "31m of 43m
left" — is derived from those counts by taking each check to speak for an
equal share of the window, which over-reports an outage caught by a single
check on a long interval and under-reports one that ended just after a check
passed. The counts are the measurement; the minutes are the translation, and
they sit next to each other for exactly that reason.

A monitor is `ok`, `at-risk` once three quarters of its budget is gone,
`breached` once its measured availability has fallen below the target, and
`no-data` while its window is empty. That last one is its own status rather
than an optimistic `ok`: a monitor that has never been checked hasn't met
its objective, it has failed to produce evidence either way.

Two things are refused at load time rather than left to look configured:

- `slo_window_days` without an `slo_target`, since nothing else reads the
  window
- a target the window cannot afford one failed check of. A window holds
  roughly `window_days * 86400 / interval_seconds` checks and a target
  affords `1 - target/100` of them, so a tight target over a short window at
  a slow interval can afford less than a single failure. Every monitor gets
  one eventually, and when it arrives the objective is breached for the rest
  of the window with no way to earn it back — an arithmetic accident rather
  than an objective anyone chose, and invisible until the first failure. The
  error names the three knobs that fix it.

An exact `slo_target: 100` is exempt from that second rule. It affords
nothing at any window or interval by definition, and someone typing it is
deliberately saying every failure counts.

The same figures appear on the dashboard's SLO column and Objective block,
in `slo` objects on both `/api/monitors` routes, as a tally in
`/api/summary`, and in `/metrics` — all computed from one place, so they
cannot disagree.

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
| `alerts_monitor_depends_on{monitor,type,upstream}` | 1 for each monitor this one depends on |
| `alerts_monitor_upstream_down{monitor,type}` | How many of those are down right now |
| `alerts_monitor_alerts_held_back{monitor,type}` | 1 while this monitor is down and being kept quiet by an upstream |
| `alerts_monitor_slo_target_ratio{monitor,type}` | The monitor's `slo_target`, as a 0-1 ratio |
| `alerts_monitor_slo_window_days{monitor,type}` | Length of the window that target is measured over |
| `alerts_monitor_slo_availability_ratio{monitor,type}` | Measured availability over that window |
| `alerts_monitor_slo_budget_consumed_ratio{monitor,type}` | Fraction of the error budget spent; above 1 means the target is missed |
| `alerts_monitor_slo_status{monitor,type,status}` | 1 for the SLO status a monitor is in, 0 for the others |

The quantile label carries the 0-1 fraction other exporters use
(`quantile="0.95"`), so a dashboard panel written against one of them works
unchanged. It stays a gauge rather than becoming a `summary`: a summary's
quantiles cover the measured process's own lifetime, while these cover a
fixed window of recent checks.

The state gauge is exported once per state so an alert rule can be written
as `alerts_monitor_state{state="down"} == 1` without knowing any numeric
encoding. Paused monitors (below) are left out entirely — a scraper can't
tell a frozen reading from a live one.

`alerts_monitor_alerts_held_back` is worth an alert rule of its own: a
monitor held back for longer than an outage should reasonably last means a
page is being swallowed by a dependency that never came back. It is exported
for every monitor, zeroes included, since a scraper can't tell an absent
series from a reading of zero.

`alerts_monitor_slo_budget_consumed_ratio` is exported unclipped, so
`> 1` is the whole alert rule for a missed objective. A monitor with no
`slo_target` exports none of the `_slo_` series at all, and one whose window
is still empty exports its target and window but nothing measured — a
scraper can't tell an absent series from a real reading of zero, and zero
availability is the loudest thing this exposition can say.

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
200 with a body naming the dependency that's broken. `expected_body_not_contains`
is the opposite assertion -- the check fails if the substring *is* found --
for a maintenance banner or an error notice served alongside an otherwise
healthy status code. Both may be set on the same monitor; each is checked
independently, not as one another's negation.

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
combined with `expected_body_contains` or `expected_body_not_contains`, which
are rejected at load time rather than failing every check.

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
  dependencies.py # The graph of what needs what, and what that suppresses
  envsubst.py     # ${VAR} expansion for config values with a secret inside
  probe.py        # One-shot checks of the configured monitors, writing nothing
  reporting.py    # Plain-text tables for `status`, `check`, `slo`, `deps`,
                  #   `incidents`, `maintenance`
  slo.py          # Error budget arithmetic for availability targets
  retention.py    # Deletes history past the configured retention window
  timeutil.py     # Shared UTC helpers (everything stored is UTC, naive on read)
  cli.py          # `alerts run` / `serve` / `check` / `status` / `slo` /
                  #   `deps` / `incidents` / `maintenance` / `prune`
migrations/       # Alembic migrations
tests/
```

## License

MIT, see [LICENSE](LICENSE).
