"""Job discovery, application tracking, and fit assessment use cases."""

from __future__ import annotations

from typing import Any

from job_scraper.models.jobs import ApplicationStatus, JobMirrorRecord
from job_scraper.models.scoring import FitAssessment, NeedsReviewAssessment
from job_scraper.storage.sqlite_jobs import SQLiteJobRepository


class JobService:
    """Expose job operations without coupling callers to SQLite details."""

    def __init__(self, repository: SQLiteJobRepository) -> None:
        self._repository = repository

    def search_jobs(
        self,
        *,
        query: str | None = None,
        source: str | None = None,
        fit_status: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[JobMirrorRecord]:
        rows = self._repository.list_jobs(
            query=query,
            source=source,
            fit_status=fit_status,
            limit=limit,
            offset=offset,
        )
        return [JobMirrorRecord.model_validate(row) for row in rows]

    def get_job(self, source_job_id: str) -> JobMirrorRecord | None:
        row = self._repository.get_job(source_job_id)
        return JobMirrorRecord.model_validate(row) if row else None

    def update_application(
        self,
        source_job_id: str,
        status: ApplicationStatus,
        notes: str | None = None,
    ) -> JobMirrorRecord:
        fields: dict[str, Any] = {"application_status": status}
        if notes is not None:
            fields["application_notes"] = notes
        return self._update(source_job_id, fields)

    def save_fit_assessment(
        self, source_job_id: str, assessment: FitAssessment,
        *, evaluation_run_id: str | None = None,
    ) -> JobMirrorRecord:
        overall_fit = round(
            (assessment.skill_stack_fit + assessment.semantic_experience_fit) / 2,
            2,
        )
        return self._update(
            source_job_id,
            {
                "skill_stack_fit": assessment.skill_stack_fit,
                "semantic_experience_fit": assessment.semantic_experience_fit,
                "overall_fit": overall_fit,
                "fit_category": assessment.fit_category,
                "fit_explanation": assessment.fit_explanation,
                "fit_status": "Scored",
            },
            profile_id=assessment.profile_id,
            profile_version=assessment.profile_version,
            rubric_version=assessment.rubric_version,
            prompt_version=assessment.prompt_version,
            evaluation_run_id=evaluation_run_id,
        )

    def mark_fit_needs_review(
        self, source_job_id: str, assessment: NeedsReviewAssessment,
        *, evaluation_run_id: str | None = None,
    ) -> JobMirrorRecord:
        """Clear a prior score when the JD cannot be assessed reliably."""
        return self._update(
            source_job_id,
            {
                "skill_stack_fit": None,
                "semantic_experience_fit": None,
                "overall_fit": None,
                "fit_category": None,
                "fit_explanation": assessment.review_reason,
                "fit_status": "NeedsReview",
            },
            profile_id=assessment.profile_id,
            profile_version=assessment.profile_version,
            rubric_version=assessment.rubric_version,
            prompt_version=assessment.prompt_version,
            evaluation_run_id=evaluation_run_id,
        )

    def mark_out_of_scope(self, source_job_id: str, reason: str) -> JobMirrorRecord:
        """Keep an unrelated posting in the tracker without assigning fit scores."""
        if not reason.strip():
            raise ValueError("Out-of-scope classification requires a reason")
        return self._update(
            source_job_id,
            {
                "skill_stack_fit": None,
                "semantic_experience_fit": None,
                "overall_fit": None,
                "fit_category": None,
                "fit_explanation": reason,
                "fit_status": "OutOfScope",
            },
        )

    def _update(
        self,
        source_job_id: str,
        fields: dict[str, Any],
        *,
        profile_id: str | None = None,
        profile_version: int | None = None,
        rubric_version: str | None = None,
        prompt_version: str | None = None,
        evaluation_run_id: str | None = None,
    ) -> JobMirrorRecord:
        row = self._repository.update_job_fields(
            source_job_id,
            fields,
            profile_id=profile_id,
            profile_version=profile_version,
            rubric_version=rubric_version,
            prompt_version=prompt_version,
            evaluation_run_id=evaluation_run_id,
        )
        if row is None:
            raise JobNotFoundError(source_job_id)
        return JobMirrorRecord.model_validate(row)


class JobNotFoundError(LookupError):
    """Raised when a requested source job ID is not in the local job mirror."""
