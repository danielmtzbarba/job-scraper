from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from job_scraper.storage.sqlite_jobs import SQLiteJobRepository


class ApplicationAttemptStorageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.repository = SQLiteJobRepository(Path(self.temp.name) / "jobs.db")
        self.repository.initialize()
        with self.repository._connect() as connection:
            connection.execute(
                """INSERT INTO jobs
                   (source, deduplication_key, source_job_id, fit_status,
                    application_status, application_url)
                   VALUES (?, ?, ?, 'Scored', 'Saved', ?)""",
                ("Agentur für Arbeit", "job-1", "job-1", "https://example.com/apply"),
            )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _attempt(self, attempt_id: str) -> dict:
        return {
            "id": attempt_id,
            "source": "Agentur für Arbeit",
            "deduplication_key": "job-1",
            "profile_id": "backend",
            "profile_version": 1,
            "cv_variant": "backend",
            "artifact_ref": attempt_id,
            "created_at": "2026-10-09T12:00:00+00:00",
        }

    def test_one_active_attempt_and_atomic_review_claim(self) -> None:
        self.repository.create_application_attempt(self._attempt("attempt-1"))
        with self.assertRaises(sqlite3.IntegrityError):
            self.repository.create_application_attempt(self._attempt("attempt-2"))

        self.repository.transition_application_attempt(
            "attempt-1", "Selected", "ReadyForReview",
            review_digest="digest-1", increment_review=True,
        )
        with self.assertRaisesRegex(ValueError, "changed"):
            self.repository.transition_application_attempt(
                "attempt-1", "ReadyForReview", "Submitting",
                expected_review_digest="wrong", approved_at="2026-10-09T12:01:00+00:00",
            )
        claimed = self.repository.transition_application_attempt(
            "attempt-1", "ReadyForReview", "Submitting",
            expected_review_digest="digest-1", approved_at="2026-10-09T12:01:00+00:00",
            submit_started_at="2026-10-09T12:01:00+00:00",
        )
        self.assertEqual(claimed["status"], "Submitting")
        self.assertEqual(claimed["review_version"], 1)
        with self.assertRaisesRegex(ValueError, "changed"):
            self.repository.transition_application_attempt(
                "attempt-1", "ReadyForReview", "Submitting",
                expected_review_digest="digest-1",
            )
        reset = self.repository.transition_application_attempt(
            "attempt-1", "Submitting", "ReadyForReview"
        )
        self.assertIsNone(reset["approved_at"])
        self.assertIsNone(reset["submit_started_at"])

    def test_confirmed_submission_updates_outcome_and_deletes_attempt(self) -> None:
        self.repository.create_application_attempt(self._attempt("attempt-1"))
        self.repository.transition_application_attempt("attempt-1", "Selected", "Submitting")
        self.repository.complete_application_attempt("attempt-1")
        self.assertIsNone(self.repository.get_application_attempt("attempt-1"))
        self.assertEqual(self.repository.get_job("job-1")["application_status"], "Applied")

    def test_discard_deletes_process_without_changing_outcome(self) -> None:
        self.repository.create_application_attempt(self._attempt("attempt-1"))
        self.repository.delete_application_attempt("attempt-1")
        self.assertIsNone(self.repository.get_application_attempt("attempt-1"))
        self.assertEqual(self.repository.get_job("job-1")["application_status"], "Saved")


if __name__ == "__main__":
    unittest.main()
