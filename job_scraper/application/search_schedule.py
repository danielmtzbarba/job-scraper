"""Local, staggered Agentur für Arbeit searches."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Awaitable, Callable
from urllib.parse import urlencode
from uuid import uuid4
from zoneinfo import ZoneInfo

import structlog

from job_scraper.sources.arbeitsagentur.html_parser import parse_html
from job_scraper.sources.arbeitsagentur.browser import SearchPage
from job_scraper.storage.sqlite_jobs import SQLiteJobRepository

BERLIN = ZoneInfo("Europe/Berlin")
BA_SEARCH_URL = "https://www.arbeitsagentur.de/jobsuche/suche"
FetchHtml = Callable[[str], Awaitable[str | SearchPage]]
logger = structlog.get_logger("job-scraper-search")


@dataclass(frozen=True)
class SearchSpec:
    id: str
    keyword: str
    hour: int
    minute: int
    location: str | None = None
    radius_km: int | None = None

    @property
    def url(self) -> str:
        params: dict[str, str] = {
            "suchbereich": "jobs",
            "angebotsart": "1",
            "was": self.keyword,
            "sort": "veroeffdatum",
        }
        if self.location:
            params["wo"] = self.location
        if self.radius_km is not None:
            params["umkreis"] = str(self.radius_km)
        return f"{BA_SEARCH_URL}?{urlencode(params)}"

    def public_dict(self) -> dict[str, object]:
        return {**asdict(self), "time_zone": "Europe/Berlin", "url": self.url}


SEARCHES = (
    SearchSpec("ai_engineer_de", "AI Engineer", 7, 0, "Deutschland (Land)"),
    SearchSpec("ki_entwickler_de", "KI Entwickler", 7, 15, "Deutschland (Land)"),
    SearchSpec("fde_de", "Forward Deployed Engineer", 7, 30, "Deutschland (Land)"),
    SearchSpec("applied_ai_de", "Applied AI Engineer", 7, 45, "Deutschland (Land)"),
    SearchSpec("python_entwickler_de", "Python Entwickler", 8, 0, "Deutschland (Land)"),
    SearchSpec("backend_python_de", "Backend Engineer Python", 8, 15, "Deutschland (Land)"),
    SearchSpec("ai_engineer_dresden", "AI Engineer", 8, 30, "Dresden", 50),
    SearchSpec("python_dresden", "Softwareentwickler Python", 8, 45, "Dresden", 50),
    SearchSpec("devops_de", "DevOps Engineer", 9, 0, "Deutschland (Land)"),
    SearchSpec("devops_dresden", "DevOps Engineer", 9, 15, "Dresden", 50),
)
SEARCH_BY_ID = {search.id: search for search in SEARCHES}


def next_start(search: SearchSpec, now: datetime) -> datetime:
    local = now.astimezone(BERLIN)
    scheduled = local.replace(hour=search.hour, minute=search.minute, second=0, microsecond=0)
    if scheduled <= local:
        next_day = local.date() + timedelta(days=1)
        scheduled = datetime(next_day.year, next_day.month, next_day.day,
                             search.hour, search.minute, tzinfo=BERLIN)
    return scheduled


async def execute_search(
    repository: SQLiteJobRepository,
    search: SearchSpec,
    run_id: str,
    fetch_html: FetchHtml,
) -> None:
    logger.info("search_run_started", search_id=search.id, run_id=run_id)
    try:
        fetched = await fetch_html(search.url)
        html = fetched.html if isinstance(fetched, SearchPage) else fetched
        postings = parse_html(html, source_url=search.url)
        counts = repository.stage_new_search_results(postings, search_run_id=run_id)
        partial = not fetched.complete if isinstance(fetched, SearchPage) else "Weitere Ergebnisse" in html
        repository.finish_search_run(
            run_id, counts={"found": len(postings), **counts},
            partial=partial,
        )
        logger.info(
            "search_run_completed", search_id=search.id, run_id=run_id,
            found=len(postings), inserted=counts["inserted"],
            duplicate=counts["duplicate"], partial=partial,
        )
    except asyncio.CancelledError:
        repository.finish_search_run(run_id, error="Cancelled")
        logger.warning("search_run_cancelled", search_id=search.id, run_id=run_id)
        raise
    except Exception as exc:
        repository.finish_search_run(run_id, error=f"{type(exc).__name__}: {exc}")
        logger.warning(
            "search_run_failed", search_id=search.id, run_id=run_id,
            error_type=type(exc).__name__,
        )


def claim_search(
    repository: SQLiteJobRepository, search: SearchSpec, slot_date: str | None
) -> str | None:
    run_id = uuid4().hex
    if repository.start_search_run(run_id, search.id, slot_date):
        return run_id
    return None


async def search_scheduler(
    repository: SQLiteJobRepository,
    fetch_html: FetchHtml,
    active_runs: set[asyncio.Task[None]],
) -> None:
    async def schedule_one(search: SearchSpec) -> None:
        while True:
            scheduled = next_start(search, datetime.now(BERLIN))
            await asyncio.sleep(max(0, (scheduled - datetime.now(BERLIN)).total_seconds()))
            run_id = claim_search(repository, search, scheduled.date().isoformat())
            if run_id:
                task = asyncio.create_task(execute_search(repository, search, run_id, fetch_html))
                active_runs.add(task)
                task.add_done_callback(active_runs.discard)

    async with asyncio.TaskGroup() as group:
        for search in SEARCHES:
            group.create_task(schedule_one(search))
