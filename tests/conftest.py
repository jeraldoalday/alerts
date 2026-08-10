import pytest

from alerts.db import Database


@pytest.fixture()
def db() -> Database:
    database = Database("sqlite:///:memory:")
    database.create_all()
    return database
