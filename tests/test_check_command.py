"""`alerts check` end to end, through `cli.main`.

The checkers are stubbed -- these cover the command, not the network -- but
everything else is real: the config is loaded from a file, the probe runs
through asyncio, and the table is the one an operator sees.
"""
import pytest

from alerts import cli
from alerts.checkers.base import CheckOutcome
from alerts.db import Database
from alerts.models import CheckResult, Incident, MonitorType


class StubChecker:
    def __init__(self, outcome: CheckOutcome):
        self._outcome = outcome
        self.closed = False

    async def check(self, request):
        return self._outcome

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture()
def stub_checkers(monkeypatch):
    """Replace the real checkers, returning the dict so a test can retune it."""
    checkers = {
        monitor_type: StubChecker(CheckOutcome(success=True, latency_ms=42.0, status_code=200))
        for monitor_type in MonitorType
    }
    monkeypatch.setattr("alerts.probe.default_checkers", lambda: checkers)
    return checkers


def _config(tmp_path, body: str) -> str:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(f"database:\n  url: sqlite:///{tmp_path / 'test.db'}\n{body}")
    return str(config_file)


ONE_MONITOR = """monitors:
  - name: api
    type: http
    target: https://api.example.com/healthz
"""

TWO_MONITORS = """monitors:
  - name: api
    type: http
    target: https://api.example.com/healthz
  - name: db
    type: tcp
    target: db.example.com:5432
"""


def test_parse_args_check_defaults():
    args = cli._parse_args(["check"])

    assert args.command == "check"
    assert args.config == "config.yaml"
    assert args.monitor is None
    assert args.exit_code is False


def test_check_prints_a_table_and_a_summary(tmp_path, capsys, stub_checkers):
    exit_code = cli.main(["check", "--config", _config(tmp_path, ONE_MONITOR)])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "MONITOR" in out and "TARGET" in out
    assert "https://api.example.com/healthz" in out
    assert "42ms" in out
    assert "1 monitor(s) checked, 1 ok, 0 failed" in out


def test_check_reports_a_failure_and_its_reason(tmp_path, capsys, stub_checkers):
    for monitor_type in MonitorType:
        stub_checkers[monitor_type] = StubChecker(
            CheckOutcome(success=False, error_message="expected status 200, got 502")
        )

    exit_code = cli.main(["check", "--config", _config(tmp_path, ONE_MONITOR)])

    out = capsys.readouterr().out
    assert "FAIL" in out
    assert "expected status 200, got 502" in out
    assert exit_code == 0, "a report exits 0 unless asked to gate"


def test_exit_code_flag_fails_on_a_failed_check(tmp_path, capsys, stub_checkers):
    stub_checkers[MonitorType.HTTP] = StubChecker(
        CheckOutcome(success=False, error_message="boom")
    )

    exit_code = cli.main(
        ["check", "--config", _config(tmp_path, ONE_MONITOR), "--exit-code"]
    )

    assert exit_code == 1


def test_exit_code_flag_passes_when_everything_answers(tmp_path, capsys, stub_checkers):
    exit_code = cli.main(
        ["check", "--config", _config(tmp_path, TWO_MONITORS), "--exit-code"]
    )

    assert exit_code == 0


def test_a_slow_check_does_not_fail_the_gate(tmp_path, capsys, stub_checkers):
    """It passed. The scheduler would call it degraded, not down."""
    stub_checkers[MonitorType.HTTP] = StubChecker(
        CheckOutcome(success=True, latency_ms=2000.0, status_code=200)
    )
    body = """monitors:
  - name: api
    type: http
    target: https://api.example.com/healthz
    timeout_seconds: 10
    degraded_latency_ms: 1500
"""

    exit_code = cli.main(["check", "--config", _config(tmp_path, body), "--exit-code"])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "slow" in out
    assert "1500ms budget" in out


def test_monitor_flag_checks_only_that_one(tmp_path, capsys, stub_checkers):
    cli.main(["check", "--config", _config(tmp_path, TWO_MONITORS), "--monitor", "db"])

    out = capsys.readouterr().out
    assert "db.example.com:5432" in out
    assert "api.example.com" not in out
    assert "1 monitor(s) checked" in out


def test_an_unknown_monitor_name_is_named_back(tmp_path, capsys, stub_checkers):
    cli.main(["check", "--config", _config(tmp_path, TWO_MONITORS), "--monitor", "nope"])

    assert "No monitor named 'nope'" in capsys.readouterr().out


def test_an_unknown_monitor_name_fails_the_gate(tmp_path, capsys, stub_checkers):
    """A typo passing silently is the one way --exit-code could fail unnoticed."""
    exit_code = cli.main(
        [
            "check",
            "--config",
            _config(tmp_path, TWO_MONITORS),
            "--monitor",
            "nope",
            "--exit-code",
        ]
    )

    assert exit_code == 1


def test_check_writes_nothing_to_the_database(tmp_path, capsys, stub_checkers):
    """The property that makes this safe to run against a live install."""
    config_path = _config(tmp_path, TWO_MONITORS)

    cli.main(["check", "--config", config_path])

    database = Database(f"sqlite:///{tmp_path / 'test.db'}")
    database.create_all()
    with database.session() as session:
        assert session.query(CheckResult).count() == 0
        assert session.query(Incident).count() == 0


def test_check_does_not_create_the_database_file_at_all(tmp_path, capsys, stub_checkers):
    """Not even an empty schema: nothing here opens a database."""
    config_path = _config(tmp_path, TWO_MONITORS)

    cli.main(["check", "--config", config_path])

    assert not (tmp_path / "test.db").exists()


def test_an_unset_header_variable_fails_the_config_before_any_check(
    tmp_path, capsys, monkeypatch, stub_checkers
):
    """`load_config` refuses it, so the failure names the variable up front."""
    monkeypatch.delenv("CHECK_CMD_TOKEN", raising=False)
    body = """monitors:
  - name: api
    type: http
    target: https://api.example.com/healthz
    headers:
      Authorization: "Bearer ${CHECK_CMD_TOKEN}"
"""

    with pytest.raises(Exception) as error:
        cli.main(["check", "--config", _config(tmp_path, body)])

    assert "CHECK_CMD_TOKEN" in str(error.value)


def test_a_config_with_no_monitors_says_so(tmp_path, capsys, stub_checkers):
    cli.main(["check", "--config", _config(tmp_path, "")])

    out = capsys.readouterr().out
    assert "No monitors to check." in out
    assert "0 monitor(s) checked" in out
