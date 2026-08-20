"""The shipped `config.example.yaml` is documentation that can rot.

It is the first thing anyone copies, and nothing else in the suite loads it,
so a renamed field or a tightened validator can leave the example invalid
while every other test passes. These load it the way a new install does.
"""
from pathlib import Path

import pytest
import yaml

from alerts.config import HTTP_METHODS, load_config
from alerts.envsubst import placeholders

EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.yaml"
ENV_EXAMPLE = Path(__file__).resolve().parent.parent / ".env.example"


def _env_example_names() -> set:
    names = set()
    for line in ENV_EXAMPLE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            names.add(line.split("=", 1)[0])
    return names


@pytest.fixture()
def example_env(monkeypatch):
    """Set every variable .env.example declares, as a real install would."""
    for name in _env_example_names():
        monkeypatch.setenv(name, "placeholder-value")


def test_the_example_config_loads(example_env):
    config = load_config(EXAMPLE)

    assert config.monitors, "the example is meant to show monitors"
    assert config.channels


def test_every_monitor_channel_reference_resolves(example_env):
    config = load_config(EXAMPLE)
    known = config.channel_names()

    for monitor in config.monitors:
        assert set(monitor.channels) <= known, monitor.name


def test_every_maintenance_window_names_a_real_monitor(example_env):
    config = load_config(EXAMPLE)
    known = {monitor.name for monitor in config.monitors}

    for window in config.maintenance:
        assert set(window.monitors) <= known, window.name


def test_every_method_in_the_example_is_a_supported_one(example_env):
    config = load_config(EXAMPLE)

    for monitor in config.monitors:
        assert monitor.method in HTTP_METHODS


def test_every_env_placeholder_is_declared_in_the_env_example():
    """Otherwise the two files copied together still don't start."""
    raw = yaml.safe_load(EXAMPLE.read_text())
    declared = _env_example_names()

    referenced = set()
    for monitor in raw.get("monitors") or []:
        for value in (monitor.get("headers") or {}).values():
            referenced.update(placeholders(value))
    for channel in raw.get("channels") or []:
        referenced.update(placeholders(channel.get("url", "")))
        for value in (channel.get("headers") or {}).values():
            referenced.update(placeholders(value))

    assert referenced, "the example is meant to demonstrate ${VAR} secrets"
    assert referenced <= declared, sorted(referenced - declared)


def test_every_env_field_is_declared_in_the_env_example():
    """The *_env fields name variables too, and copying both files must work."""
    raw = yaml.safe_load(EXAMPLE.read_text())
    declared = _env_example_names()

    referenced = {
        value
        for channel in raw.get("channels") or []
        for key, value in channel.items()
        if key.endswith("_env")
    }

    assert referenced <= declared, sorted(referenced - declared)


def test_a_missing_variable_makes_the_example_fail_to_load(monkeypatch):
    """Proof the example really depends on the environment it documents."""
    for name in _env_example_names():
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(Exception):
        load_config(EXAMPLE)
