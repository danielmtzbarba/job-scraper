"""Select the local debugging store or the Cloud SQL production store."""

from __future__ import annotations

import os
from pathlib import Path

from job_scraper.storage.sqlite_jobs import SQLiteJobRepository


def create_repository(project_root: Path) -> SQLiteJobRepository:
    backend = os.getenv("JOB_SCRAPER_STORAGE", "sqlite").strip().lower()
    if backend == "sqlite":
        database_path = Path(os.getenv("JOB_SCRAPER_DB_PATH", ".local/jobs.db")).expanduser()
        if not database_path.is_absolute():
            database_path = project_root / database_path
        return SQLiteJobRepository(database_path)
    if backend == "cloudsql":
        from job_scraper.storage.postgres_jobs import PostgresJobRepository

        return PostgresJobRepository(
            os.getenv("CLOUD_SQL_INSTANCE", ""),
            os.getenv("CLOUD_SQL_DATABASE", ""),
            os.getenv("CLOUD_SQL_IAM_USER", ""),
        )
    raise ValueError("JOB_SCRAPER_STORAGE must be 'sqlite' or 'cloudsql'")
