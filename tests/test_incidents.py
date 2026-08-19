from alerts.incidents import sync_incident
from alerts.models import Incident, IncidentState, Monitor, MonitorState, MonitorType


def _monitor(session, **overrides) -> Monitor:
    defaults = dict(
        name="site",
        type=MonitorType.HTTP,
        target="https://example.com",
        state=MonitorState.UP,
    )
    defaults.update(overrides)
    monitor = Monitor(**defaults)
    session.add(monitor)
    session.flush()
    return monitor


def test_sync_incident_opens_one_on_transition_to_down(db):
    with db.session() as session:
        monitor = _monitor(session, state=MonitorState.DOWN)
        sync_incident(session, monitor, previous_state=MonitorState.UP, cause="timeout")

    with db.session() as session:
        incident = session.query(Incident).one()
        assert incident.monitor_id == monitor.id
        assert incident.state == IncidentState.OPEN
        assert incident.cause == "timeout"
        assert incident.resolved_at is None


def test_sync_incident_resolves_open_incident_on_transition_to_up(db):
    with db.session() as session:
        monitor = _monitor(session, state=MonitorState.DOWN)
        sync_incident(session, monitor, previous_state=MonitorState.UP, cause="timeout")

    with db.session() as session:
        monitor = session.query(Monitor).filter_by(name="site").one()
        monitor.state = MonitorState.UP
        sync_incident(session, monitor, previous_state=MonitorState.DOWN)

    with db.session() as session:
        incident = session.query(Incident).one()
        assert incident.state == IncidentState.RESOLVED
        assert incident.resolved_at is not None


def test_sync_incident_opens_one_on_first_ever_check_failing(db):
    with db.session() as session:
        monitor = _monitor(session, state=MonitorState.DOWN)
        sync_incident(session, monitor, previous_state=MonitorState.UNKNOWN, cause="dns failure")

    with db.session() as session:
        incident = session.query(Incident).one()
        assert incident.state == IncidentState.OPEN
        assert incident.cause == "dns failure"


def test_sync_incident_only_resolves_the_affected_monitors_incident(db):
    with db.session() as session:
        monitor_a = _monitor(session, name="site-a", state=MonitorState.DOWN)
        monitor_b = _monitor(session, name="site-b", state=MonitorState.DOWN)
        sync_incident(session, monitor_a, previous_state=MonitorState.UP, cause="a down")
        sync_incident(session, monitor_b, previous_state=MonitorState.UP, cause="b down")
        monitor_a_id, monitor_b_id = monitor_a.id, monitor_b.id

    with db.session() as session:
        monitor_a = session.query(Monitor).filter_by(name="site-a").one()
        monitor_a.state = MonitorState.UP
        sync_incident(session, monitor_a, previous_state=MonitorState.DOWN)

    with db.session() as session:
        incident_a = session.query(Incident).filter_by(monitor_id=monitor_a_id).one()
        incident_b = session.query(Incident).filter_by(monitor_id=monitor_b_id).one()
        assert incident_a.state == IncidentState.RESOLVED
        assert incident_b.state == IncidentState.OPEN


def test_sync_incident_is_noop_when_state_unchanged(db):
    with db.session() as session:
        monitor = _monitor(session, state=MonitorState.UP)
        sync_incident(session, monitor, previous_state=MonitorState.UP)

    with db.session() as session:
        assert session.query(Incident).count() == 0


def test_repeated_down_up_cycles_create_separate_incidents(db):
    with db.session() as session:
        monitor = _monitor(session, state=MonitorState.DOWN)
        sync_incident(session, monitor, previous_state=MonitorState.UP, cause="first outage")

    with db.session() as session:
        monitor = session.query(Monitor).filter_by(name="site").one()
        monitor.state = MonitorState.UP
        sync_incident(session, monitor, previous_state=MonitorState.DOWN)

    with db.session() as session:
        monitor = session.query(Monitor).filter_by(name="site").one()
        monitor.state = MonitorState.DOWN
        sync_incident(session, monitor, previous_state=MonitorState.UP, cause="second outage")

    with db.session() as session:
        incidents = session.query(Incident).order_by(Incident.id).all()
        assert len(incidents) == 2
        assert incidents[0].state == IncidentState.RESOLVED
        assert incidents[0].cause == "first outage"
        assert incidents[1].state == IncidentState.OPEN
        assert incidents[1].cause == "second outage"


def test_recovering_from_down_into_degraded_resolves_the_incident(db):
    """A slow-but-serving recovery still ends the incident.

    Without this, the monitor would leave DOWN for DEGRADED with the incident
    still open, and the eventual DEGRADED -> UP transition -- which doesn't
    come from DOWN -- would never resolve it.
    """
    with db.session() as session:
        monitor = _monitor(session, state=MonitorState.DOWN)
        session.add(Incident(monitor_id=monitor.id, cause="boom"))
        session.flush()

        monitor.state = MonitorState.DEGRADED
        sync_incident(session, monitor, previous_state=MonitorState.DOWN)
        session.flush()

        incident = session.query(Incident).one()
        assert incident.state == IncidentState.RESOLVED
        assert incident.resolved_at is not None


def test_sliding_between_up_and_degraded_opens_no_incident(db):
    with db.session() as session:
        monitor = _monitor(session, state=MonitorState.DEGRADED)

        sync_incident(session, monitor, previous_state=MonitorState.UP)
        session.flush()

        assert session.query(Incident).count() == 0
