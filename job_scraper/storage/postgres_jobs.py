"""Cloud SQL PostgreSQL implementation of the existing job repository contract."""

from __future__ import annotations

from contextlib import contextmanager
from importlib.resources import files
from typing import Any, Iterator

from google.cloud.sql.connector import Connector

from job_scraper.storage.sqlite_jobs import SQLiteJobRepository, _stale_processing_cutoff, _utc_now


class _Row(dict[str, Any]):
    """Expose PostgreSQL results like sqlite3.Row to the shared repository logic."""

    def __getitem__(self, key: str | int) -> Any:
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


class _Cursor:
    def __init__(self, cursor: Any) -> None:
        self._cursor = cursor

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    def fetchone(self) -> _Row | None:
        row = self._cursor.fetchone()
        return self._convert(row) if row is not None else None

    def fetchall(self) -> list[_Row]:
        return [self._convert(row) for row in self._cursor.fetchall()]

    def __iter__(self) -> Iterator[_Row]:
        for row in self._cursor:
            yield self._convert(row)

    def _convert(self, row: tuple[Any, ...]) -> _Row:
        return _Row(zip((column[0] for column in self._cursor.description), row))


class _Connection:
    def __init__(self, raw: Any) -> None:
        self.raw = raw

    def execute(self, sql: str, parameters: tuple[Any, ...] = ()) -> _Cursor:
        if sql.strip().upper() == "BEGIN IMMEDIATE":
            # The SQLite code serializes claim/read/write sequences. Keep that
            # invariant across app processes without holding a session lock.
            sql = "SELECT pg_advisory_xact_lock(7112026)"
        else:
            sql = sql.replace("?", "%s").replace(" LIKE ", " ILIKE ")
        cursor = self.raw.cursor()
        cursor.execute(sql, parameters)
        return _Cursor(cursor)

    def executescript(self, sql: str) -> None:
        for statement in sql.split(";"):
            if statement.strip():
                self.execute(statement)

    def executemany(self, sql: str, rows: list[tuple[Any, ...]]) -> _Cursor:
        cursor = self.raw.cursor()
        cursor.executemany(sql.replace("?", "%s"), rows)
        return _Cursor(cursor)


class PostgresJobRepository(SQLiteJobRepository):
    """Run the shared job operations against Cloud SQL with IAM login."""

    def __init__(self, instance_connection_name: str, database: str, user: str) -> None:
        if not instance_connection_name or not database or not user:
            raise ValueError("Cloud SQL instance, database, and IAM user are required")
        self.instance_connection_name = instance_connection_name
        self.database = database
        self.user = user
        self._connector: Connector | None = None

    def _get_connector(self) -> Connector:
        if self._connector is None:
            self._connector = Connector(refresh_strategy="LAZY")
        return self._connector

    @contextmanager
    def _connect(self) -> Iterator[_Connection]:
        raw = self._get_connector().connect(
            self.instance_connection_name,
            "pg8000",
            user=self.user,
            db=self.database,
            enable_iam_auth=True,
        )
        try:
            yield _Connection(raw)
            raw.commit()
        except BaseException:
            raw.rollback()
            raise
        finally:
            raw.close()

    def initialize(self) -> None:
        schema = files("job_scraper.storage").joinpath("postgres_schema.sql").read_text()
        with self._connect() as connection:
            connection.executescript(schema)
            stale_before = _stale_processing_cutoff()
            connection.execute(
                "UPDATE job_processing SET processing_status = 'Pending' "
                "WHERE processing_status = 'Processing' AND updated_at <= ?",
                (stale_before,),
            )
            connection.execute(
                """UPDATE evaluation_runs SET status = 'Failed',
                     error_type = 'Interrupted', finished_at = ?
                   WHERE status = 'Running' AND started_at <= ?""",
                (_utc_now(), stale_before),
            )
            connection.execute(
                """UPDATE job_classifications SET
                     status = CASE WHEN attempts >= 3 THEN 'Failed' ELSE 'Pending' END,
                     claimed_at = NULL, next_retry_at = NULL, updated_at = ?
                   WHERE status = 'Running' AND claimed_at <= ?""",
                (_utc_now(), stale_before),
            )

    def close(self) -> None:
        if self._connector is not None:
            self._connector.close()
            self._connector = None
