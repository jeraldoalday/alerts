"""SLO targets surviving the trip from config into the monitors table.

The dashboard and `/metrics` never read the config -- they are built from
the database alone -- so a target that doesn't make it through
`sync_monitors` is a promise nothing can report on. These cover the way in,
the way out, and the two edits that are easy to get wrong: raising a target,
and removing one.
"""
from alerts.config import MonitorConfig
from alerts.models import Monitor, MonitorType
from alerts.scheduler import sync_monitors
from alerts.slo import DEFAULT_WINDOW_DAYS


def _config(name: str = "api", **overrides) -> MonitorConfig:
    fields = dict(name=name, type="http", target="https://example.com")
    fields.update(overrides)
    return MonitorConfig(**fields)


def _get(session, name: str = "api") -> Monitor:
    return session.query(Monitor).filter_by(name=name).one()


def test_a_new_monitor_carries_its_target_in(db):
    with db.session() as session:
        sync_monitors(session, [_config(slo_target=99.9, slo_window_days=7)])

    with db.session() as session:
        monitor = _get(session)
        assert monitor.slo_target == 99.9
        assert monitor.slo_window_days == 7
        assert monitor.has_slo is True


def test_a_monitor_without_a_target_stores_none(db):
    with db.session() as session:
        sync_monitors(session, [_config()])

    with db.session() as session:
        monitor = _get(session)
        assert monitor.slo_target is None
        assert monitor.has_slo is False


def test_a_monitor_without_a_target_still_gets_a_default_window(db):
    """The window is NOT NULL, so it holds a value nothing reads yet.

    That's deliberate: adding a target later shouldn't also require picking
    a window in the same edit.
    """
    with db.session() as session:
        sync_monitors(session, [_config()])

    with db.session() as session:
        assert _get(session).slo_window_days == DEFAULT_WINDOW_DAYS


def test_raising_a_target_on_an_existing_monitor_takes_effect(db):
    with db.session() as session:
        sync_monitors(session, [_config(slo_target=99.0)])
    with db.session() as session:
        sync_monitors(session, [_config(slo_target=99.9)])

    with db.session() as session:
        assert _get(session).slo_target == 99.9


def test_removing_a_target_from_the_config_removes_it_from_the_estate(db):
    """A promise nobody can find the declaration for is worse than none."""
    with db.session() as session:
        sync_monitors(session, [_config(slo_target=99.9)])
    with db.session() as session:
        sync_monitors(session, [_config()])

    with db.session() as session:
        monitor = _get(session)
        assert monitor.slo_target is None
        assert monitor.has_slo is False


def test_shortening_the_window_takes_effect(db):
    with db.session() as session:
        sync_monitors(session, [_config(slo_target=99.9, slo_window_days=30)])
    with db.session() as session:
        sync_monitors(session, [_config(slo_target=99.9, slo_window_days=7)])

    with db.session() as session:
        assert _get(session).slo_window_days == 7


def test_a_target_survives_a_monitor_being_paused_and_brought_back(db):
    """Pausing resets runtime state, not the declaration it was checked against."""
    with db.session() as session:
        sync_monitors(session, [_config(slo_target=99.5)])
    with db.session() as session:
        sync_monitors(session, [])
    with db.session() as session:
        assert _get(session).is_paused is True

    with db.session() as session:
        sync_monitors(session, [_config(slo_target=99.5)])

    with db.session() as session:
        monitor = _get(session)
        assert monitor.is_paused is False
        assert monitor.slo_target == 99.5


def test_targets_are_kept_per_monitor(db):
    with db.session() as session:
        sync_monitors(
            session,
            [
                _config("web", slo_target=99.9),
                _config("batch"),
                _config("api", slo_target=95.0, slo_window_days=7),
            ],
        )

    with db.session() as session:
        assert _get(session, "web").slo_target == 99.9
        assert _get(session, "batch").slo_target is None
        assert _get(session, "api").slo_window_days == 7


def test_has_slo_is_false_for_a_monitor_built_in_memory():
    """A row that hasn't been through a flush answers truthfully too."""
    monitor = Monitor(name="api", type=MonitorType.HTTP, target="https://example.com")

    assert monitor.has_slo is False
