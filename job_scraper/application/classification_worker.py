"""Server-owned classification of newly enriched canonical jobs."""

from __future__ import annotations

import logging
from threading import Event
from uuid import uuid4

from job_scraper.application.classification import (
    CLASSIFIER_PROMPT_VERSION,
    render_classification_prompt,
)
from job_scraper.application.model_evaluation import ModelEvaluator, ScoringSettings, StructuredModel
from job_scraper.application.profiles import ProfileStore
from job_scraper.models.jobs import JobMirrorRecord
from job_scraper.models.scoring import ProfileClassification
from job_scraper.storage.sqlite_jobs import SQLiteJobRepository

logger = logging.getLogger(__name__)


class ClassificationWorker:
    def __init__(
        self,
        repository: SQLiteJobRepository,
        profiles: ProfileStore,
        model: StructuredModel,
        settings: ScoringSettings,
    ) -> None:
        self.repository = repository
        self.profiles = profiles
        self.evaluator = ModelEvaluator(repository, model, settings)

    def run_once(self) -> str | None:
        """Process one queued job, returning its classification status."""
        available = self.profiles.list()
        if len(available) != 5 or any(profile.status != "reviewed" for profile in available):
            raise ValueError("All five current scoring profiles must be reviewed")
        by_id = {profile.id: profile for profile in available}
        row = self.repository.claim_next_classification_job()
        if row is None:
            return None
        claimed_at = row.pop("_classification_claimed_at")
        job = JobMirrorRecord.model_validate(row)
        try:
            result, evaluation_run_id = self.evaluator.call(
                job,
                batch_id=str(uuid4()),
                stage="classification",
                prompt=render_classification_prompt(job, available),
                schema=ProfileClassification,
                classifier_prompt_version=CLASSIFIER_PROMPT_VERSION,
            )
            profile = by_id[result.profile_id] if result.profile_id else None
            self.repository.complete_classification(
                source=job.source,
                deduplication_key=job.deduplication_key,
                claimed_at=claimed_at,
                profile_id=str(profile.id) if profile else None,
                profile_version=profile.version if profile else None,
                reason=result.reason,
                classifier_prompt_version=CLASSIFIER_PROMPT_VERSION,
                evaluation_run_id=evaluation_run_id,
            )
            logger.info(
                "classification_completed",
                extra={"source_job_id": job.source_job_id, "status":
                       "Classified" if profile else "OutOfScope"},
            )
            return "Classified" if profile else "OutOfScope"
        except Exception as exc:
            self.repository.fail_classification(
                job.source, job.deduplication_key, claimed_at, type(exc).__name__
            )
            logger.warning(
                "classification_failed",
                extra={"source_job_id": job.source_job_id, "error_type": type(exc).__name__},
            )
            return "Failed"


def classification_loop(worker: ClassificationWorker, stop: Event, poll_seconds: int) -> None:
    """Run in a dedicated thread so Vertex calls never block FastAPI requests."""
    while not stop.is_set():
        try:
            worker.run_once()
        except Exception:
            logger.exception("classification_worker_iteration_failed")
        stop.wait(poll_seconds)
