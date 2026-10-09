"""Optional local Chromium integration; never contacts an employer."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import quote

from job_scraper.application.application_workflow import ApplicationWorkflow, ApplicationWorkflowError
from job_scraper.application.browser_agent import BrowserAction
from job_scraper.storage.sqlite_jobs import SQLiteJobRepository


FORM = b'''<!doctype html><html><head><title>Mock application</title></head><body>
<form onsubmit="event.preventDefault();document.body.textContent='Thank you. Application received';">
<section id="first"><label>Name<input name="name" required></label>
<button type="button" onclick="document.querySelector('#first').hidden=true;document.querySelector('#second').hidden=false">Next</button></section>
<section id="second" hidden><label>Email<input name="email" type="email" required></label>
<label>CV<input type="file" name="cv" required></label><button type="submit">Submit application</button></section>
</form></body></html>'''


@unittest.skipUnless(os.getenv("JOB_SCRAPER_BROWSER_TEST") == "1", "requires local Chromium")
class ApplicationBrowserIntegrationTest(unittest.TestCase):
    def test_multistep_review_approval_and_redaction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = SQLiteJobRepository(root / "jobs.db")
            repository.initialize()
            url = "data:text/html," + quote(FORM.decode())
            with repository._connect() as connection:
                connection.execute(
                    """INSERT INTO jobs (source,deduplication_key,source_job_id,title,company,
                       fit_status,application_status,application_url)
                       VALUES ('Agentur für Arbeit','job-1','job-1','Engineer','Fixture Inc',
                               'Scored','Saved',?)""", (url,)
                )
                connection.execute(
                    """INSERT INTO job_classifications
                       (source,deduplication_key,status,profile_id,profile_version,created_at,updated_at)
                       VALUES ('Agentur für Arbeit','job-1','Classified','backend',1,'2026-10-09','2026-10-09')"""
                )
            private = root / ".local" / "application"
            private.mkdir(parents=True)
            cv = private / "backend.pdf"
            cv.write_bytes(b"%PDF-1.4\n% fixture only\n%%EOF")
            (private / "cv-map.json").write_text(json.dumps({"profiles": {"backend": str(cv)}}))
            profiles = SimpleNamespace(get=lambda _id: SimpleNamespace(status="reviewed", version=1))
            workflow = ApplicationWorkflow(repository, root, profiles)
            with patch("job_scraper.application.application_workflow._validate_public_url"), \
                 patch("job_scraper.application.application_workflow._guard_browser_request",
                       side_effect=lambda route: route.continue_()):
                attempt_id = workflow.start_attempt("job-1")["attempt_id"]
                first = workflow.observe_page(attempt_id)["observation"]
                name = next(c for c in first["controls"] if c["label"] == "Name")
                workflow.save_agent_answer(attempt_id, first["observation_id"], name["target_id"], "Fixture User")
                after_name = workflow.act_on_page(attempt_id, BrowserAction(
                    observation_id=first["observation_id"], kind="fill",
                    target_id=name["target_id"], value="Fixture User"))["observation"]
                next_button = next(c for c in after_name["controls"] if c["label"] == "Next")
                second = workflow.act_on_page(attempt_id, BrowserAction(
                    observation_id=after_name["observation_id"], kind="click",
                    target_id=next_button["target_id"]))["observation"]
                email = next(c for c in second["controls"] if c["label"] == "Email")
                workflow.save_agent_answer(attempt_id, second["observation_id"], email["target_id"], "fixture@example.invalid")
                workflow.act_on_page(attempt_id, BrowserAction(
                    observation_id=second["observation_id"], kind="fill",
                    target_id=email["target_id"], value="fixture@example.invalid"))
                review = workflow.fill_for_review(attempt_id)
                self.assertIn("Fixture User", [a["answer"] for a in review["answers"]])
                with self.assertRaisesRegex(ApplicationWorkflowError, "Approve this exact review"):
                    workflow.submit(attempt_id, review["review_digest"], True)
                workflow.approve_review(attempt_id, review["review_digest"], True)
                result = workflow.submit(attempt_id, review["review_digest"], True)
                self.assertEqual(result["status"], "Submitted")
                self.assertFalse((private / "attempts" / attempt_id).exists())
                self.assertEqual(repository.get_application_attempt(attempt_id)["status"], "Submitted")
                self.assertIsNone(repository.get_application_attempt(attempt_id)["review_digest"])
                self.assertEqual(repository.get_job("job-1")["application_status"], "Applied")


if __name__ == "__main__":
    unittest.main()
