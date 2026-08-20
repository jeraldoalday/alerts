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

from alerts.envsubst import missing as missing_env_vars


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
    expected_status: int = 200
    failure_threshold: int = Field(default=3, ge=1)
    recovery_threshold: int = Field(default=2, ge=1)
    degraded_latency_ms: Optional[float] = Field(default=None, gt=0)
    # HTTP only: a substring the response body must contain for the check to
    # pass, on top of the status code matching.
    expected_body_contains: Optional[str] = Field(default=None, min_length=1)
    # HTTP only: the request method, and headers sent with every check.
    # Header values may carry `${VAR}` placeholders resolved from the
    # environment, so an API token never lands in the committed config.
    method: str = "GET"
    headers: Dict[str, str] = Field(default_factory=dict)
    channels: List[str] = Field(default_factory=list)

    @field_validator("method")
    @classmethod
    def method_is_supported(cls, value: str) -> str:
        normalised = value.strip().upper()
        if normalised not in HTTP_METHODS:
            raise ValueError(
                f"unsupported HTTP method {value!r}, expected one of {list(HTTP_METHODS)}"
            )
        return normalised

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
        if self.method == "HEAD" and self.expected_body_contains is not None:
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

    @property
    def webhook_url(self) -> str:
        return _require_env(self.webhook_url_env, context=f"channel {self.name!r}")


class WebhookChannelConfig(BaseModel):
    name: str
    type: Literal["webhook"]
    url: str
    headers: dict = Field(default_factory=dict)


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
        config.validate_maintenance_monitor_refs()
    except Exception as exc:  # pydantic.ValidationError or ValueError
        raise ConfigError(f"invalid config in {config_path}: {exc}") from exc

    return config
