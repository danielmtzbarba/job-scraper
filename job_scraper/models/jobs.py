"""Validated job records for the processing queue and Airtable mirror."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from job_scraper.sources.arbeitsagentur.html_parser import JobPosting


ApplicationStatus = Literal[
    "Saved", "Applied", "Interview", "Offer", "Rejected", "Withdrawn", "Ignored"
]


class JobMirrorRecord(BaseModel):
    """The application-level equivalent of a row in Airtable's Jobs table."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    title: str | None = Field(default=None, alias="Title")
    company: str | None = Field(default=None, alias="Company")
    source: str = Field(alias="Source")
    source_job_id: str | None = Field(default=None, alias="Source Job ID")
    job_url: str | None = Field(default=None, alias="Job URL")
    deduplication_key: str = Field(alias="Deduplication Key")
    job_description: str | None = Field(default=None, alias="Job Description")
    location: str | None = Field(default=None, alias="Location")
    work_mode: str | None = Field(default=None, alias="Work Mode")
    employment_type: str | None = Field(default=None, alias="Employment Type")
    seniority: str | None = Field(default=None, alias="Seniority")
    role_matches: list[str] = Field(default_factory=list, alias="Role Matches")
    application_url: str | None = Field(default=None, alias="Application URL")
    application_status: ApplicationStatus = Field(default="Saved", alias="Application Status")
    application_notes: str | None = Field(default=None, alias="Application Notes")
    skill_stack_fit: float | None = Field(default=None, alias="Skill/Stack Fit")
    semantic_experience_fit: float | None = Field(default=None, alias="Semantic Experience Fit")
    overall_fit: float | None = Field(default=None, alias="Overall Fit")
    fit_category: str | None = Field(default=None, alias="Fit Category")
    fit_explanation: str | None = Field(default=None, alias="Fit Explanation")
    fit_status: str = Field(default="Pending", alias="Fit Status")
    search_run_id: str | None = Field(default=None, alias="Search Run ID")
    posted_at: date | None = Field(default=None, alias="Posted At")

    def to_airtable_fields(self) -> dict[str, object]:
        """Serialize populated values with Airtable's exact field names."""
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)


class JobProcessingPayload(JobMirrorRecord):
    """Staged BA data, including source-only fields not present in Airtable."""

    employer_job_url: str | None = None
    employment_type_text: str | None = None
    posted_at_text: str | None = None

    @classmethod
    def from_posting(cls, posting: JobPosting) -> JobProcessingPayload:
        values = posting.to_dict()
        values["deduplication_key"] = posting.deduplication_key
        values["application_url"] = posting.employer_job_url or posting.application_url
        return cls.model_validate(values)

    def to_mirror_record(self) -> JobMirrorRecord:
        values = self.model_dump(exclude={"employer_job_url", "employment_type_text", "posted_at_text"})
        values["application_url"] = self.employer_job_url or self.application_url
        return JobMirrorRecord.model_validate(values)


class JobSummary(BaseModel):
    source_job_id: str | None = None
    title: str | None = None
    company: str | None = None
    job_url: str | None = None
    employer_job_url: str | None = None
    posted_at: str | None = None


class JobListResponse(BaseModel):
    count: int
    limit: int
    offset: int
    jobs: list[JobMirrorRecord]


class JobDetailResponse(BaseModel):
    updated: bool
    job: JobMirrorRecord


class AirtableSyncWorkItem(BaseModel):
    source: str
    deduplication_key: str
    airtable_record_id: str | None = None
    attempts: int
    record: JobMirrorRecord


class SearchImportResponse(BaseModel):
    found: int
    inserted: int
    updated: int
    skipped: int
    jobs: list[JobSummary]
