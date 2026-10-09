"""FastAPI endpoints for importing and tracking job postings."""

from __future__ import annotations

import asyncio
import os
import random
import re
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Event
from typing import Any, Literal
from urllib.parse import urlsplit

from dotenv import load_dotenv
from fastapi.responses import FileResponse

# aiohttp builds its default TLS context during import. Load the local CA path
# before importing integrations that use it (Cloud SQL in particular).
load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)

import httpx
from curl_cffi.requests import AsyncSession
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel

from job_scraper.models.jobs import (
    JobDetailResponse,
    JobListResponse,
    JobMirrorRecord,
    JobSummary,
    SearchImportResponse,
)
from job_scraper.application.classification_worker import ClassificationWorker, classification_loop
from job_scraper.application.scoring_worker import ScoringWorker, scoring_loop
from job_scraper.application.jobs import JobService
from job_scraper.application.application_workflow import ApplicationWorkflow, ApplicationWorkflowError
from job_scraper.application.model_evaluation import ScoringSettings
from job_scraper.application.profiles import ProfileStore
from job_scraper.application.search_schedule import (
    SEARCH_BY_ID, SEARCHES, claim_search, execute_search, search_scheduler,
)
from job_scraper.application.workflow_status import workflow_status
from job_scraper.integrations.vertex_ai import VertexModelClient
from job_scraper.mcp.server import ServerContext, create_server
from job_scraper.sources.arbeitsagentur.html_parser import JobPosting, parse_html
from job_scraper.sources.arbeitsagentur.browser import fetch_all_search_results
from job_scraper.logging_config import setup_logging
from job_scraper.storage.sqlite_jobs import SQLiteJobRepository
from job_scraper.storage.repository import create_repository

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


def _classification_settings() -> ScoringSettings | None:
    if os.getenv("JOB_SCRAPER_AUTO_CLASSIFY") != "1":
        return None
    return ScoringSettings(
        project_id=os.getenv("GOOGLE_CLOUD_PROJECT", ""),
        location=os.getenv("GOOGLE_CLOUD_LOCATION", ""),
        model_id=os.getenv("JOB_SCRAPER_VERTEX_MODEL", ""),
        input_price_per_million=float(os.getenv("JOB_SCRAPER_INPUT_PRICE_PER_MILLION", "0")),
        output_price_per_million=float(os.getenv("JOB_SCRAPER_OUTPUT_PRICE_PER_MILLION", "0")),
    )


def _scoring_settings() -> ScoringSettings | None:
    if os.getenv("JOB_SCRAPER_AUTO_SCORE") != "1":
        return None
    return ScoringSettings(
        project_id=os.getenv("GOOGLE_CLOUD_PROJECT", ""),
        location=os.getenv("GOOGLE_CLOUD_LOCATION", ""),
        model_id=os.getenv("JOB_SCRAPER_VERTEX_MODEL", ""),
        input_price_per_million=float(os.getenv("JOB_SCRAPER_INPUT_PRICE_PER_MILLION", "0")),
        output_price_per_million=float(os.getenv("JOB_SCRAPER_OUTPUT_PRICE_PER_MILLION", "0")),
    )


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
                    logger.info(
                        "detail_fetch_completed", source_job_id=job_id,
                        processing_status=repository.get_processing_status(SOURCE_NAME, job_id),
                    )
                except asyncio.CancelledError:
                    repository.update_processing_status(job_id, "Pending")
                    raise
                except Exception as exc:
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
    repository = create_repository(PROJECT_ROOT)
    try:
        repository.initialize()
    except BaseException:
        if hasattr(repository, "close"):
            repository.close()
        raise
    app.state.jobs = repository
    app.state.profiles = ProfileStore(_profile_dir())
    app.state.applications = ApplicationWorkflow(repository, PROJECT_ROOT, app.state.profiles)
    pause_background_workers = os.getenv("JOB_SCRAPER_PAUSE_BACKGROUND_WORKERS") == "1"
    classification_settings = None if pause_background_workers else _classification_settings()
    scoring_settings = None if pause_background_workers else _scoring_settings()
    app.state.search_tasks = set()
    scheduler_task = None if pause_background_workers else asyncio.create_task(
        search_scheduler(repository, fetch_all_search_results, app.state.search_tasks)
    )
    worker_task = None if pause_background_workers else asyncio.create_task(
        detail_fetcher_cron(repository, interval_seconds=_detail_fetch_interval())
    )
    classification_task = None
    classification_stop = Event()
    classification_model = None
    scoring_task = None
    scoring_stop = Event()
    scoring_model = None
    if classification_settings:
        classification_model = VertexModelClient(
            project_id=classification_settings.project_id,
            location=classification_settings.location,
            model_id=classification_settings.model_id,
        )
        classifier = ClassificationWorker(
            repository, app.state.profiles, classification_model, classification_settings
        )
        classification_task = asyncio.create_task(asyncio.to_thread(
            classification_loop, classifier, classification_stop, 15
        ))
    if scoring_settings:
        scoring_model = VertexModelClient(
            project_id=scoring_settings.project_id,
            location=scoring_settings.location,
            model_id=scoring_settings.model_id,
        )
        scorer = ScoringWorker(
            repository, app.state.profiles, scoring_model, scoring_settings
        )
        scoring_task = asyncio.create_task(asyncio.to_thread(
            scoring_loop, scorer, scoring_stop, 15
        ))
    try:
        async with mcp_server.session_manager.run():
            yield
    finally:
        if scheduler_task:
            scheduler_task.cancel()
        for task in app.state.search_tasks:
            task.cancel()
        if worker_task:
            worker_task.cancel()
        classification_stop.set()
        scoring_stop.set()
        if scheduler_task:
            try:
                await scheduler_task
            except asyncio.CancelledError:
                pass
        if app.state.search_tasks:
            await asyncio.gather(*app.state.search_tasks, return_exceptions=True)
        if worker_task:
            try:
                await worker_task
            except asyncio.CancelledError:
                pass
        if classification_task:
            await classification_task
        if scoring_task:
            await scoring_task
        if classification_model:
            classification_model.close()
        if scoring_model:
            scoring_model.close()
        if hasattr(repository, "close"):
            repository.close()
        logger.info("api_shutdown_complete")


app = FastAPI(
    title="Job Scraper Local API",
    description="Import Arbeitsagentur HTML into the selected job database via upload or direct fetch.",
    version="0.1.0",
    lifespan=lifespan,
)


@app.middleware("http")
async def private_application_cache(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/applications/") or path.startswith("/api/applications/") or path.endswith("/application-attempt"):
        response.headers["Cache-Control"] = "no-store"
    return response


def _mcp_context() -> ServerContext:
    return ServerContext(
        jobs=JobService(app.state.jobs),
        profiles=app.state.profiles,
        applications=app.state.applications,
    )


mcp_server = create_server(context_provider=_mcp_context)
mcp_http_app = mcp_server.streamable_http_app(json_response=True)


def _repository(request: Request) -> SQLiteJobRepository:
    return request.app.state.jobs


def _applications(request: Request) -> ApplicationWorkflow:
    return request.app.state.applications


def _application_result(operation):
    try:
        return operation()
    except ApplicationWorkflowError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


class ApplicationAnswersRequest(BaseModel):
    answers: dict[str, str]
    explicitly_approved_consent_fields: list[str] = []


class ApplicationSubmitRequest(BaseModel):
    review_digest: str
    explicit_user_approval: bool


class ApplicationDecisionRequest(BaseModel):
    explicit_user_confirmation: bool


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
    _validate_ba_url(url)
    try:
        headers = {"User-Agent": USER_AGENT}
        async with httpx.AsyncClient(follow_redirects=False, timeout=8.0) as client:
            response = await client.get(url, headers=headers)

            if response.status_code in BLOCKED_STATUS_CODES:
                raise httpx.HTTPStatusError(
                    f"Blocked with status code {response.status_code}",
                    request=response.request,
                    response=response,
                )

            response.raise_for_status()
            if response.status_code >= 300:
                raise HTTPException(status_code=502, detail="Unexpected redirect from BA Jobsuche.")
            return response.text

    except (httpx.HTTPStatusError, httpx.RequestError) as exc:
        logger.warning(
            "standard_fetch_failed_using_fallback",
            error_type=type(exc).__name__,
        )

        try:
            async with AsyncSession(impersonate="chrome120") as session:
                response = await session.get(url, timeout=10, allow_redirects=False)
                if response.status_code >= 300:
                    raise HTTPException(
                        status_code=response.status_code,
                        detail=f"BA Jobsuche returned status {response.status_code}"
                    )
                return response.text
        except Exception as fallback_exc:
            logger.error(
                "curl_cffi_fallback_failed",
                error_type=type(fallback_exc).__name__,
            )
            raise HTTPException(
                status_code=502,
                detail=f"Both BA fetch methods failed: {type(fallback_exc).__name__}"
            ) from fallback_exc


def _validate_ba_url(url: str) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https" or parsed.hostname != "www.arbeitsagentur.de"
        or parsed.port not in {None, 443} or parsed.username or parsed.password
        or not parsed.path.startswith("/jobsuche/")
    ):
        raise HTTPException(status_code=422, detail="Only HTTPS Arbeitsagentur Jobsuche URLs are accepted.")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "storage": os.getenv("JOB_SCRAPER_STORAGE", "sqlite")}


@app.get("/searches")
def list_searches() -> list[dict[str, object]]:
    return [search.public_dict() for search in SEARCHES]


@app.get("/search-runs")
def list_search_runs(request: Request, limit: int = Query(default=50, ge=1, le=200)) -> list[dict[str, Any]]:
    return _repository(request).list_search_runs(limit)


@app.post("/searches/{search_id}/run", status_code=202)
async def run_search_now(search_id: str, request: Request) -> dict[str, str]:
    search = SEARCH_BY_ID.get(search_id)
    if search is None:
        raise HTTPException(status_code=404, detail="Search not found.")
    run_id = claim_search(_repository(request), search, None)
    if run_id is None:
        raise HTTPException(status_code=409, detail="Search run could not be started.")
    task = asyncio.create_task(execute_search(_repository(request), search, run_id, fetch_all_search_results))
    request.app.state.search_tasks.add(task)
    task.add_done_callback(request.app.state.search_tasks.discard)
    return {"run_id": run_id, "status": "Running"}


class DuplicateResolution(BaseModel):
    decision: Literal["link_existing", "keep_separate"]
    possible_source: str | None = None
    possible_key: str | None = None


@app.get("/duplicate-reviews")
def list_duplicate_reviews(request: Request) -> list[dict[str, Any]]:
    return _repository(request).list_possible_duplicates()


@app.post("/duplicate-reviews/{source}/{deduplication_key:path}/resolve")
def resolve_duplicate_review(
    source: str, deduplication_key: str, decision: DuplicateResolution, request: Request
) -> dict[str, Any]:
    try:
        resolved = _repository(request).resolve_possible_duplicate(
            source, deduplication_key,
            link_existing=decision.decision == "link_existing",
            possible_source=decision.possible_source,
            possible_key=decision.possible_key,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if resolved is None:
        raise HTTPException(status_code=404, detail="Duplicate review not found.")
    return resolved


# --- Existing Upload Endpoints ---

@app.post("/imports/search-results", response_model=SearchImportResponse)
async def import_search_results(
    request: Request,
    file: UploadFile = File(...),
    source_url: str | None = Form(default=None),
) -> dict[str, Any]:
    """Parse a supplied results page and stage only new source postings."""
    html = await _read_html(file)
    postings = parse_html(html, source_url=source_url)
    if not postings:
        raise HTTPException(status_code=422, detail="No job postings were found in the HTML.")

    counts = _repository(request).stage_new_search_results(postings)
    return {
        "found": len(postings),
        **counts,
        "updated": 0,
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
    return {
        "updated": True, "job": updated,
        "processing_status": _repository(request).get_processing_status(SOURCE_NAME, source_job_id),
    }


# --- New Smart Fetch Endpoints ---

@app.post("/fetch/search-results", response_model=SearchImportResponse)
async def fetch_and_import_search_results(
    request: Request,
    source_url: str = Query(..., description="Target URL of the search results page to scrape"),
) -> dict[str, Any]:
    """Fetch a results page and stage only new source postings."""
    fetched = await fetch_all_search_results(source_url)
    postings = parse_html(fetched.html, source_url=source_url)
    if not postings:
        raise HTTPException(status_code=422, detail="No job postings were found at the provided URL.")

    counts = _repository(request).stage_new_search_results(postings)
    return {
        "found": len(postings),
        **counts,
        "updated": 0,
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
    return {
        "updated": True, "job": updated,
        "processing_status": _repository(request).get_processing_status(SOURCE_NAME, source_job_id),
    }


# --- Standard Read Endpoints ---

@app.get("/status", include_in_schema=False)
def status_page() -> FileResponse:
    return FileResponse(PROJECT_ROOT / "job_scraper" / "prototypes" / "status.html")


@app.get("/applications/{attempt_id}", include_in_schema=False)
def application_page(attempt_id: str, request: Request) -> FileResponse:
    _application_result(lambda: _applications(request).get_attempt(attempt_id))
    return FileResponse(PROJECT_ROOT / "job_scraper" / "prototypes" / "application_review.html")


@app.post("/api/jobs/{source_job_id}/application-attempt")
def start_application_attempt(source_job_id: str, request: Request) -> dict[str, Any]:
    return _application_result(lambda: _applications(request).start_attempt(source_job_id))


@app.get("/api/applications/{attempt_id}")
def get_application_attempt(attempt_id: str, request: Request) -> dict[str, Any]:
    return _application_result(lambda: _applications(request).get_attempt(attempt_id))


@app.get("/api/applications/{attempt_id}/capture")
def get_application_capture(attempt_id: str, request: Request) -> FileResponse:
    path = _application_result(lambda: _applications(request).capture_path(attempt_id))
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-store"})


@app.post("/api/applications/{attempt_id}/inspect")
def inspect_application(attempt_id: str, request: Request) -> dict[str, Any]:
    return _application_result(lambda: _applications(request).inspect_form(attempt_id))


@app.post("/api/applications/{attempt_id}/answers")
def save_application_answers(
    attempt_id: str, body: ApplicationAnswersRequest, request: Request
) -> dict[str, Any]:
    return _application_result(lambda: _applications(request).save_answers(
        attempt_id, body.answers, body.explicitly_approved_consent_fields
    ))


@app.post("/api/applications/{attempt_id}/review")
def prepare_application_review(attempt_id: str, request: Request) -> dict[str, Any]:
    return _application_result(lambda: _applications(request).fill_for_review(attempt_id))


@app.post("/api/applications/{attempt_id}/submit")
def submit_application(
    attempt_id: str, body: ApplicationSubmitRequest, request: Request
) -> dict[str, Any]:
    return _application_result(lambda: _applications(request).submit(
        attempt_id, body.review_digest, body.explicit_user_approval
    ))


@app.post("/api/applications/{attempt_id}/confirm-submitted")
def confirm_application_submitted(
    attempt_id: str, body: ApplicationDecisionRequest, request: Request
) -> dict[str, Any]:
    return _application_result(lambda: _applications(request).confirm_submitted(
        attempt_id, body.explicit_user_confirmation
    ))


@app.post("/api/applications/{attempt_id}/discard")
def discard_application_attempt(
    attempt_id: str, body: ApplicationDecisionRequest, request: Request
) -> dict[str, Any]:
    return _application_result(lambda: _applications(request).discard(
        attempt_id, body.explicit_user_confirmation
    ))


@app.get("/api/status")
def get_status(request: Request) -> dict[str, Any]:
    return workflow_status(_repository(request))


@app.get("/jobs", response_model=JobListResponse, response_model_by_alias=False)
@app.get("/api/jobs", response_model=JobListResponse, response_model_by_alias=False)
def list_jobs(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    source: str | None = None,
    fit_status: str | None = None,
) -> dict[str, Any] | FileResponse:
    if request.url.path == "/jobs" and "text/html" in request.headers.get("accept", "").lower():
        return FileResponse(PROJECT_ROOT / "job_scraper" / "prototypes" / "scored_jobs.html")
    jobs = _repository(request).list_jobs(
        limit=limit, offset=offset, source=source, fit_status=fit_status
    )
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
