"""User-triggered Gemini fit scoring for classified jobs."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv

from job_scraper.application.jobs import JobService
from job_scraper.application.model_evaluation import ModelEvaluator, ScoringSettings, StructuredModel
from job_scraper.application.profiles import ProfileStore
from job_scraper.application.scoring_prompt import (
    PROMPT_VERSION,
    RUBRIC_VERSION,
    render_scoring_prompt,
)
from job_scraper.integrations.vertex_ai import VertexModelClient
from job_scraper.models.jobs import JobMirrorRecord
from job_scraper.models.scoring import (
    FitAssessment,
    ModelFitDecision,
    NeedsReviewAssessment,
)
from job_scraper.storage.sqlite_jobs import SQLiteJobRepository
from job_scraper.storage.repository import create_repository


PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class BatchSummary:
    batch_id: str = ""
    claimed: int = 0
    scored: int = 0
    needs_review: int = 0
    reclassification_requested: int = 0
    failed: int = 0
    estimated_cost_usd: float = 0
    unestimated_calls: int = 0


class ScoringWorker:
    def __init__(
        self,
        repository: SQLiteJobRepository,
        profiles: ProfileStore,
        model: StructuredModel,
        settings: ScoringSettings,
    ) -> None:
        self.repository = repository
        self.profiles = profiles
        self.model = model
        self.settings = settings
        self.jobs = JobService(repository)
        self.evaluator = ModelEvaluator(repository, model, settings)

    def run(self, *, limit: int = 10) -> BatchSummary:
        if not 1 <= limit <= 10:
            raise ValueError("A triggered scoring run can include 1–10 jobs")
        available = self.profiles.list()
        if len(available) != 5 or any(profile.status == "draft" for profile in available):
            raise ValueError("All five current scoring profiles must be reviewed")
        by_id = {profile.id: profile for profile in available}
        claimed = self.repository.claim_pending_scoring_jobs(limit)
        summary = BatchSummary(batch_id=str(uuid4()), claimed=len(claimed))
        for row in claimed:
            claimed_at = row.pop("_scoring_claimed_at")
            profile_id = row.pop("_classified_profile_id")
            profile_version = row.pop("_classified_profile_version")
            job = JobMirrorRecord.model_validate(row)
            try:
                profile = by_id[profile_id]
                if profile.version != profile_version:
                    self.repository.requeue_classification(job.source, job.deduplication_key)
                    summary.reclassification_requested += 1
                    continue
                result, scoring_run_id = self.evaluator.call(
                    job,
                    batch_id=summary.batch_id,
                    stage="scoring",
                    prompt=render_scoring_prompt(job, profile, automated=True),
                    schema=ModelFitDecision,
                    profile=profile,
                    rubric_version=RUBRIC_VERSION,
                    prompt_version=PROMPT_VERSION,
                )
                if result.decision == "needs_review":
                    self.jobs.mark_fit_needs_review(
                        job.source_job_id,
                        NeedsReviewAssessment(
                            profile_id=profile.id,
                            profile_version=profile.version,
                            rubric_version=RUBRIC_VERSION,
                            prompt_version=PROMPT_VERSION,
                            review_reason=result.review_reason,
                        ),
                        evaluation_run_id=scoring_run_id,
                    )
                    summary.needs_review += 1
                else:
                    overall = round(
                        (result.skill_stack_fit + result.semantic_experience_fit) / 2, 2
                    )
                    category = (
                        "Strong" if overall >= 85 else
                        "Good" if overall >= 70 else
                        "Stretch" if overall >= 50 else
                        "Low"
                    )
                    self.jobs.save_fit_assessment(
                        job.source_job_id,
                        FitAssessment(
                            profile_id=profile.id,
                            profile_version=profile.version,
                            rubric_version=RUBRIC_VERSION,
                            prompt_version=PROMPT_VERSION,
                            skill_stack_fit=result.skill_stack_fit,
                            semantic_experience_fit=result.semantic_experience_fit,
                            fit_category=category,
                            fit_explanation=result.fit_explanation,
                        ),
                        evaluation_run_id=scoring_run_id,
                    )
                    summary.scored += 1
            except Exception as exc:
                # Model failures have an evaluation record. Persistence errors
                # leave the job Pending for a later user-triggered retry.
                summary.failed += 1
                print(
                    json.dumps({"job_id": job.source_job_id, "error_type": type(exc).__name__}),
                    file=sys.stderr,
                )
            finally:
                self.repository.release_scoring_claim(job.source, job.deduplication_key, claimed_at)
        summary.estimated_cost_usd = self.repository.estimated_batch_cost(summary.batch_id)
        summary.unestimated_calls = self.repository.unestimated_batch_calls(summary.batch_id)
        return summary


def main() -> None:
    """Run only on an explicit command; API startup never starts scoring."""
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    parser = argparse.ArgumentParser(description="Score up to 10 newest classified Pending jobs")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--dry-run", action="store_true", help="List jobs without model calls or claims")
    parser.add_argument("--project", default=os.getenv("GOOGLE_CLOUD_PROJECT"))
    parser.add_argument("--location", default=os.getenv("GOOGLE_CLOUD_LOCATION"))
    parser.add_argument("--model", default=os.getenv("JOB_SCRAPER_VERTEX_MODEL"))
    parser.add_argument("--input-price-per-million", type=float)
    parser.add_argument("--output-price-per-million", type=float)
    args = parser.parse_args()
    if not 1 <= args.limit <= 10:
        parser.error("--limit must be between 1 and 10")
    profile_dir = Path(os.getenv("JOB_SCRAPER_PROFILE_DIR", ".local/profiles")).expanduser()
    if not profile_dir.is_absolute():
        profile_dir = PROJECT_ROOT / profile_dir
    repository = create_repository(PROJECT_ROOT)
    repository.initialize()
    if args.dry_run:
        jobs = repository.preview_pending_scoring_jobs(args.limit)
        print(json.dumps([
            {"source_job_id": job["source_job_id"], "title": job["title"],
             "posted_at": job["posted_at"],
             "selected_profile_id": job["selected_profile_id"],
             "selected_profile_version": job["selected_profile_version"]}
            for job in jobs
        ], ensure_ascii=False))
        if hasattr(repository, "close"):
            repository.close()
        return
    if args.input_price_per_million is None or args.output_price_per_million is None:
        parser.error("Live runs require both token prices from the selected model's current price sheet")
    try:
        settings = ScoringSettings(
            project_id=args.project or "",
            location=args.location or "",
            model_id=args.model or "",
            input_price_per_million=args.input_price_per_million,
            output_price_per_million=args.output_price_per_million,
        )
    except ValueError as exc:
        parser.error(str(exc))
    model = VertexModelClient(
        project_id=settings.project_id,
        location=settings.location,
        model_id=settings.model_id,
    )
    try:
        summary = ScoringWorker(repository, ProfileStore(profile_dir), model, settings).run(
            limit=args.limit
        )
        print(json.dumps(summary.__dict__))
    finally:
        model.close()
        if hasattr(repository, "close"):
            repository.close()


if __name__ == "__main__":
    main()
