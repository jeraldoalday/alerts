"""`depends_on` in the config: what it accepts, and what it refuses.

The refusals are the point. A dependency that names nothing looks
configured and suppresses nothing, and a cycle makes every monitor in it
upstream of every other -- so the first one down holds back the alerts for
all of them, and the estate goes quiet at the moment it should be loudest.
Both are invisible until an outage, which is why they are load-time errors.
"""
import pytest
from pydantic import ValidationError

from alerts.config import AppConfig, ConfigError, MonitorConfig, load_config


def _monitor(name: str = "api", **overrides) -> MonitorConfig:
    fields = {"name": name, "type": "http", "target": "https://example.com"}
    fields.update(overrides)
    return MonitorConfig(**fields)


def _config(*monitors: MonitorConfig) -> AppConfig:
    return AppConfig(monitors=list(monitors))


def _write(tmp_path, body: str) -> str:
    path = tmp_path / "config.yaml"
    path.write_text(body)
    return str(path)


def test_a_monitor_depends_on_nothing_by_default():
    assert _monitor().depends_on == []


def test_dependencies_keep_declaration_order():
    assert _monitor(depends_on=["db", "cache"]).depends_on == ["db", "cache"]


def test_a_repeated_dependency_is_kept_once():
    assert _monitor(depends_on=["db", "db"]).depends_on == ["db"]


def test_surrounding_whitespace_is_stripped():
    assert _monitor(depends_on=[" db "]).depends_on == ["db"]


@pytest.mark.parametrize("raw", ["", "   ", "\t"])
def test_a_blank_dependency_is_refused(raw):
    with pytest.raises(ValidationError, match="must not be blank"):
        _monitor(depends_on=[raw])


def test_a_dependency_is_not_case_folded():
    """Unlike a tag, it names one specific monitor rather than a shared label."""
    assert _monitor(depends_on=["DB"]).depends_on == ["DB"]


def test_a_monitor_depending_on_itself_is_refused():
    with pytest.raises(ValidationError, match="depends on itself"):
        _monitor("api", depends_on=["api"])


def test_a_dependency_naming_no_monitor_is_refused():
    config = _config(_monitor("web", depends_on=["typo"]), _monitor("api"))

    with pytest.raises(ValueError, match=r"depends on unknown monitor\(s\): \['typo'\]"):
        config.validate_monitor_dependency_refs()


def test_a_dependency_that_only_differs_in_case_is_unknown():
    # The other half of not folding: `DB` is not `db`, and this is the loud
    # version of that -- better than a dependency that silently watches a
    # monitor nobody has.
    config = _config(_monitor("web", depends_on=["DB"]), _monitor("db"))

    with pytest.raises(ValueError, match="unknown monitor"):
        config.validate_monitor_dependency_refs()


def test_a_valid_dependency_passes_validation():
    config = _config(_monitor("web", depends_on=["api"]), _monitor("api"))

    config.validate_monitor_dependency_refs()


def test_a_cycle_between_two_monitors_is_refused():
    config = _config(
        _monitor("web", depends_on=["api"]), _monitor("api", depends_on=["web"])
    )

    with pytest.raises(ValueError, match="depend on each other in a cycle"):
        config.validate_monitor_dependency_refs()


def test_a_longer_cycle_is_refused_and_names_the_chain():
    config = _config(
        _monitor("web", depends_on=["api"]),
        _monitor("api", depends_on=["db"]),
        _monitor("db", depends_on=["web"]),
    )

    with pytest.raises(ValueError, match="api -> db -> web -> api"):
        config.validate_monitor_dependency_refs()


def test_a_diamond_is_allowed():
    # Two services needing the same database is the normal shape, not a loop.
    config = _config(
        _monitor("web", depends_on=["api", "db"]),
        _monitor("api", depends_on=["db"]),
        _monitor("db"),
    )

    config.validate_monitor_dependency_refs()


def test_the_graph_covers_every_configured_monitor():
    graph = _config(
        _monitor("web", depends_on=["api"]), _monitor("api", depends_on=["db"]), _monitor("db")
    ).dependency_graph()

    assert graph.upstream("web") == ["api", "db"]
    assert graph.downstream("db") == ["api", "web"]


def test_a_config_declaring_no_dependencies_has_an_empty_graph():
    assert not _config(_monitor("web"), _monitor("api")).dependency_graph()


def test_loading_a_config_with_dependencies(tmp_path):
    path = _write(
        tmp_path,
        """
monitors:
  - name: web
    type: http
    target: https://example.com
    depends_on: [api]
  - name: api
    type: http
    target: https://api.example.com
    depends_on: [db]
  - name: db
    type: tcp
    target: db.example.com:5432
""",
    )

    config = load_config(path)

    assert config.monitors[0].depends_on == ["api"]
    assert config.dependency_graph().upstream("web") == ["api", "db"]


def test_loading_a_config_whose_dependency_names_nothing_fails(tmp_path):
    path = _write(
        tmp_path,
        """
monitors:
  - name: web
    type: http
    target: https://example.com
    depends_on: [database]
""",
    )

    with pytest.raises(ConfigError, match="unknown monitor"):
        load_config(path)


def test_loading_a_config_containing_a_cycle_fails(tmp_path):
    path = _write(
        tmp_path,
        """
monitors:
  - name: web
    type: http
    target: https://example.com
    depends_on: [api]
  - name: api
    type: http
    target: https://api.example.com
    depends_on: [web]
""",
    )

    with pytest.raises(ConfigError, match="cycle"):
        load_config(path)
