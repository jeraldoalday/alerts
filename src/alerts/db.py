"""SQLAlchemy engine/session wiring, decoupled from any specific config source."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from alerts.models.base import Base


def make_engine(database_url: str):
    connect_args = {}
    if database_url.startswith("sqlite"):
        # SQLite needs this to allow use across the scheduler's worker threads.
        connect_args["check_same_thread"] = False
    return create_engine(database_url, connect_args=connect_args)


class Database:
    """Owns an engine + session factory for one configured database URL."""

    def __init__(self, database_url: str):
        self.engine = make_engine(database_url)
        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)

    def create_all(self) -> None:
        Base.metadata.create_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self.session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
