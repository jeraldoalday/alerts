"""Dependencies surviving the trip from config into the monitors table.

The dashboard and `alerts deps` never read the config -- they are built
from the database alone -- so a dependency that doesn't make it through
`sync_monitors` is an edge that exists in the file and nowhere anybody
can see it. These cover the way in, the way out, and the two edits that
are easy to get wrong: adding an upstream, and removing one.
"""
from alerts.config import MonitorConfig
from alerts.dependencies import graph_of
from alerts.models import Monitor, MonitorState, MonitorType
from alerts.scheduler import sync_monitors


def _config(name: str = "api", **overrides) -> MonitorConfig:
    fields = dict(name=name, type="http", target="https://example.com")
    fields.update(overrides)
    return MonitorConfig(**fields)


def _get(session, name: str = "api") -> Monitor:
    return session.query(Monitor).filter_by(name=name).one()


def test_a_new_monitor_carries_its_dependencies_in(db):
    with db.session() as session:
        sync_monitors(session, [_config("web", depends_on=["api"]), _config("api")])

    with db.session() as session:
        assert _get(session, "web").depends_on == ["api"]


def test_a_monitor_declaring_none_stores_an_empty_list(db):
    with db.session() as session:
        sync_monitors(session, [_config()])

    with db.session() as session:
        assert _get(session).depends_on == []


def test_adding_an_upstream_to_an_existing_monitor_takes_effect(db):
    with db.session() as session:
        sync_monitors(session, [_config("web"), _config("api")])
    with db.session() as session:
        sync_monitors(session, [_config("web", depends_on=["api"]), _config("api")])

    with db.session() as session:
        assert _get(session, "web").depends_on == ["api"]


def test_removing_an_upstream_clears_it(db):
    """Otherwise the alerts stay suppressed by an edge nobody declares any more."""
    with db.session() as session:
        sync_monitors(session, [_config("web", depends_on=["api"]), _config("api")])
    with db.session() as session:
        sync_monitors(session, [_config("web"), _config("api")])

    with db.session() as session:
        assert _get(session, "web").depends_on == []


def test_declaration_order_survives_the_round_trip(db):
    with db.session() as session:
        sync_monitors(
            session,
            [_config("web", depends_on=["db", "cache"]), _config("db"), _config("cache")],
        )

    with db.session() as session:
        assert _get(session, "web").depends_on == ["db", "cache"]


def test_the_graph_can_be_rebuilt_from_the_stored_rows(db):
    """`alerts deps` and the dashboard build it this way, never from the config."""
    with db.session() as session:
        sync_monitors(
            session,
            [_config("web", depends_on=["api"]), _config("api", depends_on=["db"]), _config("db")],
        )

    with db.session() as session:
        graph = graph_of(session.query(Monitor).all())
        assert graph.upstream("web") == ["api", "db"]
        assert graph.downstream("db") == ["api", "web"]


def test_a_down_monitor_blocks_its_dependents(db):
    with db.session() as session:
        session.add(
            Monitor(
                name="db",
                type=MonitorType.TCP,
                target="db.example.com:5432",
                state=MonitorState.DOWN,
            )
        )

    with db.session() as session:
        assert _get(session, "db").blocks_dependents is True


def test_a_degraded_monitor_does_not_block_its_dependents(db):
    # Degraded is "still serving, slowly". A dependent failing while its
    # upstream is merely slow is its own outage, and the page is about it.
    with db.session() as session:
        session.add(
            Monitor(
                name="db",
                type=MonitorType.TCP,
                target="db.example.com:5432",
                state=MonitorState.DEGRADED,
            )
        )

    with db.session() as session:
        assert _get(session, "db").blocks_dependents is False


def test_a_paused_monitor_never_blocks_its_dependents(db):
    """Its state is frozen, so it would suppress a live service forever."""
    with db.session() as session:
        session.add(
            Monitor(
                name="db",
                type=MonitorType.TCP,
                target="db.example.com:5432",
                state=MonitorState.DOWN,
                enabled=False,
            )
        )

    with db.session() as session:
        assert _get(session, "db").blocks_dependents is False
