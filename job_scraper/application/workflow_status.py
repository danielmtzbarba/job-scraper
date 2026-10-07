"""Build a daily operations view from persisted search and worker records."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from job_scraper.application.search_schedule import BERLIN, SEARCHES
from job_scraper.storage.sqlite_jobs import SQLiteJobRepository


def workflow_status(repository: SQLiteJobRepository, now: datetime | None = None) -> dict[str, Any]:
    local_now = (now or datetime.now(BERLIN)).astimezone(BERLIN)
    today = local_now.date()
    data = repository.get_workflow_status(today.isoformat())
    runs_by_search = {run["search_id"]: run for run in data["runs"]}
    schedule = []
    for search in SEARCHES:
        scheduled = datetime(today.year, today.month, today.day,
                             search.hour, search.minute, tzinfo=BERLIN)
        run = runs_by_search.get(search.id)
        status = run["status"] if run else (
            "Missed" if local_now >= scheduled + timedelta(minutes=10) else "Upcoming"
        )
        if run and status == "Running":
            started = datetime.fromisoformat(run["started_at"])
            if local_now - started.astimezone(BERLIN) >= timedelta(minutes=90):
                status = "Stalled"
        schedule.append({
            "search_id": search.id,
            "name": search.keyword + (f" · {search.location}" if search.location != "Deutschland (Land)" else ""),
            "scheduled_at": scheduled.isoformat(),
            "status": status,
            "found": run["found"] if run else None,
            "inserted": run["inserted"] if run else None,
            "error": run["error"] if run else None,
            "started_at": run["started_at"] if run else None,
            "finished_at": run["finished_at"] if run else None,
        })
    processing = data["processing"]
    classifications = data["classifications"]
    fit = data["fit"]
    attention = sum(item["status"] in {"Missed", "Failed", "Partial", "Stalled"} for item in schedule)
    attention += processing.get("Failed", 0) + classifications.get("Failed", 0)
    return {
        "as_of": local_now.isoformat(),
        "timezone": "Europe/Berlin",
        "date": today.isoformat(),
        "attention_count": attention,
        "searches": {
            "scheduled": len(schedule),
            "completed": sum(item["status"] == "Completed" for item in schedule),
            "missed": sum(item["status"] == "Missed" for item in schedule),
            "runs": schedule,
        },
        "applications": {"unapplied": data["applications"].get("Saved", 0)},
        "details": {
            "awaiting": processing.get("Pending", 0) + processing.get("Processing", 0),
            "failed": processing.get("Failed", 0),
        },
        "classification": {
            "awaiting": data["unclassified"] + classifications.get("Pending", 0)
                        + classifications.get("Running", 0),
            "failed": classifications.get("Failed", 0),
        },
        "scoring": {"awaiting": data["scoring_ready"], "scored_total": fit.get("Scored", 0)},
        "issues": data["issues"],
    }
