"""Local stdio MCP server for searching, scoring, and tracking jobs."""

from __future__ import annotations

import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Callable, Literal

from dotenv import load_dotenv

# Load SSL_CERT_FILE before Cloud SQL's aiohttp dependency is imported.
load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver import Context
from pydantic import BaseModel, ConfigDict, Field

from job_scraper.application.jobs import JobService
from job_scraper.application.profiles import ProfileStore
from job_scraper.application.scoring_prompt import (
    PROMPT_VERSION,
    RUBRIC_VERSION,
    render_scoring_prompt,
)
from job_scraper.logging_config import setup_logging
from job_scraper.models.jobs import ApplicationStatus, JobMirrorRecord
from job_scraper.models.profiles import ProfileId, ScoringProfile
from job_scraper.models.scoring import FitAssessment, NeedsReviewAssessment
from job_scraper.storage.repository import create_repository

# MCP stdio uses stdout for protocol messages, so application logs must use stderr.
setup_logging("job-scraper-mcp", stream=sys.stderr)

class JobScoringContext(BaseModel):
    """Complete scoring prompt plus the versions needed to submit its result."""

    model_config = ConfigDict(extra="forbid")

    source_job_id: str
    profile_id: ProfileId
    profile_version: int
    rubric_version: str
    prompt_version: str
    prompt: str


class ProfileSummary(BaseModel):
    id: ProfileId
    version: int
    status: Literal["draft", "reviewed", "active"]
    headline: str
    focus: list[str]


@dataclass(slots=True)
class ServerContext:
    jobs: JobService
    profiles: ProfileStore


@asynccontextmanager
async def _server_lifespan(_server: MCPServer) -> AsyncIterator[ServerContext]:
    project_root = Path(__file__).resolve().parents[2]
    repository = create_repository(project_root)
    repository.initialize()
    profile_dir = Path(os.getenv("JOB_SCRAPER_PROFILE_DIR", ".local/profiles")).expanduser()
    if not profile_dir.is_absolute():
        profile_dir = project_root / profile_dir

    try:
        yield ServerContext(jobs=JobService(repository), profiles=ProfileStore(profile_dir))
    finally:
        if hasattr(repository, "close"):
            repository.close()


def create_server(
    context_provider: Callable[[], ServerContext] | None = None,
) -> MCPServer:
    """Build the MCP server and register its small, typed tool surface."""
    @asynccontextmanager
    async def server_lifespan(server: MCPServer) -> AsyncIterator[ServerContext]:
        if context_provider is not None:
            yield context_provider()
        else:
            async with _server_lifespan(server) as context:
                yield context

    server = MCPServer(
        "job-scraper",
        instructions=(
            "Use these tools to find jobs, review the five private career profiles, "
            "and score fit against exactly one reviewed profile per job. Scoring "
            "writes are validated and queued for Airtable synchronization."
        ),
        lifespan=server_lifespan,
    )

    @server.tool()
    def search_jobs(
        ctx: Context[ServerContext],
        query: Annotated[str | None, Field(max_length=200)] = None,
        source: Annotated[str | None, Field(max_length=100)] = None,
        fit_status: Literal["Pending", "Scored", "NeedsReview", "OutOfScope"] | None = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> list[JobMirrorRecord]:
        """Search saved job records; use fit_status='Pending' to find jobs to score."""
        return ctx.request_context.lifespan_context.jobs.search_jobs(
            query=query,
            source=source,
            fit_status=fit_status,
            limit=limit,
            offset=offset,
        )

    @server.tool()
    def list_scoring_profiles(ctx: Context[ServerContext]) -> list[ProfileSummary]:
        """List the available private career profiles and their review status."""
        return [
            ProfileSummary.model_validate(
                profile.model_dump(include={"id", "version", "status", "headline", "focus"})
            )
            for profile in ctx.request_context.lifespan_context.profiles.list()
        ]

    @server.tool()
    def get_scoring_profile(
        ctx: Context[ServerContext], profile_id: ProfileId
    ) -> ScoringProfile:
        """Read one complete private profile, including its evidence and review status."""
        return ctx.request_context.lifespan_context.profiles.get(profile_id)

    @server.tool()
    def get_job_for_scoring(
        ctx: Context[ServerContext], source_job_id: str, profile_id: ProfileId
    ) -> JobScoringContext:
        """Get a job, one selected career profile, and the scoring rubric."""
        job = ctx.request_context.lifespan_context.jobs.get_job(source_job_id)
        if job is None:
            raise ToolError(f"Job {source_job_id} was not found")
        profile = ctx.request_context.lifespan_context.profiles.get(profile_id)
        if profile.status == "draft":
            raise ToolError(
                f"{profile_id} is a draft. Call get_scoring_profile to review its "
                "evidence; after the user approves it, change its private JSON "
                "status to 'reviewed' and retry."
            )
        return JobScoringContext(
            source_job_id=source_job_id,
            profile_id=profile.id,
            profile_version=profile.version,
            rubric_version=RUBRIC_VERSION,
            prompt_version=PROMPT_VERSION,
            prompt=render_scoring_prompt(job, profile),
        )

    @server.tool()
    def save_fit_assessment(
        ctx: Context[ServerContext],
        source_job_id: str,
        assessment: FitAssessment,
    ) -> JobMirrorRecord:
        """Save validated scores; overall fit is calculated as the 50/50 average."""
        profile = ctx.request_context.lifespan_context.profiles.get(assessment.profile_id)
        if (
            profile.status == "draft"
            or profile.version != assessment.profile_version
            or assessment.rubric_version != RUBRIC_VERSION
            or assessment.prompt_version != PROMPT_VERSION
        ):
            raise ToolError("Fit assessment requires current reviewed profile and instruction versions")
        return ctx.request_context.lifespan_context.jobs.save_fit_assessment(
            source_job_id, assessment
        )

    @server.tool()
    def mark_fit_needs_review(
        ctx: Context[ServerContext],
        source_job_id: str,
        assessment: NeedsReviewAssessment,
    ) -> JobMirrorRecord:
        """Record an unscorable JD and clear any previous numeric fit score."""
        profile = ctx.request_context.lifespan_context.profiles.get(assessment.profile_id)
        if (
            profile.status == "draft"
            or profile.version != assessment.profile_version
            or assessment.rubric_version != RUBRIC_VERSION
            or assessment.prompt_version != PROMPT_VERSION
        ):
            raise ToolError("Fit review requires current reviewed profile and instruction versions")
        return ctx.request_context.lifespan_context.jobs.mark_fit_needs_review(
            source_job_id, assessment
        )

    @server.tool()
    def update_application_status(
        ctx: Context[ServerContext],
        source_job_id: str,
        status: ApplicationStatus,
        notes: str | None = None,
    ) -> JobMirrorRecord:
        """Update a job's application status and optionally replace its notes."""
        return ctx.request_context.lifespan_context.jobs.update_application(
            source_job_id, status, notes
        )

    return server


def main() -> None:
    """Run the server over stdio for a local MCP host to launch."""
    mcp.run()


mcp = create_server()


if __name__ == "__main__":
    main()
