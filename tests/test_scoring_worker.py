"""Exercise the triggered scoring gate and audit trail without network calls."""

from __future__ import annotations

import tempfile
import unittest
import sqlite3
from pathlib import Path

from job_scraper.application.classification_worker import ClassificationWorker
from job_scraper.application.scoring_worker import ScoringSettings, ScoringWorker
from job_scraper.integrations.vertex_ai import ModelResponse
from job_scraper.models.profiles import (
    ProfileEvidence,
    ProfileId,
    ProfileSkill,
    ScoringProfile,
)
from job_scraper.models.scoring import ModelFitDecision, ProfileClassification
from job_scraper.sources.arbeitsagentur.html_parser import JobPosting
from job_scraper.storage.sqlite_jobs import SQLiteJobRepository


class FakeProfiles:
    def __init__(self, version: int = 1) -> None:
        self.version = version

    def list(self) -> list[ScoringProfile]:
        return [
            ScoringProfile(
                id=profile_id,
                version=self.version,
                status="reviewed",
                headline=str(profile_id),
                focus=[str(profile_id)],
                skills=[ProfileSkill(name="Python", evidence_ids=["E1"], level="demonstrated")],
                evidence=[ProfileEvidence(
                    id="E1", setting="professional", context="Software delivery",
                    contribution="Built Python services", proof="documented",
                    source_refs=["fixture"],
                )],
            )
            for profile_id in ProfileId
        ]


class FakeModel:
    def __init__(self, *, bad_classification: bool = False) -> None:
        self.calls: list[str] = []
        self.bad_classification = bad_classification

    def generate(self, prompt: str, schema: type) -> ModelResponse:
        self.calls.append(schema.__name__)
        if schema is ProfileClassification:
            if self.bad_classification:
                text = '{"decision":"profile","profile_id":null,"reason":"bad"}'
            elif "Sales representative" in prompt:
                text = '{"decision":"out_of_scope","profile_id":null,"reason":"Sales role"}'
            else:
                text = '{"decision":"profile","profile_id":"backend","reason":"Backend work"}'
        elif schema is ModelFitDecision:
            if "Unclear engineer" in prompt:
                text = ('{"decision":"needs_review","skill_stack_fit":null,'
                        '"semantic_experience_fit":null,"fit_explanation":null,'
                        '"review_reason":"Central responsibilities missing"}')
            else:
                text = ('{"decision":"scored","skill_stack_fit":70,'
                        '"semantic_experience_fit":80,"fit_explanation":"Python work [E1]",'
                        '"review_reason":null}')
        else:
            raise AssertionError(schema)
        return ModelResponse(text, prompt_tokens=1000, candidate_tokens=100, thought_tokens=10)


class TriggeredScoringTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repository = SQLiteJobRepository(Path(temporary.name) / "jobs.db")
        self.repository.initialize()
        self.settings = ScoringSettings(
            project_id="test-project", location="europe-west4", model_id="test-model",
            input_price_per_million=1.0, output_price_per_million=2.0,
        )

    def add_job(self, job_id: str, title: str, posted_at: str) -> None:
        job = JobPosting(
            source="Agentur für Arbeit", source_job_id=job_id,
            title=title, company="Example", job_url=f"https://example.test/{job_id}",
            job_description=f"Responsibilities for {title}", posted_at=posted_at,
            employment_type="Permanent full-time",
        )
        self.repository.stage_new_search_results([job])
        self.repository.enrich_from_detail(job_id, job)

    def test_classifies_every_job_and_skips_out_of_scope(self) -> None:
        self.add_job("old", "Backend engineer", "2026-09-01")
        self.add_job("new", "Sales representative", "2026-10-01")
        model = FakeModel()
        classifier = ClassificationWorker(self.repository, FakeProfiles(), model, self.settings)
        self.assertEqual(classifier.run_once(), "OutOfScope")
        self.assertEqual(classifier.run_once(), "Classified")
        summary = ScoringWorker(self.repository, FakeProfiles(), model, self.settings).run(limit=2)

        self.assertEqual((summary.claimed, summary.scored, summary.failed), (1, 1, 0))
        self.assertEqual(model.calls, ["ProfileClassification", "ProfileClassification", "ModelFitDecision"])
        skipped = self.repository.get_job("new")
        self.assertEqual(skipped["fit_status"], "OutOfScope")
        self.assertIsNone(skipped["overall_fit"])
        self.assertEqual(
            self.repository.get_classification("Agentur für Arbeit", "arbeitsagentur:new")["status"],
            "OutOfScope",
        )
        scored = self.repository.get_job("old")
        self.assertEqual(scored["fit_status"], "Scored")
        self.assertEqual(scored["overall_fit"], 75)
        runs = self.repository.list_evaluation_runs("Agentur für Arbeit", "arbeitsagentur:old")
        self.assertEqual([run["stage"] for run in runs], ["classification", "scoring"])
        self.assertEqual(runs[1]["profile_id"], "backend")
        self.assertEqual(runs[1]["rubric_version"], "2.1.0")
        self.assertEqual(summary.estimated_cost_usd, 0.00122)

    def test_invalid_classification_is_recorded_and_job_remains_pending(self) -> None:
        self.add_job("one", "Backend engineer", "2026-10-01")
        model = FakeModel(bad_classification=True)
        result = ClassificationWorker(
            self.repository, FakeProfiles(), model, self.settings
        ).run_once()

        self.assertEqual(result, "Failed")
        self.assertEqual(self.repository.get_job("one")["fit_status"], "Pending")
        classification = self.repository.get_classification(
            "Agentur für Arbeit", "arbeitsagentur:one"
        )
        self.assertEqual(classification["status"], "Pending")
        self.assertEqual(classification["attempts"], 1)
        runs = self.repository.list_evaluation_runs("Agentur für Arbeit", "arbeitsagentur:one")
        self.assertEqual(runs[0]["status"], "Failed")
        self.assertEqual(runs[0]["error_type"], "ValidationError")
        self.assertEqual(runs[0]["estimated_cost_usd"], 0.00122)

    def test_unclear_jd_needs_review_after_profile_selection(self) -> None:
        self.add_job("unclear", "Unclear engineer", "2026-10-01")
        ClassificationWorker(
            self.repository, FakeProfiles(), FakeModel(), self.settings
        ).run_once()
        summary = ScoringWorker(
            self.repository, FakeProfiles(), FakeModel(), self.settings
        ).run(limit=1)

        self.assertEqual(summary.needs_review, 1)
        job = self.repository.get_job("unclear")
        self.assertEqual(job["fit_status"], "NeedsReview")
        self.assertIsNone(job["overall_fit"])
        runs = self.repository.list_evaluation_runs(
            "Agentur für Arbeit", "arbeitsagentur:unclear"
        )
        self.assertEqual([run["stage"] for run in runs], ["classification", "scoring"])

    def test_claim_uses_newest_posting_and_caps_at_ten(self) -> None:
        for day in range(1, 12):
            self.add_job(f"job-{day}", "Backend engineer", f"2026-09-{day:02d}")
        classifier = ClassificationWorker(
            self.repository, FakeProfiles(), FakeModel(), self.settings
        )
        for _ in range(11):
            self.assertEqual(classifier.run_once(), "Classified")
        claimed = self.repository.claim_pending_scoring_jobs(10)
        self.assertEqual(len(claimed), 10)
        self.assertEqual(claimed[0]["source_job_id"], "job-11")
        self.assertNotIn("job-1", [job["source_job_id"] for job in claimed])

    def test_enriched_job_without_application_link_is_queued_once(self) -> None:
        self.add_job("one", "Backend engineer", "2026-10-01")
        self.assertIsNone(self.repository.get_job("one")["application_url"])
        classification = self.repository.get_classification(
            "Agentur für Arbeit", "arbeitsagentur:one"
        )
        self.assertEqual(classification["status"], "Pending")
        self.repository.initialize()
        self.assertEqual(
            self.repository.get_classification(
                "Agentur für Arbeit", "arbeitsagentur:one"
            )["status"], "Pending",
        )

    def test_classifies_existing_backlog_one_job_per_cycle(self) -> None:
        self.add_job("old", "Backend engineer", "2026-09-01")
        self.add_job("new", "Backend engineer", "2026-10-01")
        with sqlite3.connect(self.repository.database_path) as connection:
            connection.execute("DELETE FROM job_classifications")
        self.repository.initialize()
        self.assertIsNone(self.repository.get_classification(
            "Agentur für Arbeit", "arbeitsagentur:old"
        ))
        classifier = ClassificationWorker(
            self.repository, FakeProfiles(), FakeModel(), self.settings
        )
        self.assertEqual(classifier.run_once(), "Classified")
        self.assertEqual(
            self.repository.get_classification("Agentur für Arbeit", "arbeitsagentur:new")["status"],
            "Classified",
        )
        self.assertIsNone(self.repository.get_classification(
            "Agentur für Arbeit", "arbeitsagentur:old"
        ))
        self.assertEqual(classifier.run_once(), "Classified")
        self.assertEqual(classifier.run_once(), None)

    def test_profile_version_change_requeues_classification(self) -> None:
        self.add_job("one", "Backend engineer", "2026-10-01")
        model = FakeModel()
        ClassificationWorker(self.repository, FakeProfiles(), model, self.settings).run_once()
        summary = ScoringWorker(
            self.repository, FakeProfiles(version=2), model, self.settings
        ).run(limit=1)
        self.assertEqual(summary.reclassification_requested, 1)
        self.assertEqual(summary.scored, 0)
        self.assertEqual(model.calls, ["ProfileClassification"])
        self.assertEqual(self.repository.get_classification(
            "Agentur für Arbeit", "arbeitsagentur:one"
        )["status"], "Pending")


if __name__ == "__main__":
    unittest.main()
