from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from pydantic import ValidationError

from job_scraper.storage.sqlite_jobs import SQLiteJobRepository
from job_scraper.application.application_audit import event


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
        with self.assertRaisesRegex(ValueError, "Review changed"):
            self.repository.approve_application_review("attempt-1", "wrong")
        self.assertIsNotNone(self.repository.approve_application_review(
            "attempt-1", "digest-1")["approved_at"])
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

    def test_confirmed_submission_updates_outcome_and_redacts_attempt(self) -> None:
        self.repository.create_application_attempt(self._attempt("attempt-1"))
        self.repository.transition_application_attempt("attempt-1", "Selected", "Submitting")
        self.repository.complete_application_attempt("attempt-1")
        history = self.repository.get_application_attempt("attempt-1")
        self.assertEqual(history["status"], "Submitted")
        self.assertEqual(history["artifact_ref"], "")
        self.assertIsNone(history["review_digest"])
        self.assertIsNotNone(history["submitted_at"])
        self.assertEqual(len(self.repository.list_application_attempts()), 1)
        self.assertEqual(self.repository.get_job("job-1")["application_status"], "Applied")

    def test_discard_deletes_process_without_changing_outcome(self) -> None:
        self.repository.create_application_attempt(self._attempt("attempt-1"))
        self.repository.append_application_audit_event(event(
            "attempt-1", "answer_saved", "succeeded", target_id="c1"))
        self.repository.delete_application_attempt("attempt-1")
        self.assertIsNone(self.repository.get_application_attempt("attempt-1"))
        self.assertEqual(self.repository.get_job("job-1")["application_status"], "Saved")
        events = self.repository.list_application_audit_events("attempt-1")
        self.assertEqual([row["event_type"] for row in events],
                         ["attempt_started", "answer_saved", "attempt_discarded"])
        self.assertEqual(self.repository.list_application_attempts()[0]["status"], "Discarded")
        with self.assertRaises(ValidationError):
            event("attempt-1", "answer_saved", "succeeded", answer="private value")

    def test_audit_and_state_transition_commit_together(self) -> None:
        self.repository.create_application_attempt(self._attempt("attempt-1"))
        before = self.repository.list_application_audit_events("attempt-1")
        with self.assertRaisesRegex(ValueError, "changed"):
            self.repository.transition_application_attempt(
                "attempt-1", "ReadyForReview", "Submitting")
        self.assertEqual(self.repository.list_application_audit_events("attempt-1"), before)
        self.assertEqual(self.repository.get_application_attempt("attempt-1")["status"], "Selected")


if __name__ == "__main__":
    unittest.main()
