"""Fails if the migrations and the ORM models disagree about the schema.

`Database.create_all()` builds the schema straight from the models, while a
real install gets there through the migrations. Nothing keeps the two in
step: a column added to a model without a matching migration works
perfectly in the tests (which use create_all) and is missing in production
the moment someone upgrades an existing database.

Run against a database that has been migrated to head:

    ALERTS_DATABASE_URL=sqlite:///ci_test.db alembic upgrade head
    python scripts/check_migration_parity.py sqlite:///ci_test.db
"""
from __future__ import annotations

import sys
from typing import Dict, List, Set

import sqlalchemy as sa

from alerts.models import Base

# Alembic's own bookkeeping table has no model, by design.
IGNORED_TABLES = {"alembic_version"}


def _migrated_schema(database_url: str) -> Dict[str, Set[str]]:
    inspector = sa.inspect(sa.create_engine(database_url))
    return {
        table: {column["name"] for column in inspector.get_columns(table)}
        for table in inspector.get_table_names()
        if table not in IGNORED_TABLES
    }


def _model_schema() -> Dict[str, Set[str]]:
    return {table.name: set(table.columns.keys()) for table in Base.metadata.sorted_tables}


def differences(models: Dict[str, Set[str]], migrated: Dict[str, Set[str]]) -> List[str]:
    """Every way the two schemas disagree, as human-readable lines."""
    problems: List[str] = []
    for table in sorted(set(models) | set(migrated)):
        if table not in migrated:
            problems.append(f"{table}: in the models but no migration creates it")
            continue
        if table not in models:
            problems.append(f"{table}: created by a migration but no model declares it")
            continue
        missing = sorted(models[table] - migrated[table])
        extra = sorted(migrated[table] - models[table])
        if missing:
            problems.append(f"{table}: columns missing from the migrations: {missing}")
        if extra:
            problems.append(f"{table}: columns the models no longer declare: {extra}")
    return problems


def main(argv: List[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0]} DATABASE_URL", file=sys.stderr)
        return 2

    problems = differences(_model_schema(), _migrated_schema(argv[1]))
    if problems:
        print("migrations and models disagree:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    print("migrations and models agree on every table and column")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
