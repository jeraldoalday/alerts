"""Loading and validation for the YAML application config.

Monitors, notification channels, and scheduler defaults are declared in a
single YAML file. Secrets (SMTP passwords, webhook URLs) are never written
into the file directly -- each channel references an environment variable
name, and the value is resolved at load time.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, List, Literal, Optional, Union

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


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
    channels: List[str] = Field(default_factory=list)

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
    except Exception as exc:  # pydantic.ValidationError or ValueError
        raise ConfigError(f"invalid config in {config_path}: {exc}") from exc

    return config
