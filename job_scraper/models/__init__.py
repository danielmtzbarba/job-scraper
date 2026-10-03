"""Pydantic models used at API and persistence boundaries."""

from job_scraper.models.jobs import (
    AirtableSyncWorkItem,
    JobDetailResponse,
    JobListResponse,
    JobMirrorRecord,
    JobProcessingPayload,
    JobSummary,
    SearchImportResponse,
)
from job_scraper.models.scoring import FitAssessment, NeedsReviewAssessment
from job_scraper.models.profiles import ProfileEvidence, ProfileId, ProfileSkill, ScoringProfile

__all__ = [
    "AirtableSyncWorkItem",
    "FitAssessment",
    "NeedsReviewAssessment",
    "JobDetailResponse",
    "JobListResponse",
    "JobMirrorRecord",
    "JobProcessingPayload",
    "JobSummary",
    "ProfileEvidence",
    "ProfileId",
    "ProfileSkill",
    "ScoringProfile",
    "SearchImportResponse",
]
