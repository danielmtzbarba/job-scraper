"""Verify scheduled-slot health against the persisted search ledger."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from job_scraper.application.search_schedule import BERLIN, SEARCHES
from job_scraper.application.workflow_status import workflow_status
from job_scraper.storage.sqlite_jobs import SQLiteJobRepository


class WorkflowStatusTest(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.repository = SQLiteJobRepository(Path(temp.name) / "jobs.db")
        self.repository.initialize()

    def test_upcoming_search_becomes_missed_after_grace_period(self) -> None:
        before = workflow_status(self.repository, datetime(2026, 10, 7, 7, 5, tzinfo=BERLIN))
        after = workflow_status(self.repository, datetime(2026, 10, 7, 7, 11, tzinfo=BERLIN))
        self.assertEqual(before["searches"]["runs"][0]["status"], "Upcoming")
        self.assertEqual(after["searches"]["runs"][0]["status"], "Missed")
        self.assertEqual(after["searches"]["missed"], 1)

    def test_recorded_run_clears_missed_slot(self) -> None:
        search = SEARCHES[0]
        self.repository.start_search_run("run-1", search.id, "2026-10-07")
        self.repository.finish_search_run("run-1", counts={"found": 42, "inserted": 3})
        status = workflow_status(self.repository, datetime(2026, 10, 7, 7, 11, tzinfo=BERLIN))
        self.assertEqual(status["searches"]["completed"], 1)
        self.assertEqual(status["searches"]["missed"], 0)
        self.assertEqual(status["searches"]["runs"][0]["found"], 42)

    def test_long_running_search_is_reported_as_stalled(self) -> None:
        self.repository.start_search_run("run-1", SEARCHES[0].id, "2026-10-07")
        with self.repository._connect() as connection:
            connection.execute(
                "UPDATE search_runs SET started_at = ? WHERE id = ?",
                ("2026-10-07T05:00:00+00:00", "run-1"),
            )
        status = workflow_status(self.repository, datetime(2026, 10, 7, 8, 31, tzinfo=BERLIN))
        self.assertEqual(status["searches"]["runs"][0]["status"], "Stalled")
        self.assertGreaterEqual(status["attention_count"], 1)

    def test_unapplied_count_only_includes_saved_jobs(self) -> None:
        with self.repository._connect() as connection:
            for key, application_status in (
                ("saved", "Saved"), ("applied", "Applied"), ("ignored", "Ignored")
            ):
                connection.execute(
                    """INSERT INTO jobs (source, deduplication_key, application_status)
                       VALUES (?, ?, ?)""",
                    ("Agentur für Arbeit", key, application_status),
                )
        status = workflow_status(self.repository, datetime(2026, 10, 7, 8, 31, tzinfo=BERLIN))
        self.assertEqual(status["applications"]["unapplied"], 1)


if __name__ == "__main__":
    unittest.main()
