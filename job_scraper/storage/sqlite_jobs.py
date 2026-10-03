"""SQLite staging queue and Airtable-shaped job mirror."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from pydantic import ValidationError

from job_scraper.models.jobs import (
    AirtableSyncWorkItem,
    JobMirrorRecord,
    JobProcessingPayload,
)
from job_scraper.sources.arbeitsagentur.html_parser import JobPosting

_SOURCE = "Agentur für Arbeit"
_FETCH_UPDATE_FIELDS = (
    "title", "company", "source_job_id", "job_url", "job_description", "location",
    "work_mode", "employment_type", "application_url", "posted_at",
)


class SQLiteJobRepository:
    """Persist in-flight source jobs separately from final Airtable-shaped rows."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            if "jobs" in tables:
                columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(jobs)")
                }
                if "id" in columns or "employer_job_url" in columns:
                    connection.execute("DROP INDEX IF EXISTS idx_jobs_source_job_id")
                    connection.execute("DROP INDEX IF EXISTS idx_jobs_detail_fetch")
                    connection.execute("ALTER TABLE jobs RENAME TO jobs_legacy")

            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    source TEXT NOT NULL,
                    source_job_id TEXT,
                    deduplication_key TEXT NOT NULL,
                    title TEXT,
                    company TEXT,
                    job_url TEXT,
                    job_description TEXT,
                    location TEXT,
                    work_mode TEXT,
                    employment_type TEXT,
                    seniority TEXT,
                    role_matches TEXT NOT NULL DEFAULT '[]',
                    application_url TEXT,
                    application_status TEXT NOT NULL DEFAULT 'Saved',
                    application_notes TEXT,
                    skill_stack_fit REAL,
                    semantic_experience_fit REAL,
                    overall_fit REAL,
                    fit_category TEXT,
                    fit_explanation TEXT,
                    fit_status TEXT NOT NULL DEFAULT 'Pending',
                    search_run_id TEXT,
                    posted_at TEXT,
                    PRIMARY KEY (source, deduplication_key)
                );
                CREATE TABLE IF NOT EXISTS job_processing (
                    source TEXT NOT NULL,
                    deduplication_key TEXT NOT NULL,
                    source_job_id TEXT,
                    payload_json TEXT NOT NULL,
                    processing_status TEXT NOT NULL DEFAULT 'Pending',
                    fetch_attempts INTEGER NOT NULL DEFAULT 0,
                    fetch_error TEXT,
                    fetch_next_retry_at TEXT,
                    airtable_next_retry_at TEXT,
                    airtable_record_id TEXT,
                    airtable_sync_status TEXT NOT NULL DEFAULT 'Pending',
                    airtable_sync_attempts INTEGER NOT NULL DEFAULT 0,
                    airtable_sync_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (source, deduplication_key)
                );
                """
            )

            # CREATE TABLE IF NOT EXISTS does not update an existing database.
            # Add columns introduced after the original staging schema before
            # creating indexes or querying them during worker startup.
            processing_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(job_processing)")
            }
            additive_columns = {
                "fetch_next_retry_at": "TEXT",
                "airtable_next_retry_at": "TEXT",
                "airtable_record_id": "TEXT",
                "airtable_sync_status": "TEXT NOT NULL DEFAULT 'Pending'",
                "airtable_sync_attempts": "INTEGER NOT NULL DEFAULT 0",
                "airtable_sync_error": "TEXT",
            }
            for column_name, column_definition in additive_columns.items():
                if column_name not in processing_columns:
                    connection.execute(
                        f"ALTER TABLE job_processing ADD COLUMN {column_name} {column_definition}"
                    )

            connection.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_job_processing_queue
                    ON job_processing (source, processing_status, fetch_next_retry_at);
                CREATE INDEX IF NOT EXISTS idx_job_airtable_sync_queue
                    ON job_processing (processing_status, airtable_sync_status, airtable_next_retry_at);
                CREATE INDEX IF NOT EXISTS idx_jobs_source_job_id
                    ON jobs (source, source_job_id);
                """
            )

            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            if "jobs_legacy" in tables:
                self._migrate_legacy_jobs(connection)
                connection.execute("DROP TABLE jobs_legacy")

            # A process may have stopped while it owned a fetch.
            connection.execute(
                "UPDATE job_processing SET processing_status = 'Pending' "
                "WHERE processing_status = 'Processing'"
            )
            connection.execute(
                "UPDATE job_processing SET airtable_sync_status = 'Pending' "
                "WHERE airtable_sync_status = 'Processing'"
            )

    def _migrate_legacy_jobs(self, connection: sqlite3.Connection) -> None:
        """Move existing mixed rows into staging, then publish completed rows."""
        rows = connection.execute("SELECT * FROM jobs_legacy ORDER BY id").fetchall()
        for row in rows:
            item = dict(row)
            source = item.get("source") or _SOURCE
            key = item.get("deduplication_key")
            if not key:
                continue
            values: dict[str, Any] = {
                "source": source,
                "source_job_id": item.get("source_job_id"),
                "deduplication_key": key,
                "title": item.get("title"),
                "company": item.get("company"),
                "job_url": item.get("job_url"),
                "employer_job_url": item.get("employer_job_url"),
                "application_url": item.get("employer_job_url") or item.get("application_url"),
                "job_description": item.get("job_description"),
                "location": item.get("location"),
                "work_mode": item.get("work_mode"),
                "employment_type": item.get("employment_type"),
                "employment_type_text": item.get("employment_type_text"),
                "posted_at": item.get("posted_at"),
                "posted_at_text": item.get("posted_at_text"),
                "application_status": _legacy_application_status(
                    item.get("application_status")
                ),
            }
            try:
                payload = JobProcessingPayload.model_validate(values)
            except ValidationError:
                values["posted_at"] = None
                payload = JobProcessingPayload.model_validate(values)
            status = item.get("detail_fetch_status", "Pending")
            processing_status = {
                "Completed": "ReadyToSync",
                "Failed": "Failed",
            }.get(status, "Pending")
            now = _utc_now()
            connection.execute(
                """INSERT OR IGNORE INTO job_processing (
                    source, deduplication_key, source_job_id, payload_json,
                    processing_status, fetch_attempts, fetch_error,
                    airtable_sync_status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'Pending', ?, ?)""",
                (
                    source, key, payload.source_job_id, payload.model_dump_json(),
                    processing_status, int(item.get("detail_fetch_attempts") or 0),
                    item.get("detail_fetch_error"),
                    item.get("created_at") or now, item.get("updated_at") or now,
                ),
            )
            if processing_status == "ReadyToSync":
                self._upsert_mirror(connection, payload.to_mirror_record())

    def upsert_search_results(self, postings: list[JobPosting]) -> dict[str, int]:
        inserted = updated = skipped = 0
        now = _utc_now()
        with self._connect() as connection:
            for posting in postings:
                key = posting.deduplication_key
                if not key:
                    skipped += 1
                    continue
                incoming = JobProcessingPayload.from_posting(posting)
                row = connection.execute(
                    "SELECT * FROM job_processing WHERE source = ? AND deduplication_key = ?",
                    (posting.source, key),
                ).fetchone()
                if row is None:
                    connection.execute(
                        """INSERT INTO job_processing (
                            source, deduplication_key, source_job_id, payload_json,
                            processing_status, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, 'Pending', ?, ?)""",
                        (posting.source, key, posting.source_job_id, incoming.model_dump_json(), now, now),
                    )
                    inserted += 1
                    continue

                previous = JobProcessingPayload.model_validate_json(row["payload_json"])
                merged_values = previous.model_dump()
                for name, value in incoming.model_dump().items():
                    if value is not None and value != []:
                        merged_values[name] = value
                merged = JobProcessingPayload.model_validate(merged_values)
                connection.execute(
                    "UPDATE job_processing SET source_job_id = ?, payload_json = ?, updated_at = ? "
                    "WHERE source = ? AND deduplication_key = ?",
                    (merged.source_job_id, merged.model_dump_json(), now, posting.source, key),
                )
                if row["processing_status"] in {"ReadyToSync", "Synced"}:
                    self._upsert_mirror(connection, merged.to_mirror_record())
                    connection.execute(
                        """UPDATE job_processing SET processing_status = 'ReadyToSync',
                            airtable_sync_status = 'Pending', airtable_sync_error = NULL,
                            updated_at = ? WHERE source = ? AND deduplication_key = ?""",
                        (now, posting.source, key),
                    )
                updated += 1
        return {"inserted": inserted, "updated": updated, "skipped": skipped}

    def claim_next_detail_job(self) -> str | None:
        """Claim the next BA staging row awaiting detail enrichment."""
        now = _utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT source, deduplication_key, source_job_id FROM job_processing
                WHERE source = ? AND source_job_id IS NOT NULL
                  AND processing_status = 'Pending'
                  AND (fetch_next_retry_at IS NULL OR fetch_next_retry_at <= ?)
                ORDER BY created_at, deduplication_key LIMIT 1""",
                (_SOURCE, now),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """UPDATE job_processing SET processing_status = 'Processing',
                    fetch_attempts = fetch_attempts + 1, fetch_error = NULL, updated_at = ?
                WHERE source = ? AND deduplication_key = ?""",
                (now, row["source"], row["deduplication_key"]),
            )
            return str(row["source_job_id"])

    def update_processing_status(
        self,
        source_job_id: str,
        status: str,
        error: str | None = None,
    ) -> None:
        if status not in {"Pending", "Processing", "Failed"}:
            raise ValueError(f"Invalid processing status: {status}")
        with self._connect() as connection:
            connection.execute(
                """UPDATE job_processing SET processing_status = ?, fetch_error = ?,
                    updated_at = ? WHERE source = ? AND source_job_id = ?""",
                (status, error[:1000] if error else None, _utc_now(), _SOURCE, source_job_id),
            )

    def get_fetch_attempts(self, source_job_id: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT fetch_attempts FROM job_processing "
                "WHERE source = ? AND source_job_id = ?",
                (_SOURCE, source_job_id),
            ).fetchone()
            return int(row["fetch_attempts"]) if row else 0

    def claim_next_airtable_sync(self) -> AirtableSyncWorkItem | None:
        """Claim the next completed job awaiting Airtable insertion or update."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT p.*, j.* FROM job_processing AS p JOIN jobs AS j
                    USING (source, deduplication_key)
                WHERE p.processing_status = 'ReadyToSync'
                  AND p.airtable_sync_status = 'Pending'
                  AND (p.airtable_next_retry_at IS NULL OR p.airtable_next_retry_at <= ?)
                ORDER BY p.created_at, p.deduplication_key LIMIT 1""",
                (_utc_now(),),
            ).fetchone()
            if row is None:
                return None
            item = dict(row)
            connection.execute(
                """UPDATE job_processing SET airtable_sync_status = 'Processing',
                    airtable_sync_attempts = airtable_sync_attempts + 1,
                    airtable_next_retry_at = NULL, updated_at = ?
                WHERE source = ? AND deduplication_key = ?""",
                (_utc_now(), item["source"], item["deduplication_key"]),
            )
            mirror_values = {
                key: value for key, value in item.items()
                if key not in {
                    "payload_json", "processing_status", "fetch_attempts", "fetch_error",
                    "fetch_next_retry_at", "airtable_next_retry_at", "airtable_record_id", "airtable_sync_status",
                    "airtable_sync_attempts", "airtable_sync_error", "created_at", "updated_at",
                }
            }
            return AirtableSyncWorkItem(
                source=item["source"],
                deduplication_key=item["deduplication_key"],
                airtable_record_id=item["airtable_record_id"],
                attempts=int(item["airtable_sync_attempts"]) + 1,
                record=self._row_to_record(mirror_values),
            )

    def mark_airtable_synced(
        self, source: str, deduplication_key: str, record_id: str
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """UPDATE job_processing SET airtable_record_id = ?,
                    airtable_sync_status = 'Synced', airtable_sync_error = NULL,
                    processing_status = 'Synced', updated_at = ?
                WHERE source = ? AND deduplication_key = ?""",
                (record_id, _utc_now(), source, deduplication_key),
            )

    def mark_airtable_sync_failed(
        self, source: str, deduplication_key: str, error: str
    ) -> None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT airtable_sync_attempts FROM job_processing "
                "WHERE source = ? AND deduplication_key = ?",
                (source, deduplication_key),
            ).fetchone()
            attempts = int(row["airtable_sync_attempts"]) if row else 1
            if attempts >= 8:
                status = "Failed"
                retry_at = None
            else:
                status = "Pending"
                retry_seconds = min(3600, 15 * (2 ** max(0, attempts - 1)))
                retry_at = (
                    datetime.now(timezone.utc) + timedelta(seconds=retry_seconds)
                ).isoformat(timespec="seconds")
            connection.execute(
                """UPDATE job_processing SET airtable_sync_status = ?,
                    airtable_sync_error = ?, airtable_next_retry_at = ?, updated_at = ?
                WHERE source = ? AND deduplication_key = ?""",
                (status, error[:1000], retry_at, _utc_now(), source, deduplication_key),
            )

    def enrich_from_detail(
        self, source_job_id: str, detail: JobPosting
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM job_processing WHERE source = ? AND source_job_id = ?",
                (detail.source, source_job_id),
            ).fetchone()
            if row is None:
                return None

            staged = JobProcessingPayload.model_validate_json(row["payload_json"])
            detail_payload = JobProcessingPayload.from_posting(detail)
            values = staged.model_dump()
            for name, value in detail_payload.model_dump().items():
                if value is not None and value != []:
                    values[name] = value
            merged = JobProcessingPayload.model_validate(values)
            mirror = self._upsert_mirror(connection, merged.to_mirror_record())
            now = _utc_now()
            connection.execute(
                """UPDATE job_processing SET payload_json = ?,
                    processing_status = 'ReadyToSync', fetch_error = NULL,
                    airtable_sync_status = 'Pending', airtable_sync_error = NULL,
                    updated_at = ? WHERE source = ? AND deduplication_key = ?""",
                (merged.model_dump_json(), now, detail.source, staged.deduplication_key),
            )
            return mirror.model_dump(mode="json")

    def _upsert_mirror(
        self,
        connection: sqlite3.Connection,
        record: JobMirrorRecord,
    ) -> JobMirrorRecord:
        values = record.model_dump(mode="json")
        values["role_matches"] = json.dumps(values["role_matches"], ensure_ascii=False)
        update_clause = ", ".join(
            f"{field} = COALESCE(excluded.{field}, jobs.{field})"
            for field in _FETCH_UPDATE_FIELDS
        )
        connection.execute(
            f"""INSERT INTO jobs (
                source, source_job_id, deduplication_key, title, company, job_url,
                job_description, location, work_mode, employment_type, seniority,
                role_matches, application_url, application_status, application_notes,
                skill_stack_fit, semantic_experience_fit, overall_fit, fit_category,
                fit_explanation, fit_status, search_run_id, posted_at
            ) VALUES (
                :source, :source_job_id, :deduplication_key, :title, :company, :job_url,
                :job_description, :location, :work_mode, :employment_type, :seniority,
                :role_matches, :application_url, :application_status, :application_notes,
                :skill_stack_fit, :semantic_experience_fit, :overall_fit, :fit_category,
                :fit_explanation, :fit_status, :search_run_id, :posted_at
            ) ON CONFLICT (source, deduplication_key) DO UPDATE SET {update_clause}""",
            values,
        )
        row = connection.execute(
            "SELECT * FROM jobs WHERE source = ? AND deduplication_key = ?",
            (record.source, record.deduplication_key),
        ).fetchone()
        return self._row_to_record(dict(row))

    def list_jobs(
        self, *, limit: int, offset: int, source: str | None = None
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            if source:
                rows = connection.execute(
                    "SELECT * FROM jobs WHERE source = ? ORDER BY posted_at DESC LIMIT ? OFFSET ?",
                    (source, limit, offset),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM jobs ORDER BY posted_at DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall()
            return [self._row_to_record(dict(row)).model_dump(mode="json") for row in rows]

    def get_job(self, source_job_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE source = ? AND source_job_id = ?",
                (_SOURCE, source_job_id),
            ).fetchone()
            return self._row_to_record(dict(row)).model_dump(mode="json") if row else None

    @staticmethod
    def _row_to_record(row: dict[str, Any]) -> JobMirrorRecord:
        # Some databases may still have workflow columns on `jobs`/joined rows
        # from the earlier mixed schema. Only pass Airtable mirror fields into
        # the strict Pydantic model.
        model_fields = JobMirrorRecord.model_fields
        allowed = set(model_fields)
        allowed.update(
            field.alias for field in model_fields.values() if field.alias is not None
        )
        values = {key: value for key, value in row.items() if key in allowed}
        if "role_matches" in values:
            values["role_matches"] = json.loads(values["role_matches"] or "[]")
        return JobMirrorRecord.model_validate(values)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _legacy_application_status(value: Any) -> str:
    statuses = {
        "saved": "Saved",
        "new": "Saved",
        "applied": "Applied",
        "interview": "Interview",
        "interviewing": "Interview",
        "offer": "Offer",
        "rejected": "Rejected",
        "withdrawn": "Withdrawn",
        "ignored": "Ignored",
    }
    return statuses.get(str(value or "Saved").strip().lower(), "Saved")
