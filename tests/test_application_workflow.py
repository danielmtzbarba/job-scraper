from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from job_scraper.application.application_workflow import (
    ApplicationWorkflow,
    ApplicationWorkflowError,
)


class _Repository:
    def __init__(self, *, fit_status: str = "Scored", classification: dict | None = None) -> None:
        self.fit_status = fit_status
        self.classification = classification or {
            "status": "Classified", "profile_id": "backend", "profile_version": 1,
            "reason": "Backend role",
        }
        self.updated: list[tuple[str, dict]] = []
        self.attempts: dict[str, dict] = {}

    def get_job(self, _job_id: str) -> dict:
        return {
            "Source": "Agentur für Arbeit", "Source Job ID": "job-1",
            "Deduplication Key": "job-1", "Title": "Backend Engineer",
            "Company": "Example GmbH", "Application URL": "https://example.com/apply",
            "Fit Status": self.fit_status,
        }

    def get_classification(self, _source: str, _key: str) -> dict | None:
        return self.classification

    def update_job_fields(self, job_id: str, fields: dict) -> dict:
        self.updated.append((job_id, fields))
        return {}

    def get_application_attempt_for_job(self, source: str, key: str) -> dict | None:
        return next((row.copy() for row in self.attempts.values()
                     if row["source"] == source and row["deduplication_key"] == key), None)

    def create_application_attempt(self, attempt: dict) -> dict:
        row = {**attempt, "status": "Selected", "review_version": 0,
               "review_digest": None, "approved_at": None, "submit_started_at": None,
               "updated_at": attempt["created_at"]}
        self.attempts[row["id"]] = row
        return row.copy()

    def get_application_attempt(self, attempt_id: str) -> dict | None:
        row = self.attempts.get(attempt_id)
        return row.copy() if row else None

    def transition_application_attempt(
        self, attempt_id: str, expected_status: str, new_status: str, **updates
    ) -> dict:
        row = self.attempts[attempt_id]
        if row["status"] != expected_status or (
            updates.get("expected_review_digest") is not None
            and row["review_digest"] != updates["expected_review_digest"]
        ):
            raise ValueError("Application attempt changed")
        row["status"] = new_status
        row["updated_at"] = datetime.now(timezone.utc).isoformat()
        for key in ("review_digest", "approved_at", "submit_started_at", "cv_variant"):
            if updates.get(key) is not None:
                row[key] = updates[key]
        if new_status in {"Draft", "Inspecting", "NeedsInput"}:
            row["review_digest"] = None
        if updates.get("increment_review"):
            row["review_version"] += 1
        return row.copy()

    def delete_application_attempt(self, attempt_id: str) -> None:
        self.attempts.pop(attempt_id, None)

    def complete_application_attempt(self, attempt_id: str) -> None:
        self.update_job_fields("job-1", {"application_status": "Applied"})
        self.delete_application_attempt(attempt_id)


class _Profiles:
    def get(self, _profile_id: str) -> SimpleNamespace:
        return SimpleNamespace(status="reviewed", version=1)


class ApplicationWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.private = self.root / ".local" / "application"
        self.private.mkdir(parents=True)
        (self.private / "answers.yaml").write_text("facts: {}\n", encoding="utf-8")
        self.repository = _Repository()
        self.workflow = ApplicationWorkflow(self.repository, self.root, _Profiles())

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_readiness_requires_scored_job_and_saved_classification(self) -> None:
        self.repository.fit_status = "Pending"
        with self.assertRaisesRegex(ApplicationWorkflowError, "Only scored"):
            self.workflow.readiness("job-1")

        self.repository.fit_status = "Scored"
        self.repository.classification = None
        with self.assertRaisesRegex(ApplicationWorkflowError, "classification is missing"):
            self.workflow.readiness("job-1")

    def test_non_saved_job_cannot_start_another_application(self) -> None:
        self.repository.get_job = lambda _job_id: {
            "Source": "Agentur für Arbeit", "Source Job ID": "job-1",
            "Deduplication Key": "job-1", "Title": "Backend Engineer",
            "Application URL": "https://example.com/apply", "Fit Status": "Scored",
            "Application Status": "Applied",
        }
        with self.assertRaisesRegex(ApplicationWorkflowError, "not in Saved status"):
            self.workflow.start_attempt("job-1")

    def test_starts_private_attempt_and_retains_until_explicit_discard(self) -> None:
        attempt = self.workflow.start_attempt("job-1")
        attempt_id = attempt["attempt_id"]
        attempt_file = self.private / "attempts" / attempt_id / "attempt.json"
        self.assertTrue(attempt_file.is_file())
        self.assertEqual(json.loads(attempt_file.read_text())["status"], "Selected")
        self.assertEqual(self.workflow.start_attempt("job-1")["attempt_id"], attempt_id)

        with self.assertRaisesRegex(ApplicationWorkflowError, "explicit user decision"):
            self.workflow.discard(attempt_id, False)
        self.assertTrue(attempt_file.is_file())

        result = self.workflow.discard(attempt_id, True)
        self.assertEqual(result["status"], "Discarded")
        self.assertFalse(attempt_file.parent.exists())

    def test_cv_mapping_is_required_before_review_fill(self) -> None:
        attempt = self.workflow.start_attempt("job-1")
        state = self.workflow._load(attempt["attempt_id"])
        state["fields"] = [{"selector": "#name", "label": "Name", "required": False, "type": "text"}]
        self.workflow._save(state)
        with self.assertRaisesRegex(ApplicationWorkflowError, "No prepared PDF is mapped"):
            self.workflow.fill_for_review(attempt["attempt_id"])

    def test_stale_classification_is_blocked(self) -> None:
        class StaleProfiles:
            def get(self, _profile_id: str) -> SimpleNamespace:
                return SimpleNamespace(status="reviewed", version=2)

        workflow = ApplicationWorkflow(self.repository, self.root, StaleProfiles())
        with self.assertRaisesRegex(ApplicationWorkflowError, "stale or not reviewed"):
            workflow.readiness("job-1")

    def test_progressive_fact_requires_approval_and_never_overwrites(self) -> None:
        with self.assertRaisesRegex(ApplicationWorkflowError, "explicit user approval"):
            self.workflow.add_approved_fact("portfolio_url", "https://example.com", False)
        result = self.workflow.add_approved_fact("portfolio_url", "https://example.com", True)
        self.assertEqual(result["status"], "approved")
        self.assertIn('value: "https://example.com"', (self.private / "answers.yaml").read_text())
        with self.assertRaisesRegex(ApplicationWorkflowError, "already exists"):
            self.workflow.add_approved_fact("portfolio_url", "https://other.example", True)

    def test_pilot_talent_pool_choice_requires_employer_specific_approval(self) -> None:
        attempt = self.workflow.start_attempt("job-1")
        state = self.workflow._load(attempt["attempt_id"])
        state["fields"] = [{
            "selector": "#talent-pool", "label": "Add me to the talent pool",
            "tag": "input", "type": "checkbox", "required": False, "options": [],
        }]
        self.workflow._save(state)
        with self.assertRaisesRegex(ApplicationWorkflowError, "explicit user approval"):
            self.workflow.save_answers(attempt["attempt_id"], {"#talent-pool": "yes"})
        result = self.workflow.save_answers(
            attempt["attempt_id"], {"#talent-pool": "yes"}, ["#talent-pool"]
        )
        self.assertEqual(result["saved_fields"], 1)

    def test_unverified_submission_cannot_be_reopened_for_editing(self) -> None:
        attempt_id = self.workflow.start_attempt("job-1")["attempt_id"]
        self.repository.transition_application_attempt(attempt_id, "Selected", "SubmissionUnverified")
        with self.assertRaisesRegex(ApplicationWorkflowError, "confirm the employer result"):
            self.workflow.save_answers(attempt_id, {"#name": "Example"})

    def test_submit_requires_matching_review_approval_and_unverified_close_is_explicit(self) -> None:
        attempt = self.workflow.start_attempt("job-1")
        state = self.workflow._load(attempt["attempt_id"])
        self.workflow._transition(state, "ReadyForReview", review_digest="review-123")
        with self.assertRaisesRegex(ApplicationWorkflowError, "explicit user approval"):
            self.workflow.submit(attempt["attempt_id"], "review-123", False)
        with self.assertRaisesRegex(ApplicationWorkflowError, "exact review digest"):
            self.workflow.submit(attempt["attempt_id"], "wrong", True)

        self.workflow._transition(state, "SubmissionUnverified")
        with self.assertRaisesRegex(ApplicationWorkflowError, "explicit user confirmation"):
            self.workflow.confirm_submitted(attempt["attempt_id"], False)
        result = self.workflow.confirm_submitted(attempt["attempt_id"], True)
        self.assertTrue(result["temporary_attempt_erased"])
        self.assertEqual(self.repository.updated, [("job-1", {"application_status": "Applied"})])


if __name__ == "__main__":
    unittest.main()
