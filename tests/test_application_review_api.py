from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx2

from job_scraper.api import app
from job_scraper.application.application_workflow import ApplicationWorkflow
from job_scraper.storage.sqlite_jobs import SQLiteJobRepository


class ApplicationReviewApiTest(unittest.TestCase):
    def test_start_review_and_discard_share_one_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = SQLiteJobRepository(root / "jobs.db")
            repository.initialize()
            with repository._connect() as connection:
                connection.execute(
                    """INSERT INTO jobs
                       (source, deduplication_key, source_job_id, title, company,
                        fit_status, application_status, application_url)
                       VALUES (?, ?, ?, ?, ?, 'Scored', 'Saved', ?)""",
                    ("Agentur für Arbeit", "job-1", "job-1", "Engineer", "Example",
                     "https://example.com/apply"),
                )
                connection.execute(
                    """INSERT INTO job_classifications
                       (source, deduplication_key, status, profile_id, profile_version,
                        created_at, updated_at)
                       VALUES (?, ?, 'Classified', 'backend', 1, ?, ?)""",
                    ("Agentur für Arbeit", "job-1", "2026-10-09", "2026-10-09"),
                )
            profiles = SimpleNamespace(get=lambda _id: SimpleNamespace(status="reviewed", version=1))
            app.state.jobs = repository
            app.state.applications = ApplicationWorkflow(repository, root, profiles)

            async def exercise() -> None:
                async with httpx2.AsyncClient(
                    transport=httpx2.ASGITransport(app=app), base_url="http://test"
                ) as client:
                    first = await client.post("/api/jobs/job-1/application-attempt")
                    self.assertEqual(first.status_code, 200, first.text)
                    attempt_id = first.json()["attempt_id"]
                    action = await client.get("/api/jobs/job-1/application-action")
                    self.assertEqual(action.json()["label"], "Continue application")
                    listing = await client.get("/api/applications")
                    self.assertEqual(listing.json()["applications"][0]["id"], attempt_id)
                    self.assertEqual((await client.get("/applications")).status_code, 200)
                    second = await client.post("/api/jobs/job-1/application-attempt")
                    self.assertEqual(second.json()["attempt_id"], attempt_id)
                    review = await client.get(f"/applications/{attempt_id}")
                    self.assertEqual(review.status_code, 200)
                    self.assertIn("Form capture", review.text)
                    detail = await client.get(f"/api/applications/{attempt_id}")
                    self.assertEqual(detail.headers["cache-control"], "no-store")
                    self.assertEqual(detail.json()["status"], "Selected")
                    discarded = await client.post(
                        f"/api/applications/{attempt_id}/discard",
                        json={"explicit_user_confirmation": True},
                    )
                    self.assertEqual(discarded.status_code, 200, discarded.text)
                    self.assertEqual(repository.get_job("job-1")["application_status"], "Saved")
                    history = (await client.get("/api/applications")).json()["applications"]
                    self.assertEqual(history[0]["status"], "Discarded")
                    self.assertEqual((await client.get(f"/api/applications/{attempt_id}")).json()["status"], "Discarded")
                    audit = await client.get(f"/api/applications/{attempt_id}/audit")
                    self.assertEqual(audit.headers["cache-control"], "no-store")
                    events = audit.json()["events"]
                    self.assertEqual([item["event_type"] for item in events],
                                     ["attempt_started", "artifact_erased", "attempt_discarded"])
                    self.assertEqual({item["actor_kind"] for item in events}, {"web"})
                    self.assertTrue(all(item["request_id"] for item in events))

            asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
