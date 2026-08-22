"""Tag declaration, normalisation, and lookup on the loaded config."""
import pytest
from pydantic import ValidationError

from alerts.config import AppConfig, ConfigError, MonitorConfig, load_config, normalise_tag


def _monitor(name: str = "api", **overrides) -> MonitorConfig:
    fields = {"name": name, "type": "http", "target": "https://example.com"}
    fields.update(overrides)
    return MonitorConfig(**fields)


def test_a_monitor_has_no_tags_by_default():
    assert _monitor().tags == []


def test_tags_are_kept_in_declaration_order():
    """The order is how the operator grouped them; the table shows it back."""
    assert _monitor(tags=["prod", "payments", "eu-west-1"]).tags == [
        "prod",
        "payments",
        "eu-west-1",
    ]


@pytest.mark.parametrize("raw", ["PROD", " prod ", "Prod", "\tprod\n"])
def test_a_tag_is_folded_to_one_spelling(raw):
    assert _monitor(tags=[raw]).tags == ["prod"]
    assert normalise_tag(raw) == "prod"


def test_a_repeated_tag_is_kept_once():
    assert _monitor(tags=["prod", "PROD", "prod"]).tags == ["prod"]


@pytest.mark.parametrize("raw", ["", "   ", "\t"])
def test_a_blank_tag_is_refused(raw):
    with pytest.raises(ValidationError, match="must not be blank"):
        _monitor(tags=[raw])


def test_a_tag_containing_a_comma_is_refused():
    """It would render as two tags nobody declared, and read back as two."""
    with pytest.raises(ValidationError, match="commas or whitespace"):
        _monitor(tags=["prod,staging"])


def test_a_tag_containing_a_space_is_refused():
    with pytest.raises(ValidationError, match="commas or whitespace"):
        _monitor(tags=["eu west"])


def test_the_config_lists_every_tag_in_use_sorted():
    config = AppConfig(
        monitors=[
            _monitor("a", tags=["prod", "payments"]),
            _monitor("b", tags=["prod", "staging"]),
            _monitor("c"),
        ]
    )

    assert config.tags() == ["payments", "prod", "staging"]


def test_the_config_has_no_tags_when_nothing_declares_one():
    assert AppConfig(monitors=[_monitor()]).tags() == []


def test_monitors_tagged_returns_them_in_config_order():
    config = AppConfig(
        monitors=[
            _monitor("z", tags=["prod"]),
            _monitor("a", tags=["staging"]),
            _monitor("m", tags=["prod"]),
        ]
    )

    assert [m.name for m in config.monitors_tagged("prod")] == ["z", "m"]


def test_monitors_tagged_normalises_what_it_is_asked_for():
    """`--tag Prod` typed at a terminal has to find a monitor tagged `prod`."""
    config = AppConfig(monitors=[_monitor("a", tags=["prod"])])

    assert [m.name for m in config.monitors_tagged(" PROD ")] == ["a"]


def test_an_unknown_tag_matches_nothing():
    config = AppConfig(monitors=[_monitor("a", tags=["prod"])])

    assert config.monitors_tagged("nope") == []


def test_tags_load_from_a_yaml_file(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        """
monitors:
  - name: api
    type: http
    target: https://example.com
    tags: [Prod, payments]
"""
    )

    config = load_config(config_file)

    assert config.monitors[0].tags == ["prod", "payments"]


def test_a_bad_tag_fails_the_load_with_the_file_named(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        """
monitors:
  - name: api
    type: http
    target: https://example.com
    tags: ["prod,staging"]
"""
    )

    with pytest.raises(ConfigError, match="commas or whitespace"):
        load_config(config_file)


def test_tags_are_allowed_on_every_monitor_type():
    """Unlike headers, a tag says nothing about how the check is made."""
    for monitor_type, target in (
        ("http", "https://example.com"),
        ("tcp", "example.com:443"),
        ("dns", "example.com"),
        ("ssl", "example.com:443"),
    ):
        assert _monitor(type=monitor_type, target=target, tags=["prod"]).tags == ["prod"]
