"""SQLite staging queue and Airtable-shaped job mirror."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

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
_STALE_PROCESSING_MINUTES = 10


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
                    airtable_pending_fields_json TEXT NOT NULL DEFAULT '{}',
                    airtable_sync_status TEXT NOT NULL DEFAULT 'Pending',
                    airtable_sync_attempts INTEGER NOT NULL DEFAULT 0,
                    airtable_sync_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (source, deduplication_key)
                );
                CREATE TABLE IF NOT EXISTS fit_assessment_provenance (
                    source TEXT NOT NULL,
                    deduplication_key TEXT NOT NULL,
                    profile_id TEXT NOT NULL,
                    profile_version INTEGER NOT NULL,
                    rubric_version TEXT,
                    prompt_version TEXT,
                    assessed_at TEXT NOT NULL,
                    PRIMARY KEY (source, deduplication_key)
                );
                CREATE TABLE IF NOT EXISTS source_aliases (
                    source TEXT NOT NULL,
                    deduplication_key TEXT NOT NULL,
                    canonical_source TEXT NOT NULL,
                    canonical_key TEXT NOT NULL,
                    PRIMARY KEY (source, deduplication_key)
                );
                CREATE TABLE IF NOT EXISTS possible_duplicates (
                    source TEXT NOT NULL,
                    deduplication_key TEXT NOT NULL,
                    possible_source TEXT NOT NULL,
                    possible_key TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (source, deduplication_key, possible_source, possible_key)
                );
                CREATE TABLE IF NOT EXISTS search_runs (
                    id TEXT PRIMARY KEY,
                    search_id TEXT NOT NULL,
                    slot_date TEXT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    found INTEGER NOT NULL DEFAULT 0,
                    inserted INTEGER NOT NULL DEFAULT 0,
                    duplicate INTEGER NOT NULL DEFAULT 0,
                    skipped INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    UNIQUE (search_id, slot_date)
                );
                """
            )

            duplicate_pk = [
                row[1] for row in connection.execute("PRAGMA table_info(possible_duplicates)")
                if row[5]
            ]
            if duplicate_pk == ["source", "deduplication_key"]:
                connection.executescript(
                    """ALTER TABLE possible_duplicates RENAME TO possible_duplicates_old;
                    CREATE TABLE possible_duplicates (
                        source TEXT NOT NULL, deduplication_key TEXT NOT NULL,
                        possible_source TEXT NOT NULL, possible_key TEXT NOT NULL,
                        reason TEXT NOT NULL, created_at TEXT NOT NULL,
                        PRIMARY KEY (source, deduplication_key, possible_source, possible_key)
                    );
                    INSERT INTO possible_duplicates SELECT * FROM possible_duplicates_old;
                    DROP TABLE possible_duplicates_old;"""
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
                "airtable_pending_fields_json": "TEXT NOT NULL DEFAULT '{}'",
                "airtable_sync_status": "TEXT NOT NULL DEFAULT 'Pending'",
                "airtable_sync_attempts": "INTEGER NOT NULL DEFAULT 0",
                "airtable_sync_error": "TEXT",
            }
            for column_name, column_definition in additive_columns.items():
                if column_name not in processing_columns:
                    connection.execute(
                        f"ALTER TABLE job_processing ADD COLUMN {column_name} {column_definition}"
                    )

            provenance_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(fit_assessment_provenance)")
            }
            for column_name in ("rubric_version", "prompt_version"):
                if column_name not in provenance_columns:
                    connection.execute(
                        f"ALTER TABLE fit_assessment_provenance ADD COLUMN {column_name} TEXT"
                    )

            connection.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_job_processing_queue
                    ON job_processing (source, processing_status, fetch_next_retry_at);
                CREATE INDEX IF NOT EXISTS idx_job_airtable_sync_queue
                    ON job_processing (processing_status, airtable_sync_status, airtable_next_retry_at);
                CREATE INDEX IF NOT EXISTS idx_jobs_source_job_id
                    ON jobs (source, source_job_id);
                CREATE INDEX IF NOT EXISTS idx_jobs_company_title
                    ON jobs (company, title);
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

            connection.execute(
                """INSERT OR IGNORE INTO source_aliases
                   (source, deduplication_key, canonical_source, canonical_key)
                   SELECT source, deduplication_key, source, deduplication_key FROM jobs"""
            )

            # Recover only claims that have exceeded the lease period. The API
            # and stdio MCP process may share this database, so a fresh claim
            # can belong to another live process.
            stale_before = _stale_processing_cutoff()
            connection.execute(
                "UPDATE job_processing SET processing_status = 'Pending' "
                "WHERE processing_status = 'Processing' AND updated_at <= ?",
                (stale_before,),
            )
            connection.execute(
                "UPDATE job_processing SET airtable_sync_status = 'Pending' "
                "WHERE airtable_sync_status = 'Processing' AND updated_at <= ?",
                (stale_before,),
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

    def stage_new_search_results(
        self, postings: list[JobPosting], *, search_run_id: str | None = None
    ) -> dict[str, int]:
        """Insert only unseen source postings, safely across concurrent searches."""
        inserted = duplicate = skipped = 0
        now = _utc_now()
        with self._connect() as connection:
            for posting in postings:
                key = posting.deduplication_key
                if not key or not posting.source_job_id:
                    skipped += 1
                    continue
                payload = JobProcessingPayload.from_posting(posting)
                payload.search_run_id = search_run_id
                cursor = connection.execute(
                    """INSERT INTO job_processing
                       (source, deduplication_key, source_job_id, payload_json,
                        processing_status, created_at, updated_at)
                       VALUES (?, ?, ?, ?, 'Pending', ?, ?)
                       ON CONFLICT(source, deduplication_key) DO NOTHING""",
                    (posting.source, key, posting.source_job_id, payload.model_dump_json(), now, now),
                )
                if cursor.rowcount:
                    inserted += 1
                else:
                    duplicate += 1
        return {"inserted": inserted, "duplicate": duplicate, "skipped": skipped}

    def start_search_run(self, run_id: str, search_id: str, slot_date: str | None) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """INSERT INTO search_runs (id, search_id, slot_date, started_at, status)
                   VALUES (?, ?, ?, ?, 'Running')
                   ON CONFLICT(search_id, slot_date) DO NOTHING""",
                (run_id, search_id, slot_date, _utc_now()),
            )
            return bool(cursor.rowcount)

    def finish_search_run(
        self, run_id: str, *, counts: dict[str, int] | None = None,
        error: str | None = None, partial: bool = False,
    ) -> None:
        counts = counts or {}
        with self._connect() as connection:
            connection.execute(
                """UPDATE search_runs SET finished_at = ?, status = ?, found = ?,
                   inserted = ?, duplicate = ?, skipped = ?, error = ? WHERE id = ?""",
                (
                    _utc_now(), "Failed" if error else "Partial" if partial else "Completed",
                    counts.get("found", 0), counts.get("inserted", 0),
                    counts.get("duplicate", 0), counts.get("skipped", 0),
                    error[:1000] if error else None, run_id,
                ),
            )

    def list_search_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM search_runs ORDER BY started_at DESC, rowid DESC LIMIT ?", (limit,)
            )]

    def claim_next_detail_job(self) -> str | None:
        """Claim the next BA staging row awaiting detail enrichment."""
        now = _utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """UPDATE job_processing SET processing_status = 'Pending', updated_at = ?
                WHERE processing_status = 'Processing' AND updated_at <= ?""",
                (now, _stale_processing_cutoff()),
            )
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
            connection.execute(
                """UPDATE job_processing SET airtable_sync_status = 'Pending', updated_at = ?
                WHERE airtable_sync_status = 'Processing' AND updated_at <= ?""",
                (_utc_now(), _stale_processing_cutoff()),
            )
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
                    "airtable_pending_fields_json", "airtable_sync_attempts", "airtable_sync_error", "created_at", "updated_at",
                }
            }
            return AirtableSyncWorkItem(
                source=item["source"],
                deduplication_key=item["deduplication_key"],
                airtable_record_id=item["airtable_record_id"],
                attempts=int(item["airtable_sync_attempts"]) + 1,
                additional_fields=json.loads(item.get("airtable_pending_fields_json") or "{}"),
                record=self._row_to_record(mirror_values),
            )

    def mark_airtable_synced(
        self,
        source: str,
        deduplication_key: str,
        record_id: str,
        synced_fields: dict[str, object] | None = None,
    ) -> None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT airtable_pending_fields_json FROM job_processing "
                "WHERE source = ? AND deduplication_key = ?",
                (source, deduplication_key),
            ).fetchone()
            pending_fields = (
                json.loads(row["airtable_pending_fields_json"] or "{}")
                if row
                else {}
            )
            for field_name, value in (synced_fields or {}).items():
                if pending_fields.get(field_name) == value:
                    pending_fields.pop(field_name)
            pending_json = json.dumps(pending_fields, ensure_ascii=False)
            sync_pending = bool(pending_fields)
            connection.execute(
                """UPDATE job_processing SET airtable_record_id = ?,
                    airtable_sync_status = ?, airtable_sync_error = NULL,
                    airtable_pending_fields_json = ?,
                    processing_status = ?, airtable_next_retry_at = NULL, updated_at = ?
                WHERE source = ? AND deduplication_key = ?""",
                (
                    record_id,
                    "Pending" if sync_pending else "Synced",
                    pending_json,
                    "ReadyToSync" if sync_pending else "Synced",
                    _utc_now(),
                    source,
                    deduplication_key,
                ),
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
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM job_processing WHERE source = ? AND source_job_id = ?",
                (detail.source, source_job_id),
            ).fetchone()
            if row is None:
                return None

            if row["processing_status"] not in {"Pending", "Processing"}:
                existing = connection.execute(
                    "SELECT * FROM jobs WHERE source = ? AND deduplication_key = ?",
                    (detail.source, row["deduplication_key"]),
                ).fetchone()
                if existing is not None:
                    return self._row_to_record(dict(existing)).model_dump(mode="json")
                return JobProcessingPayload.model_validate_json(
                    row["payload_json"]
                ).to_mirror_record().model_dump(mode="json")

            staged = JobProcessingPayload.model_validate_json(row["payload_json"])
            detail_payload = JobProcessingPayload.from_posting(detail)
            values = staged.model_dump()
            for name, value in detail_payload.model_dump().items():
                if value is not None and value != []:
                    values[name] = value
            merged = JobProcessingPayload.model_validate(values)
            candidate = merged.to_mirror_record()
            existing_job = connection.execute(
                "SELECT 1 FROM jobs WHERE source = ? AND deduplication_key = ?",
                (detail.source, staged.deduplication_key),
            ).fetchone()
            if existing_job is None and _excluded_employment(merged):
                connection.execute(
                    """UPDATE job_processing SET payload_json = ?,
                       processing_status = 'Filtered', updated_at = ?
                       WHERE source = ? AND deduplication_key = ?""",
                    (merged.model_dump_json(), _utc_now(), detail.source, staged.deduplication_key),
                )
                return candidate.model_dump(mode="json")
            if existing_job is None:
                exact, possible = self._cross_source_matches(connection, candidate)
                if exact is not None and not possible:
                    connection.execute(
                        """INSERT OR REPLACE INTO source_aliases
                           (source, deduplication_key, canonical_source, canonical_key)
                           VALUES (?, ?, ?, ?)""",
                        (detail.source, staged.deduplication_key, exact["source"], exact["deduplication_key"]),
                    )
                    connection.execute(
                        """UPDATE job_processing SET payload_json = ?,
                           processing_status = 'Merged', updated_at = ?
                           WHERE source = ? AND deduplication_key = ?""",
                        (merged.model_dump_json(), _utc_now(), detail.source, staged.deduplication_key),
                    )
                    return candidate.model_dump(mode="json")
                if possible:
                    for match in possible:
                        connection.execute(
                            """INSERT OR IGNORE INTO possible_duplicates
                               (source, deduplication_key, possible_source, possible_key,
                                reason, created_at) VALUES (?, ?, ?, ?, ?, ?)""",
                            (detail.source, staged.deduplication_key, match["source"],
                             match["deduplication_key"], match["reason"], _utc_now()),
                        )
                    connection.execute(
                        """UPDATE job_processing SET payload_json = ?,
                           processing_status = 'NeedsReview', updated_at = ?
                           WHERE source = ? AND deduplication_key = ?""",
                        (merged.model_dump_json(), _utc_now(), detail.source, staged.deduplication_key),
                    )
                    return candidate.model_dump(mode="json")
            mirror = self._upsert_mirror(connection, merged.to_mirror_record())
            connection.execute(
                """INSERT OR IGNORE INTO source_aliases
                   (source, deduplication_key, canonical_source, canonical_key)
                   VALUES (?, ?, ?, ?)""",
                (detail.source, staged.deduplication_key, detail.source, staged.deduplication_key),
            )
            now = _utc_now()
            connection.execute(
                """UPDATE job_processing SET payload_json = ?,
                    processing_status = 'ReadyToSync', fetch_error = NULL,
                    airtable_sync_status = 'Pending', airtable_sync_error = NULL,
                    updated_at = ? WHERE source = ? AND deduplication_key = ?""",
                (merged.model_dump_json(), now, detail.source, staged.deduplication_key),
            )
            return mirror.model_dump(mode="json")

    def get_processing_status(self, source: str, source_job_id: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT processing_status FROM job_processing WHERE source = ? AND source_job_id = ?",
                (source, source_job_id),
            ).fetchone()
            return str(row["processing_status"]) if row else None

    def _cross_source_matches(
        self, connection: sqlite3.Connection, candidate: JobMirrorRecord
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        """Auto-link only a shared direct URL with matching employer, title and JD."""
        rows = [dict(row) for row in connection.execute(
            "SELECT source, deduplication_key, title, company, application_url, job_description "
            "FROM jobs WHERE source <> ?", (candidate.source,)
        )]
        candidate_url = _direct_application_url(candidate.application_url)
        exact = [row for row in rows if candidate_url and
                 _direct_application_url(row["application_url"]) == candidate_url]
        possible: list[dict[str, Any]] = []
        if len(exact) == 1:
            same_company = _normalized_text(candidate.company) == _normalized_text(exact[0]["company"])
            title_similarity = SequenceMatcher(
                None, _normalized_text(candidate.title), _normalized_text(exact[0]["title"])
            ).ratio()
            description = _normalized_text(candidate.job_description)
            other_description = _normalized_text(exact[0]["job_description"])
            description_similarity = (
                SequenceMatcher(None, description, other_description).ratio()
                if min(len(description), len(other_description)) >= 80 else 0
            )
            if same_company and candidate.company and title_similarity >= 0.86 and description_similarity >= 0.9:
                return exact[0], []
        for row in exact:
            possible.append({**row, "reason": "Shared application URL; verify job descriptions"})
        company = _normalized_text(candidate.company)
        title = _normalized_text(candidate.title)
        if company and title:
            for row in rows:
                if row in exact:
                    continue
                if _normalized_text(row["company"]) != company:
                    continue
                other_title = _normalized_text(row["title"])
                if other_title and SequenceMatcher(None, title, other_title).ratio() >= 0.86:
                    possible.append({**row, "reason": "Similar employer and title"})
        return None, possible

    def list_possible_duplicates(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT d.source, d.deduplication_key, d.possible_source,
                   d.possible_key, d.reason, d.created_at, p.payload_json,
                   j.title AS possible_title, j.company AS possible_company,
                   j.job_url AS possible_job_url
                   FROM possible_duplicates d
                   JOIN job_processing p USING (source, deduplication_key)
                   JOIN jobs j ON j.source = d.possible_source
                              AND j.deduplication_key = d.possible_key
                   ORDER BY d.created_at"""
            ).fetchall()
            results = []
            for row in rows:
                item = dict(row)
                item["candidate"] = json.loads(item.pop("payload_json"))
                results.append(item)
            return results

    def resolve_possible_duplicate(
        self, source: str, deduplication_key: str, *, link_existing: bool,
        possible_source: str | None = None, possible_key: str | None = None,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT p.payload_json, d.possible_source, d.possible_key
                   FROM possible_duplicates d JOIN job_processing p
                   USING (source, deduplication_key)
                   WHERE d.source = ? AND d.deduplication_key = ?
                     AND p.processing_status = 'NeedsReview'""",
                (source, deduplication_key),
            ).fetchall()
            if not row:
                return None
            if link_existing:
                matches = [item for item in row if
                           (possible_source is None or item["possible_source"] == possible_source)
                           and (possible_key is None or item["possible_key"] == possible_key)]
                if len(matches) != 1:
                    raise ValueError("Select exactly one possible_source and possible_key.")
                selected = matches[0]
            else:
                selected = row[0]
            payload = JobProcessingPayload.model_validate_json(selected["payload_json"])
            if link_existing:
                canonical = connection.execute(
                    "SELECT * FROM jobs WHERE source = ? AND deduplication_key = ?",
                    (selected["possible_source"], selected["possible_key"]),
                ).fetchone()
                if canonical is None:
                    raise LookupError("The possible matching job no longer exists.")
                result = self._row_to_record(dict(canonical)).model_dump(mode="json")
                status = "Merged"
                canonical_source, canonical_key = selected["possible_source"], selected["possible_key"]
            else:
                result = self._upsert_mirror(connection, payload.to_mirror_record()).model_dump(mode="json")
                status = "ReadyToSync"
                canonical_source, canonical_key = source, deduplication_key
            connection.execute(
                """INSERT OR REPLACE INTO source_aliases
                   (source, deduplication_key, canonical_source, canonical_key)
                   VALUES (?, ?, ?, ?)""",
                (source, deduplication_key, canonical_source, canonical_key),
            )
            connection.execute(
                "UPDATE job_processing SET processing_status = ?, updated_at = ? "
                "WHERE source = ? AND deduplication_key = ?",
                (status, _utc_now(), source, deduplication_key),
            )
            connection.execute(
                "DELETE FROM possible_duplicates WHERE source = ? AND deduplication_key = ?",
                (source, deduplication_key),
            )
            return {"resolution": "linked" if link_existing else "separate", "job": result}

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
        self,
        *,
        limit: int,
        offset: int,
        source: str | None = None,
        query: str | None = None,
        fit_status: str | None = None,
    ) -> list[dict[str, Any]]:
        conditions: list[str] = []
        parameters: list[Any] = []
        if source:
            conditions.append("source = ?")
            parameters.append(source)
        if fit_status:
            conditions.append("fit_status = ?")
            parameters.append(fit_status)
        if query:
            conditions.append(
                "(title LIKE ? OR company LIKE ? OR location LIKE ? OR job_description LIKE ?)"
            )
            search_term = f"%{query}%"
            parameters.extend([search_term] * 4)
        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        parameters.extend([limit, offset])
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM jobs {where_clause} ORDER BY posted_at DESC LIMIT ? OFFSET ?",
                parameters,
            ).fetchall()
            return [self._row_to_record(dict(row)).model_dump(mode="json") for row in rows]

    def get_job(self, source_job_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE source = ? AND source_job_id = ?",
                (_SOURCE, source_job_id),
            ).fetchone()
            return self._row_to_record(dict(row)).model_dump(mode="json") if row else None

    def update_job_fields(
        self,
        source_job_id: str,
        fields: dict[str, Any],
        *,
        profile_id: str | None = None,
        profile_version: int | None = None,
        rubric_version: str | None = None,
        prompt_version: str | None = None,
    ) -> dict[str, Any] | None:
        """Update user-managed job fields and queue their Airtable patch."""
        allowed_fields = {
            "application_status",
            "application_notes",
            "skill_stack_fit",
            "semantic_experience_fit",
            "overall_fit",
            "fit_category",
            "fit_explanation",
            "fit_status",
        }
        if not fields or not fields.keys() <= allowed_fields:
            raise ValueError("Only application and fit fields can be updated.")
        if (profile_id is None) != (profile_version is None):
            raise ValueError("Profile ID and version must be supplied together.")
        if profile_id is not None and (rubric_version is None or prompt_version is None):
            raise ValueError("Fit updates require rubric and prompt versions.")
        if profile_id is None and (rubric_version is not None or prompt_version is not None):
            raise ValueError("Rubric and prompt versions require a profile.")

        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE source = ? AND source_job_id = ?",
                (_SOURCE, source_job_id),
            ).fetchone()
            if row is None:
                return None

            existing = self._row_to_record(dict(row))
            updated = JobMirrorRecord.model_validate(
                {**existing.model_dump(), **fields}
            )
            pending_row = connection.execute(
                "SELECT airtable_pending_fields_json FROM job_processing "
                "WHERE source = ? AND source_job_id = ?",
                (_SOURCE, source_job_id),
            ).fetchone()
            if pending_row is None:
                raise RuntimeError(
                    "Job has no processing row; cannot queue its Airtable update."
                )

            serialized = updated.model_dump(mode="json")
            assignments: list[str] = []
            values: list[Any] = []
            for name in fields:
                value = serialized[name]
                if name == "role_matches":
                    value = json.dumps(value, ensure_ascii=False)
                assignments.append(f"{name} = ?")
                values.append(value)
            values.extend([_SOURCE, source_job_id])
            connection.execute(
                f"UPDATE jobs SET {', '.join(assignments)} "
                "WHERE source = ? AND source_job_id = ?",
                values,
            )

            pending_fields = json.loads(
                pending_row["airtable_pending_fields_json"] or "{}"
            )
            for name in fields:
                alias = JobMirrorRecord.model_fields[name].alias or name
                pending_fields[alias] = serialized[name]
            connection.execute(
                """UPDATE job_processing SET airtable_pending_fields_json = ?,
                    processing_status = 'ReadyToSync', airtable_sync_status = 'Pending',
                    airtable_sync_error = NULL, airtable_next_retry_at = NULL,
                    updated_at = ? WHERE source = ? AND source_job_id = ?""",
                (
                    json.dumps(pending_fields, ensure_ascii=False),
                    _utc_now(),
                    _SOURCE,
                    source_job_id,
                )
            )
            if profile_id is not None and profile_version is not None:
                connection.execute(
                    """INSERT INTO fit_assessment_provenance
                       (source, deduplication_key, profile_id, profile_version,
                        rubric_version, prompt_version, assessed_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(source, deduplication_key) DO UPDATE SET
                         profile_id = excluded.profile_id,
                         profile_version = excluded.profile_version,
                         rubric_version = excluded.rubric_version,
                         prompt_version = excluded.prompt_version,
                         assessed_at = excluded.assessed_at""",
                    (
                        _SOURCE,
                        row["deduplication_key"],
                        profile_id,
                        profile_version,
                        rubric_version,
                        prompt_version,
                        _utc_now(),
                    ),
                )
            return serialized

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


def _normalized_text(value: str | None) -> str:
    return " ".join(re.findall(r"[\w]+", (value or "").casefold()))


def _excluded_employment(payload: JobProcessingPayload) -> bool:
    if payload.employment_type in {"Contract", "Freelance"}:
        return True
    details = (payload.employment_type_text or "").casefold()
    return any(term in details for term in ("minijob", "werkstudent", "praktikum", "trainee"))


def _direct_application_url(value: str | None) -> str | None:
    if not value:
        return None
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return None
    path = parts.path.rstrip("/").lower()
    if path in {"", "/jobs", "/job", "/careers", "/career", "/karriere", "/stellenangebote"}:
        return None
    query = urlencode([
        (key, item) for key, item in parse_qsl(parts.query)
        if not key.lower().startswith("utm_") and key.lower() not in {"fbclid", "gclid"}
    ])
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, query, ""))


def _stale_processing_cutoff() -> str:
    return (
        datetime.now(timezone.utc) - timedelta(minutes=_STALE_PROCESSING_MINUTES)
    ).isoformat(timespec="seconds")


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
