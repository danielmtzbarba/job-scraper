"""One-time, verified import from the local SQLite store to Cloud SQL."""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# Set the local CA bundle before the Cloud SQL connector imports aiohttp.
load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)

from job_scraper.storage.postgres_jobs import PostgresJobRepository, _Connection


PROJECT_ROOT = Path(__file__).resolve().parents[2]
TABLE_KEYS = {
    "jobs": ("source", "deduplication_key"),
    "job_processing": ("source", "deduplication_key"),
    "fit_assessment_provenance": ("source", "deduplication_key"),
    "source_aliases": ("source", "deduplication_key"),
    "possible_duplicates": ("source", "deduplication_key", "possible_source", "possible_key"),
    "search_runs": ("id",),
    "evaluation_runs": ("id",),
    "job_classifications": ("source", "deduplication_key"),
}
_IDENTIFIER = re.compile(r"^[a-z_]+$")


@dataclass(frozen=True)
class TableSnapshot:
    columns: tuple[str, ...]
    rows: list[tuple[Any, ...]]


def _identifier(name: str) -> str:
    if not _IDENTIFIER.fullmatch(name):
        raise ValueError(f"Unexpected database identifier: {name!r}")
    return '"' + name + '"'


def _target_columns(connection: _Connection, table: str) -> tuple[str, ...]:
    rows = connection.execute(
        """SELECT column_name FROM information_schema.columns
           WHERE table_schema = 'public' AND table_name = ?
           ORDER BY ordinal_position""",
        (table,),
    ).fetchall()
    columns = tuple(row["column_name"] for row in rows)
    if not columns:
        raise RuntimeError(f"Cloud SQL table {table} is missing; apply the schema first")
    return columns


def _source_snapshot(path: Path, columns_by_table: dict[str, tuple[str, ...]]) -> dict[str, TableSnapshot]:
    if not path.is_file():
        raise FileNotFoundError(path)
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN")
        snapshot: dict[str, TableSnapshot] = {}
        for table, columns in columns_by_table.items():
            source_columns = {
                row[1] for row in connection.execute(f"PRAGMA table_info({_identifier(table)})")
            }
            missing = set(columns) - source_columns
            if missing:
                raise RuntimeError(f"SQLite table {table} lacks Cloud SQL columns: {sorted(missing)}")
            select = ", ".join(map(_identifier, columns))
            order = ", ".join(map(_identifier, TABLE_KEYS[table]))
            rows = []
            for row in connection.execute(f"SELECT {select} FROM {_identifier(table)} ORDER BY {order}"):
                values = tuple(row[column] for column in columns)
                if table == "job_processing":
                    status_index = columns.index("processing_status")
                    if values[status_index] in {"ReadyToSync", "Synced"}:
                        values = values[:status_index] + ("Completed",) + values[status_index + 1:]
                rows.append(values)
            snapshot[table] = TableSnapshot(columns=columns, rows=rows)
        return snapshot
    finally:
        connection.close()


def _matches_target(connection: _Connection, snapshot: dict[str, TableSnapshot]) -> bool:
    for table, data in snapshot.items():
        columns = ", ".join(map(_identifier, data.columns))
        order = ", ".join(map(_identifier, TABLE_KEYS[table]))
        rows = connection.execute(
            f"SELECT {columns} FROM {_identifier(table)} ORDER BY {order}"
        ).fetchall()
        if len(rows) != len(data.rows):
            return False
        for target, source in zip(rows, data.rows, strict=True):
            if tuple(target[column] for column in data.columns) != source:
                return False
    return True


def import_sqlite(
    path: Path, repository: PostgresJobRepository, *, apply: bool
) -> tuple[dict[str, int], bool]:
    with repository._connect() as connection:
        columns_by_table = {
            table: _target_columns(connection, table) for table in TABLE_KEYS
        }
    snapshot = _source_snapshot(path, columns_by_table)
    counts = {table: len(data.rows) for table, data in snapshot.items()}
    if not apply:
        return counts, False

    # All inserts and exact row comparisons happen in one Cloud SQL transaction.
    with repository._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        target_counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {_identifier(table)}").fetchone()[0]
            for table in TABLE_KEYS
        }
        if any(target_counts.values()):
            if target_counts == counts and _matches_target(connection, snapshot):
                return counts, False
            raise RuntimeError("Cloud SQL already contains different or partial data; no rows were changed")

        for table, data in snapshot.items():
            if not data.rows:
                continue
            columns = ", ".join(map(_identifier, data.columns))
            placeholders = ", ".join("?" for _ in data.columns)
            connection.executemany(
                f"INSERT INTO {_identifier(table)} ({columns}) VALUES ({placeholders})",
                data.rows,
            )
        if not _matches_target(connection, snapshot):
            raise RuntimeError("Cloud SQL verification failed; transaction was rolled back")
    return counts, True


def main() -> None:
    parser = argparse.ArgumentParser(description="Import SQLite jobs into empty Cloud SQL tables")
    parser.add_argument("--sqlite-path", type=Path, default=PROJECT_ROOT / ".local/jobs.db")
    parser.add_argument("--apply", action="store_true", help="Import and verify in one transaction")
    args = parser.parse_args()
    repository = PostgresJobRepository(
        os.getenv("CLOUD_SQL_INSTANCE", ""),
        os.getenv("CLOUD_SQL_DATABASE", ""),
        os.getenv("CLOUD_SQL_IAM_USER", ""),
    )
    try:
        counts, changed = import_sqlite(args.sqlite_path, repository, apply=args.apply)
    finally:
        repository.close()
    action = "Imported and verified" if changed else ("Already matches" if args.apply else "Ready to import")
    print(f"{action}: " + ", ".join(f"{table}={count}" for table, count in counts.items()))


if __name__ == "__main__":
    main()
