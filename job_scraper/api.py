"""Local FastAPI endpoints for importing supplied job HTML into SQLite."""

from __future__ import annotations

import asyncio
import os
import random
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
import httpx
from curl_cffi.requests import AsyncSession
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile

from job_scraper.models.jobs import (
    JobDetailResponse,
    JobListResponse,
    JobMirrorRecord,
    JobSummary,
    SearchImportResponse,
)
from job_scraper.application.airtable_sync import airtable_sync_worker
from job_scraper.application.jobs import JobService
from job_scraper.application.profiles import ProfileStore
from job_scraper.integrations.airtable import AirtableSettings
from job_scraper.mcp.server import ServerContext, create_server
from job_scraper.sources.arbeitsagentur.html_parser import JobPosting, parse_html
from job_scraper.logging_config import setup_logging
from job_scraper.storage.sqlite_jobs import SQLiteJobRepository

MAX_HTML_BYTES = 10 * 1024 * 1024
SOURCE_NAME = "Agentur für Arbeit"
BA_ORIGIN = "https://www.arbeitsagentur.de"
JOB_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")

logger = setup_logging("job-scraper-api")
PROJECT_ROOT = Path(__file__).resolve().parents[1]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
BLOCKED_STATUS_CODES = {403, 429, 503}


def _database_path() -> Path:
    configured = os.getenv("JOB_SCRAPER_DB_PATH", ".local/jobs.db")
    path = Path(configured).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def _profile_dir() -> Path:
    path = Path(os.getenv("JOB_SCRAPER_PROFILE_DIR", ".local/profiles")).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def _detail_fetch_interval() -> int:
    try:
        return max(1, int(os.getenv("JOB_SCRAPER_DETAIL_FETCH_INTERVAL", "60")))
    except ValueError:
        logger.warning(
            "invalid_detail_fetch_interval",
            configured_value=os.getenv("JOB_SCRAPER_DETAIL_FETCH_INTERVAL"),
            fallback_seconds=60,
        )
        return 60


async def detail_fetcher_cron(
    repository: SQLiteJobRepository, interval_seconds: int = 60
) -> None:
    """Continuously enrich queued BA jobs, retrying transient failures up to three times."""
    logger.info("detail_fetcher_started", interval_seconds=interval_seconds)
    while True:
        try:
            job_id = repository.claim_next_detail_job()
            if job_id:
                detail_url = f"{BA_ORIGIN}/jobsuche/jobdetail/{job_id}"
                try:
                    logger.info("detail_fetch_started", source_job_id=job_id)
                    html = await _fetch_html_smart(detail_url)
                    postings = parse_html(html, source_url=detail_url)
                    detail = next(
                        (job for job in postings if job.source_job_id == job_id), None
                    )
                    if detail is None:
                        raise ValueError(f"Detail page did not parse job {job_id}")
                    updated = repository.enrich_from_detail(job_id, detail)
                    if updated is None:
                        raise LookupError(f"Job {job_id} no longer exists in the repository")
                    logger.info("detail_fetch_completed", source_job_id=job_id)
                except asyncio.CancelledError:
                    repository.update_processing_status(job_id, "Pending")
                    raise
                except Exception:
                    attempts = repository.get_fetch_attempts(job_id)
                    next_status = "Pending" if attempts < 3 else "Failed"
                    repository.update_processing_status(job_id, next_status, str(exc))
                    logger.warning(
                        "detail_fetch_failed",
                        source_job_id=job_id,
                        attempt=attempts,
                        next_status=next_status,
                        error_type=type(exc).__name__,
                    )
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("detail_fetcher_iteration_failed")

        sleep_seconds = max(1, interval_seconds + random.uniform(-5, 5))
        await asyncio.sleep(sleep_seconds)
    logger.info("detail_fetcher_stopped")


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_dotenv(override=False)
    repository = SQLiteJobRepository(_database_path())
    repository.initialize()
    app.state.jobs = repository
    app.state.profiles = ProfileStore(_profile_dir())
    worker_task = asyncio.create_task(
        detail_fetcher_cron(repository, interval_seconds=_detail_fetch_interval())
    )
    airtable_settings = AirtableSettings.from_environment()
    airtable_task = None
    if airtable_settings:
        airtable_task = asyncio.create_task(
            airtable_sync_worker(repository, airtable_settings)
        )
    else:
        logger.warning("airtable_sync", action="worker_disabled")
    try:
        async with mcp_server.session_manager.run():
            yield
    finally:
        worker_task.cancel()
        if airtable_task:
            airtable_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass
        if airtable_task:
            try:
                await airtable_task
            except asyncio.CancelledError:
                pass
        logger.info("api_shutdown_complete")


app = FastAPI(
    title="Job Scraper Local API",
    description="Import Arbeitsagentur HTML into a local SQLite database via upload or direct fetch.",
    version="0.1.0",
    lifespan=lifespan,
)


def _mcp_context() -> ServerContext:
    return ServerContext(
        jobs=JobService(app.state.jobs),
        profiles=app.state.profiles,
    )


mcp_server = create_server(context_provider=_mcp_context)
mcp_http_app = mcp_server.streamable_http_app(json_response=True)


def _repository(request: Request) -> SQLiteJobRepository:
    return request.app.state.jobs


async def _read_html(file: UploadFile) -> str:
    contents = await file.read(MAX_HTML_BYTES + 1)
    if len(contents) > MAX_HTML_BYTES:
        raise HTTPException(status_code=413, detail="HTML upload exceeds the 10 MiB limit.")
    try:
        return contents.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="HTML must be UTF-8 encoded.") from exc


async def _fetch_html_smart(url: str) -> str:
    """Fetch HTML using httpx, falling back to curl_cffi if blocked by a WAF."""
    try:
        headers = {"User-Agent": USER_AGENT}
        async with httpx.AsyncClient(follow_redirects=True, timeout=8.0) as client:
            response = await client.get(url, headers=headers)

            if response.status_code in BLOCKED_STATUS_CODES:
                raise httpx.HTTPStatusError(
                    f"Blocked with status code {response.status_code}",
                    request=response.request,
                    response=response,
                )

            response.raise_for_status()
            return response.text

    except (httpx.HTTPStatusError, httpx.RequestError) as exc:
        logger.warning(
            "standard_fetch_failed_using_fallback",
            error_type=type(exc).__name__,
        )

        try:
            async with AsyncSession(impersonate="chrome120") as session:
                response = await session.get(url, timeout=10)
                if response.status_code >= 400:
                    raise HTTPException(
                        status_code=response.status_code,
                        detail=f"curl_cffi failed to bypass blocking (Status: {response.status_code})"
                    )
                return response.text
        except Exception as fallback_exc:
            logger.error(
                "curl_cffi_fallback_failed",
                error_type=type(fallback_exc).__name__,
            )
            raise HTTPException(
                status_code=502,
                detail=f"Both standard fetch and TLS impersonation fallback failed: {fallback_exc}"
            )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "storage": "sqlite"}


# --- Existing Upload Endpoints ---

@app.post("/imports/search-results", response_model=SearchImportResponse)
async def import_search_results(
    request: Request,
    file: UploadFile = File(...),
    source_url: str | None = Form(default=None),
) -> dict[str, Any]:
    """Parse a supplied results page and upsert its job cards into SQLite."""
    html = await _read_html(file)
    postings = parse_html(html, source_url=source_url)
    if not postings:
        raise HTTPException(status_code=422, detail="No job postings were found in the HTML.")

    counts = _repository(request).upsert_search_results(postings)
    return {
        "found": len(postings),
        **counts,
        "jobs": [_job_summary(posting) for posting in postings],
    }


@app.post(
    "/jobs/{source_job_id}/detail",
    response_model=JobDetailResponse,
    response_model_by_alias=False,
)
async def import_job_detail(
    source_job_id: str,
    request: Request,
    file: UploadFile = File(...),
    source_url: str | None = Form(default=None),
) -> dict[str, Any]:
    """Enrich an existing result-card row from supplied detail-page HTML."""
    if not JOB_ID_PATTERN.fullmatch(source_job_id):
        raise HTTPException(status_code=422, detail="Invalid source job ID.")

    html = await _read_html(file)
    detail_url = source_url or f"{BA_ORIGIN}/jobsuche/jobdetail/{source_job_id}"
    postings = parse_html(html, source_url=detail_url)
    detail = next((job for job in postings if job.source_job_id == source_job_id), None)

    if detail is None:
        raise HTTPException(
            status_code=422,
            detail="The supplied HTML did not contain the requested job ID.",
        )

    updated = _repository(request).enrich_from_detail(source_job_id, detail)
    if updated is None:
        raise HTTPException(
            status_code=404,
            detail="Import this job's search-results page before adding its detail page.",
        )
    return {"updated": True, "job": updated}


# --- New Smart Fetch Endpoints ---

@app.post("/fetch/search-results", response_model=SearchImportResponse)
async def fetch_and_import_search_results(
    request: Request,
    source_url: str = Query(..., description="Target URL of the search results page to scrape"),
) -> dict[str, Any]:
    """Fetch a results page from a URL and upsert its job cards into SQLite."""
    html = await _fetch_html_smart(source_url)
    postings = parse_html(html, source_url=source_url)
    if not postings:
        raise HTTPException(status_code=422, detail="No job postings were found at the provided URL.")

    counts = _repository(request).upsert_search_results(postings)
    return {
        "found": len(postings),
        **counts,
        "jobs": [_job_summary(posting) for posting in postings],
    }


@app.post(
    "/fetch/jobs/{source_job_id}/detail",
    response_model=JobDetailResponse,
    response_model_by_alias=False,
)
async def fetch_and_import_job_detail(
    source_job_id: str,
    request: Request,
    source_url: str | None = Query(default=None, description="Optional override for the detail page URL"),
) -> dict[str, Any]:
    """Fetch detail-page HTML directly from a URL and enrich an existing result-card row."""
    if not JOB_ID_PATTERN.fullmatch(source_job_id):
        raise HTTPException(status_code=422, detail="Invalid source job ID.")

    detail_url = source_url or f"{BA_ORIGIN}/jobsuche/jobdetail/{source_job_id}"
    html = await _fetch_html_smart(detail_url)

    postings = parse_html(html, source_url=detail_url)
    detail = next((job for job in postings if job.source_job_id == source_job_id), None)

    if detail is None:
        raise HTTPException(
            status_code=422,
            detail="The fetched HTML did not contain the requested job ID.",
        )

    updated = _repository(request).enrich_from_detail(source_job_id, detail)
    if updated is None:
        raise HTTPException(
            status_code=404,
            detail="Import this job's search-results page before adding its detail page.",
        )
    return {"updated": True, "job": updated}


# --- Standard Read Endpoints ---

@app.get("/jobs", response_model=JobListResponse, response_model_by_alias=False)
def list_jobs(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    source: str | None = None,
) -> dict[str, Any]:
    jobs = _repository(request).list_jobs(limit=limit, offset=offset, source=source)
    return {"count": len(jobs), "limit": limit, "offset": offset, "jobs": jobs}


@app.get(
    "/jobs/{source_job_id}",
    response_model=JobMirrorRecord,
    response_model_by_alias=False,
)
def get_job(source_job_id: str, request: Request) -> dict[str, Any]:
    job = _repository(request).get_job(source_job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    return job


def _job_summary(posting: JobPosting) -> JobSummary:
    return JobSummary(
        source_job_id=posting.source_job_id,
        title=posting.title,
        company=posting.company,
        job_url=posting.job_url,
        employer_job_url=posting.employer_job_url,
        posted_at=posting.posted_at,
    )


# Keep this mount after API routes; the MCP app serves /mcp on the same port.
app.mount("/", mcp_http_app)
