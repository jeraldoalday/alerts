import pytest

from alerts.db import Database


@pytest.fixture()
def db() -> Database:
    database = Database("sqlite:///:memory:")
    database.create_all()
    return database


@pytest.fixture()
def db_config(tmp_path):
    """A config file plus the database it points at, for CLI-level tests."""
    config_file = tmp_path / "config.yaml"
    db_path = tmp_path / "test.db"
    config_file.write_text(f"database:\n  url: sqlite:///{db_path}\n")
    database = Database(f"sqlite:///{db_path}")
    database.create_all()
    return str(config_file), database
