"""Cloud SQL PostgreSQL implementation of the existing job repository contract."""

from __future__ import annotations

import re
import socket
import time
from contextlib import contextmanager
from typing import Any, Iterator

from aiohttp import ClientConnectorError
from google.cloud.sql.connector import Connector
from pg8000.exceptions import InterfaceError

from job_scraper.storage.sqlite_jobs import SQLiteJobRepository, _stale_processing_cutoff, _utc_now


_REQUIRED_TABLES = frozenset({
    "jobs", "job_processing", "fit_assessment_provenance", "source_aliases",
    "possible_duplicates", "search_runs", "evaluation_runs", "job_classifications",
})

_CONNECT_ATTEMPTS = 3


def _is_transient_connect_error(exc: Exception) -> bool:
    """Return whether opening a Cloud SQL connection can reasonably be retried."""
    if isinstance(exc, (socket.gaierror, TimeoutError, ConnectionError, ClientConnectorError)):
        return True
    # pg8000 reports a dropped socket during its TLS/PostgreSQL handshake as an
    # InterfaceError("network error"). Other interface errors may be permanent.
    return isinstance(exc, InterfaceError) and "network error" in str(exc).lower()


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

    def execute(
        self, sql: str, parameters: tuple[Any, ...] | dict[str, Any] = ()
    ) -> _Cursor:
        if sql.strip().upper() == "BEGIN IMMEDIATE":
            # The SQLite code serializes claim/read/write sequences. Keep that
            # invariant across app processes without holding a session lock.
            sql = "SELECT pg_advisory_xact_lock(7112026)"
        elif isinstance(parameters, dict):
            # Shared repository SQL may use SQLite's :name bindings. pg8000's
            # default format style requires positional %s parameters.
            bound_values: list[Any] = []

            def bind_named(match: re.Match[str]) -> str:
                bound_values.append(parameters[match.group(1)])
                return "%s"

            sql = re.sub(r"(?<!:):([A-Za-z_]\w*)", bind_named, sql)
            parameters = tuple(bound_values)
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
        raw = None
        for attempt in range(_CONNECT_ATTEMPTS):
            try:
                raw = self._get_connector().connect(
                    self.instance_connection_name,
                    "pg8000",
                    user=self.user,
                    db=self.database,
                    enable_iam_auth=True,
                )
                break
            except Exception as exc:
                if attempt + 1 >= _CONNECT_ATTEMPTS or not _is_transient_connect_error(exc):
                    raise
                # The repository API is synchronous; keep this short, bounded
                # backoff local to connection setup so workers can recover from
                # brief DNS or network interruptions without failing an item.
                time.sleep(0.5 * (2**attempt))
        if raw is None:
            raise RuntimeError("Cloud SQL connection attempts ended without a connection")
        try:
            yield _Connection(raw)
            raw.commit()
        except BaseException:
            raw.rollback()
            raise
        finally:
            raw.close()

    def initialize(self) -> None:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname = 'public'"
            ).fetchall()
            missing = _REQUIRED_TABLES - {row["tablename"] for row in rows}
            if missing:
                raise RuntimeError(
                    "Cloud SQL schema is incomplete; provision these tables before startup: "
                    + ", ".join(sorted(missing))
                )
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
