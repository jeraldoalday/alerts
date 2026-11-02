"""Loading and validation for the YAML application config.

Monitors, notification channels, and scheduler defaults are declared in a
single YAML file. Secrets (SMTP passwords, webhook URLs) are never written
into the file directly -- each channel references an environment variable
name, and the value is resolved at load time.
"""
from __future__ import annotations

import os
from datetime import datetime, time
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Union

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from alerts.dependencies import DependencyGraph, describe_cycle, graph_of
from alerts.dependencies import normalise_names as normalise_dependencies
from alerts.envsubst import expand as expand_env
from alerts.envsubst import expand_mapping as expand_env_mapping
from alerts.envsubst import missing as missing_env_vars
from alerts.slo import DEFAULT_WINDOW_DAYS, MAX_TARGET, MIN_TARGET, SECONDS_PER_DAY
from alerts.tags import forbidden_characters, normalise_all
from alerts.tags import normalise as normalise_tag


class DatabaseConfig(BaseModel):
    url: str = "sqlite:///./alerts.db"


class SchedulerConfig(BaseModel):
    default_interval_seconds: int = Field(default=60, ge=5)
    max_concurrent_checks: int = Field(default=20, ge=1)


class NotificationsConfig(BaseModel):
    cooldown_seconds: int = Field(default=900, ge=1)


class RetentionConfig(BaseModel):
    """How long check and incident history is kept before `alerts prune` drops it.

    A `None` age means "keep forever" for that table, which is the escape
    hatch for anyone doing their own archival. `min_check_results_per_monitor`
    is a floor that survives the age cutoff: a monitor checked every 15
    minutes would otherwise have its whole history disappear during a quiet
    stretch, leaving the dashboard with no data to show at all.
    """

    check_results_days: Optional[int] = Field(default=30, ge=1)
    resolved_incidents_days: Optional[int] = Field(default=90, ge=1)
    min_check_results_per_monitor: int = Field(default=20, ge=0)


class DashboardConfig(BaseModel):
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = Field(default=8080, ge=1, le=65535)


WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class MaintenanceWindowConfig(BaseModel):
    """A span during which a monitor's notifications are suppressed.

    Declared one of two ways, and exactly one of them:

    - recurring: `start` (UTC "HH:MM") plus `duration_minutes`, optionally
      narrowed to certain `days`. A window may run past midnight; the day it
      is matched against is the day it *starts* on.
    - fixed: `starts_at` and `ends_at`, for a one-off change window that
      doesn't repeat.

    `monitors` scopes the window to specific monitors by name; omitting it
    covers every monitor, which is what a whole-estate maintenance window
    usually means.

    Checks keep running and results keep being recorded throughout -- the
    window suppresses alerting, not monitoring, so the history of what a
    deploy did to a service is still there afterwards.
    """

    name: str
    monitors: List[str] = Field(default_factory=list)
    days: List[str] = Field(default_factory=list)
    start: Optional[time] = None
    duration_minutes: Optional[int] = Field(default=None, ge=1)
    starts_at: Optional[datetime] = None
    ends_at: Optional[datetime] = None

    @field_validator("days")
    @classmethod
    def days_are_known_weekdays(cls, days: List[str]) -> List[str]:
        normalised = [day.strip().lower()[:3] for day in days]
        unknown = [day for day in normalised if day not in WEEKDAYS]
        if unknown:
            raise ValueError(f"unknown day(s) {unknown}, expected any of {list(WEEKDAYS)}")
        return normalised

    @model_validator(mode="after")
    def exactly_one_form(self) -> "MaintenanceWindowConfig":
        recurring = self.start is not None or self.duration_minutes is not None
        fixed = self.starts_at is not None or self.ends_at is not None

        if recurring and fixed:
            raise ValueError(
                f"maintenance window {self.name!r} mixes the recurring form "
                "(start/duration_minutes) with the fixed form (starts_at/ends_at); "
                "use one or the other"
            )
        if recurring:
            if self.start is None or self.duration_minutes is None:
                raise ValueError(
                    f"maintenance window {self.name!r} needs both start and "
                    "duration_minutes"
                )
            return self
        if fixed:
            if self.starts_at is None or self.ends_at is None:
                raise ValueError(
                    f"maintenance window {self.name!r} needs both starts_at and ends_at"
                )
            if self.ends_at <= self.starts_at:
                raise ValueError(
                    f"maintenance window {self.name!r} ends at or before it starts"
                )
            if self.days:
                raise ValueError(
                    f"maintenance window {self.name!r} sets days on a fixed window, "
                    "which already names the exact dates it covers"
                )
            return self
        raise ValueError(
            f"maintenance window {self.name!r} declares no schedule: give it either "
            "start + duration_minutes or starts_at + ends_at"
        )

    @property
    def is_recurring(self) -> bool:
        return self.start is not None

    def applies_to(self, monitor_name: str) -> bool:
        """Whether this window covers a given monitor.

        An empty `monitors` list covers everything: a window declared without
        naming any monitor is an estate-wide one, not a window that covers
        nothing at all.
        """
        return not self.monitors or monitor_name in self.monitors


# The HTTP methods a monitor may use. All of them are request methods with
# no body: there is nowhere in a monitor's config to put one, and a POST
# with an empty body is a different request from the one the operator
# thinks they configured. HEAD is the cheap one -- it asks the target for
# the same status code without transferring the page.
HTTP_METHODS = ("GET", "HEAD", "OPTIONS")


class MonitorConfig(BaseModel):
    name: str
    type: Literal["http", "tcp", "dns", "ssl"]
    target: str
    interval_seconds: int = Field(default=60, ge=5)
    timeout_seconds: int = Field(default=10, ge=1)
    expected_status: int = Field(default=200, ge=100, le=599)
    failure_threshold: int = Field(default=3, ge=1)
    recovery_threshold: int = Field(default=2, ge=1)
    degraded_latency_ms: Optional[float] = Field(default=None, gt=0)
    # HTTP only: a substring the response body must contain for the check to
    # pass, on top of the status code matching.
    expected_body_contains: Optional[str] = Field(default=None, min_length=1)
    # HTTP only: a substring the response body must NOT contain. Catches a
    # health endpoint that answers 200 with its normal body while a banner
    # or maintenance page is layered in front of it -- the opposite failure
    # mode from expected_body_contains, and checked independently of it.
    expected_body_not_contains: Optional[str] = Field(default=None, min_length=1)
    # HTTP only: the request method, and headers sent with every check.
    # Header values may carry `${VAR}` placeholders resolved from the
    # environment, so an API token never lands in the committed config.
    method: str = "GET"
    headers: Dict[str, str] = Field(default_factory=dict)
    channels: List[str] = Field(default_factory=list)
    # The availability this monitor is expected to hold over a rolling
    # window, as a percentage, and the length of that window in days. A
    # target changes nothing about how the monitor is checked or alerted --
    # it is a promise to measure against, reported by `alerts slo`, the
    # dashboard, and `/metrics`.
    slo_target: Optional[float] = Field(default=None, gt=MIN_TARGET, le=MAX_TARGET)
    slo_window_days: int = Field(default=DEFAULT_WINDOW_DAYS, ge=1, le=365)
    # The monitors this one needs in order to be healthy. Naming them
    # changes nothing about how this monitor is checked -- it changes who
    # gets paged when it fails: while an upstream is down, this monitor's
    # own alerts are held back, because the page that names the database is
    # the one worth reading.
    depends_on: List[str] = Field(default_factory=list)
    # Free-form labels for grouping: `prod`, `payments`, `eu-west-1`. They
    # carry no behaviour of their own -- nothing schedules, alerts, or
    # escalates differently because of a tag -- they exist so a report can be
    # narrowed to the part of the estate someone is responsible for.
    tags: List[str] = Field(default_factory=list)

    @field_validator("method")
    @classmethod
    def method_is_supported(cls, value: str) -> str:
        normalised = value.strip().upper()
        if normalised not in HTTP_METHODS:
            raise ValueError(
                f"unsupported HTTP method {value!r}, expected one of {list(HTTP_METHODS)}"
            )
        return normalised

    @field_validator("depends_on")
    @classmethod
    def dependencies_are_named_once(cls, names: List[str]) -> List[str]:
        """Strip and de-duplicate the dependency list, keeping its order.

        Not lowercased, unlike tags: a dependency names one specific
        monitor rather than a label several of them share, and folding
        would let `depends_on: [DB]` resolve to a monitor called `db` that
        every other lookup in this project treats as a different row. A
        name that matches nothing is refused below instead.
        """
        for raw in names:
            if not raw.strip():
                raise ValueError("depends_on entries must not be blank")
        return list(normalise_dependencies(names))

    @model_validator(mode="after")
    def a_monitor_does_not_depend_on_itself(self) -> "MonitorConfig":
        """Reject a monitor naming itself as a dependency.

        It reads as a harmless no-op and is not one: the monitor would
        suppress its own alerts for as long as it was down, which is
        exactly the outage nobody would then hear about. Caught here rather
        than by the cycle check below so the error names the mistake
        instead of describing a one-element loop.
        """
        if self.name in self.depends_on:
            raise ValueError(
                f"monitor {self.name!r} depends on itself, which would suppress "
                "its own alerts for the whole time it was down"
            )
        return self

    @field_validator("tags")
    @classmethod
    def tags_are_normalised_and_unique(cls, tags: List[str]) -> List[str]:
        """Normalise, validate, and de-duplicate a monitor's tags.

        De-duplication preserves declaration order rather than sorting: the
        order is how the operator grouped them in their head, and the status
        table shows them in it.
        """
        for raw in tags:
            tag = normalise_tag(raw)
            if not tag:
                raise ValueError("tags must not be blank")
            bad = forbidden_characters(tag)
            if bad:
                raise ValueError(
                    f"tag {raw!r} contains {bad!r}; tags may not contain commas or whitespace"
                )
        return normalise_all(tags)

    @field_validator("headers")
    @classmethod
    def header_names_are_not_blank(cls, headers: Dict[str, str]) -> Dict[str, str]:
        blank = [name for name in headers if not name.strip()]
        if blank:
            raise ValueError("header names must not be blank")
        return headers

    @model_validator(mode="after")
    def http_only_fields_are_on_http_monitors(self) -> "MonitorConfig":
        """Reject HTTP request options on a check that issues no HTTP request.

        Silently ignoring them is the failure mode worth avoiding: an
        operator who puts an `Authorization` header on a TCP monitor believes
        the endpoint is being checked as an authenticated client, when only
        its port is being opened.
        """
        if self.type == "http":
            return self
        wrong: List[str] = []
        if self.method != "GET":
            wrong.append("method")
        if self.headers:
            wrong.append("headers")
        if wrong:
            raise ValueError(
                f"monitor {self.name!r} sets {', '.join(wrong)} but is a "
                f"{self.type!r} monitor, which issues no HTTP request"
            )
        return self

    @model_validator(mode="after")
    def head_requests_have_no_body_to_assert_on(self) -> "MonitorConfig":
        """Reject a body assertion on a method that never returns a body.

        A HEAD response carries headers only, so the assertion could never
        match and every check would fail -- reporting an endpoint as down
        because of how it is being asked, not how it is behaving.
        """
        if self.method == "HEAD" and (
            self.expected_body_contains is not None or self.expected_body_not_contains is not None
        ):
            raise ValueError(
                f"monitor {self.name!r} asserts on the response body but uses "
                "HEAD, which returns no body"
            )
        return self

    @model_validator(mode="after")
    def body_assertion_applies_to_the_monitor_type(self) -> "MonitorConfig":
        """Reject a body assertion on a check that never reads a body.

        Only the HTTP checker fetches a response body; a TCP connect, a DNS
        lookup, and a certificate fetch have nothing to match against. Silently
        ignoring the field would leave an operator believing a health endpoint
        is being content-checked when only its port is.
        """
        if self.expected_body_contains is not None and self.type != "http":
            raise ValueError(
                f"monitor {self.name!r} sets expected_body_contains but is a "
                f"{self.type!r} monitor, which never reads a response body"
            )
        if self.expected_body_not_contains is not None and self.type != "http":
            raise ValueError(
                f"monitor {self.name!r} sets expected_body_not_contains but is a "
                f"{self.type!r} monitor, which never reads a response body"
            )
        return self

    @model_validator(mode="after")
    def slo_window_needs_a_target_to_measure(self) -> "MonitorConfig":
        """Reject an SLO window on a monitor that has no objective.

        The window is only ever read as the span an error budget is computed
        over, so setting one without a target does nothing at all. Left
        alone it reads as a configured SLO, which is the failure mode this
        project keeps refusing: a promise that looks measured and isn't.
        """
        if self.slo_target is None and "slo_window_days" in self.model_fields_set:
            raise ValueError(
                f"monitor {self.name!r} sets slo_window_days but no slo_target, "
                "so there is no objective for the window to be measured against"
            )
        return self

    @model_validator(mode="after")
    def slo_window_can_afford_one_failed_check(self) -> "MonitorConfig":
        """Reject a target the window is too short to ever satisfy in practice.

        An error budget is spent in whole checks. A window holds roughly
        `window_days * 86400 / interval_seconds` of them, and a target
        affords `1 - target/100` of that many failures -- so a tight target
        over a short window at a slow interval can afford less than a single
        failed check. Every monitor gets a failed check eventually (a
        redeploy, a DNS blip, a timeout on a busy afternoon), and when that
        one arrives the objective is breached for the rest of the window
        with no way to earn it back.

        That is not an objective anyone chose; it is an arithmetic accident,
        and it is invisible until the first failure. Refusing it at load
        time names the three knobs that fix it, which is what an operator
        would otherwise have to derive.

        An exact 100% target is exempt. It affords nothing at any window or
        interval, but that is the whole point of writing it down: someone
        typing 100 is deliberately saying every failure counts, which is a
        choice rather than the accident this check exists to catch.
        """
        if self.slo_target is None or self.slo_target >= MAX_TARGET:
            return self
        checks_in_window = (
            self.slo_window_days * SECONDS_PER_DAY
        ) / self.interval_seconds
        affordable = checks_in_window * (MAX_TARGET - self.slo_target) / MAX_TARGET
        if affordable < 1:
            raise ValueError(
                f"monitor {self.name!r} sets slo_target={self.slo_target} over "
                f"{self.slo_window_days} day(s) at a {self.interval_seconds}s "
                f"interval, which affords {affordable:.2f} failed checks -- a "
                "single failure would breach it for the whole window. Lengthen "
                "slo_window_days, lower slo_target, or shorten interval_seconds"
            )
        return self

    @model_validator(mode="after")
    def latency_budget_is_reachable(self) -> "MonitorConfig":
        """Reject a degraded threshold the timeout can never let a check reach.

        A check that runs past `timeout_seconds` is recorded as a failure, not
        a slow success, so a `degraded_latency_ms` at or above the timeout
        would never mark anything degraded -- it would silently do nothing,
        which is worse than being told about it at load time.
        """
        if (
            self.degraded_latency_ms is not None
            and self.degraded_latency_ms >= self.timeout_seconds * 1000
        ):
            raise ValueError(
                f"monitor {self.name!r} sets degraded_latency_ms="
                f"{self.degraded_latency_ms} at or above its timeout_seconds="
                f"{self.timeout_seconds} ({self.timeout_seconds * 1000}ms), so no "
                "successful check could ever be slow enough to trigger it"
            )
        return self

    @field_validator("name")
    @classmethod
    def name_is_slug_friendly(cls, value: str) -> str:
        if not value or "/" in value or " " in value:
            raise ValueError(
                f"monitor name {value!r} must be non-empty and contain no spaces or slashes"
            )
        return value


class SlackChannelConfig(BaseModel):
    name: str
    type: Literal["slack"]
    webhook_url_env: str
    timeout_seconds: float = Field(default=10.0, gt=0)

    @property
    def webhook_url(self) -> str:
        return _require_env(self.webhook_url_env, context=f"channel {self.name!r}")


class WebhookChannelConfig(BaseModel):
    """A generic webhook target: a URL, and headers sent with every POST.

    Both the URL and the header values may carry `${VAR}` placeholders
    resolved from the environment, which is what keeps a receiver's token out
    of the committed config. Plenty of webhook endpoints put the secret in
    the URL itself rather than in a header, so restricting expansion to
    headers would have left half of them uncoverable.

    Expansion happens when the notifier is built, at startup, so a missing
    variable is a startup failure rather than a page that doesn't arrive.
    """

    name: str
    type: Literal["webhook"]
    url: str
    headers: Dict[str, str] = Field(default_factory=dict)
    timeout_seconds: float = Field(default=10.0, gt=0)

    @property
    def resolved_url(self) -> str:
        return expand_env(self.url)

    @property
    def resolved_headers(self) -> Dict[str, str]:
        return expand_env_mapping(self.headers)

    def env_placeholder_problems(self) -> List[str]:
        """Variable names referenced here that aren't set in the environment."""
        unresolved = list(missing_env_vars(self.url))
        for value in self.headers.values():
            unresolved.extend(name for name in missing_env_vars(value) if name not in unresolved)
        return unresolved


class EmailChannelConfig(BaseModel):
    name: str
    type: Literal["email"]
    smtp_host: str
    smtp_port: int = 587
    use_tls: bool = True
    from_addr: str
    to_addrs: List[str]
    username_env: Optional[str] = None
    password_env: Optional[str] = None
    timeout_seconds: float = Field(default=10.0, gt=0)

    @field_validator("to_addrs")
    @classmethod
    def to_addrs_names_at_least_one_recipient(cls, addrs: List[str]) -> List[str]:
        """Reject an email channel with no one to send to.

        An empty list passes the type check and builds a notifier that
        sends an email with a blank `To` header -- a failure the SMTP
        server reports back as a notification-send error, at the moment an
        alert is firing, instead of here at config load where it belongs.
        """
        if not addrs:
            raise ValueError("to_addrs must name at least one recipient")
        for raw in addrs:
            if not raw.strip():
                raise ValueError("to_addrs entries must not be blank")
        return addrs

    @property
    def username(self) -> Optional[str]:
        return os.environ.get(self.username_env) if self.username_env else None

    @property
    def password(self) -> Optional[str]:
        return os.environ.get(self.password_env) if self.password_env else None


ChannelConfig = Union[SlackChannelConfig, WebhookChannelConfig, EmailChannelConfig]


class AppConfig(BaseModel):
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)
    monitors: List[MonitorConfig] = Field(default_factory=list)
    channels: List[ChannelConfig] = Field(default_factory=list)
    maintenance: List[MaintenanceWindowConfig] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def apply_scheduler_default_interval(cls, data: Any) -> Any:
        """Backfill a monitor's interval from `scheduler.default_interval_seconds`.

        Runs on the raw dict before per-monitor validation, so it only fills
        in monitors that omit `interval_seconds` entirely -- one explicitly
        set to the same value as the default is left alone, not that it
        would behave differently either way. Without this, the documented
        `scheduler.default_interval_seconds` config.example.yaml knob had no
        effect: MonitorConfig's own field default of 60 always won.
        """
        if not isinstance(data, dict):
            return data
        scheduler_cfg = data.get("scheduler")
        default_interval = (
            scheduler_cfg.get("default_interval_seconds")
            if isinstance(scheduler_cfg, dict)
            else None
        )
        if default_interval is None:
            return data
        for monitor in data.get("monitors") or []:
            if isinstance(monitor, dict) and "interval_seconds" not in monitor:
                monitor["interval_seconds"] = default_interval
        return data

    @field_validator("monitors")
    @classmethod
    def unique_monitor_names(cls, monitors: List[MonitorConfig]) -> List[MonitorConfig]:
        names = [m.name for m in monitors]
        duplicates = {n for n in names if names.count(n) > 1}
        if duplicates:
            raise ValueError(f"duplicate monitor names in config: {sorted(duplicates)}")
        return monitors

    @field_validator("channels")
    @classmethod
    def unique_channel_names(cls, channels: List[ChannelConfig]) -> List[ChannelConfig]:
        names = [c.name for c in channels]
        duplicates = {n for n in names if names.count(n) > 1}
        if duplicates:
            raise ValueError(f"duplicate channel names in config: {sorted(duplicates)}")
        return channels

    def channel_names(self) -> set:
        return {c.name for c in self.channels}

    def tags(self) -> List[str]:
        """Every tag any monitor declares, sorted, without repeats.

        Sorted here rather than in declaration order: across the whole config
        there is no one order to preserve, and this is what a `--tag` typo
        gets reported against.
        """
        found = {tag for monitor in self.monitors for tag in monitor.tags}
        return sorted(found)

    def monitors_tagged(self, tag: str) -> List[MonitorConfig]:
        """Every monitor carrying a tag, in config order.

        The tag is normalised on the way in, so a `--tag Prod` typed at a
        terminal matches a monitor that declared `prod`.
        """
        wanted = normalise_tag(tag)
        return [monitor for monitor in self.monitors if wanted in monitor.tags]

    def dependency_graph(self) -> DependencyGraph:
        """The declared dependencies between the configured monitors.

        Built from the config rather than the database so the commands that
        open no database at all -- `alerts check`, config validation -- ask
        the same graph the scheduler does.
        """
        return graph_of(self.monitors)

    def validate_monitor_dependency_refs(self) -> None:
        """Reject a dependency naming a monitor that doesn't exist, or a cycle.

        Both failures are silent and expensive if they are allowed through.
        An unknown name looks configured and suppresses nothing, so the one
        outage the operator set the dependency up for still pages every
        service behind it. A cycle is worse: every monitor in it is
        upstream of every other, so the first one to go down holds back the
        alerts for all of them, and the estate goes quiet at the moment it
        should be loudest.
        """
        graph = self.dependency_graph()
        known = {monitor.name for monitor in self.monitors}
        for name, unknown in sorted(graph.unknown_references(known).items()):
            raise ValueError(
                f"monitor {name!r} depends on unknown monitor(s): {unknown}"
            )
        cycle = graph.cycle()
        if cycle is not None:
            raise ValueError(
                "monitors depend on each other in a cycle: "
                f"{describe_cycle(cycle)} -- while any one of them is down it "
                "would hold back the alerts for all of them"
            )

    def validate_monitor_channel_refs(self) -> None:
        known = self.channel_names()
        for monitor in self.monitors:
            unknown = [c for c in monitor.channels if c not in known]
            if unknown:
                raise ValueError(
                    f"monitor {monitor.name!r} references unknown channel(s): {unknown}"
                )

    def validate_monitor_header_env_refs(self) -> None:
        """Reject a header placeholder naming a variable that isn't set.

        Header values are stored as written and expanded per check, so an
        unresolvable placeholder would otherwise surface as the target
        rejecting the request -- an outage that looks like the service's
        fault. Checking at load time turns a typo in a variable name into a
        startup error naming it.
        """
        for monitor in self.monitors:
            unresolved: List[str] = []
            for value in monitor.headers.values():
                unresolved.extend(
                    name for name in missing_env_vars(value) if name not in unresolved
                )
            if unresolved:
                raise ValueError(
                    f"monitor {monitor.name!r} has header(s) referencing unset "
                    f"environment variable(s): {unresolved}"
                )

    def validate_channel_env_refs(self) -> None:
        """Reject a channel placeholder naming a variable that isn't set.

        The same reasoning as the monitor headers above, one step worse in
        consequence: a webhook whose token can't be resolved fails at the
        moment there is something to say, and a notification that never
        arrives is indistinguishable from nothing having gone wrong.
        """
        for channel in self.channels:
            if not isinstance(channel, WebhookChannelConfig):
                # Slack and email reference their secrets by variable name
                # through `*_env` fields, which resolve on access and raise
                # their own error naming the variable.
                continue
            unresolved = channel.env_placeholder_problems()
            if unresolved:
                raise ValueError(
                    f"channel {channel.name!r} references unset environment "
                    f"variable(s): {unresolved}"
                )

    def validate_maintenance_monitor_refs(self) -> None:
        """Reject a maintenance window naming a monitor that doesn't exist.

        A typo here fails silently and expensively: the window looks
        configured, suppresses nothing, and the first anyone hears of it is
        being paged during the change it was meant to cover.
        """
        known = {m.name for m in self.monitors}
        for window in self.maintenance:
            unknown = [name for name in window.monitors if name not in known]
            if unknown:
                raise ValueError(
                    f"maintenance window {window.name!r} references unknown "
                    f"monitor(s): {unknown}"
                )


class ConfigError(Exception):
    """Raised for missing files, invalid YAML, or failed validation."""


def _require_env(var_name: str, *, context: str) -> str:
    value = os.environ.get(var_name)
    if not value:
        raise ConfigError(f"{context} requires environment variable {var_name!r} to be set")
    return value


def load_config(path: Union[str, Path]) -> AppConfig:
    config_path = Path(path)
    if not config_path.is_file():
        raise ConfigError(f"config file not found: {config_path}")

    try:
        raw = yaml.safe_load(config_path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {config_path}: {exc}") from exc

    try:
        config = AppConfig.model_validate(raw)
        config.validate_monitor_channel_refs()
        config.validate_monitor_header_env_refs()
        config.validate_channel_env_refs()
        config.validate_maintenance_monitor_refs()
        config.validate_monitor_dependency_refs()
    except Exception as exc:  # pydantic.ValidationError or ValueError
        raise ConfigError(f"invalid config in {config_path}: {exc}") from exc

    return config
