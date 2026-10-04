"""Exercise concurrent source inserts and conservative canonical matching."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from job_scraper.sources.arbeitsagentur.html_parser import JobPosting
from job_scraper.application.search_schedule import SEARCHES, claim_search, execute_search
from job_scraper.storage.sqlite_jobs import SQLiteJobRepository


def posting(source: str, job_id: str, url: str | None = None) -> JobPosting:
    return JobPosting(
        source=source,
        source_job_id=job_id,
        title="Python Engineer",
        company="Example GmbH",
        job_url=f"https://example.com/jobs/{job_id}",
        employer_job_url=url,
        employment_type="Permanent full-time",
        job_description=f"Description for {job_id}",
    )


class SearchCollectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repository = SQLiteJobRepository(Path(self.temp.name) / "jobs.db")
        self.repository.initialize()

    def test_concurrent_searches_insert_one_source_posting(self) -> None:
        item = posting("Agentur für Arbeit", "BA-1")
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(
                self.repository.stage_new_search_results, ([item], [item])
            ))
        self.assertEqual(sum(result["inserted"] for result in results), 1)
        self.assertEqual(sum(result["duplicate"] for result in results), 1)

    def test_same_employer_and_title_with_distinct_ba_ids_stay_separate(self) -> None:
        for job_id in ("BA-1", "BA-2"):
            item = posting("Agentur für Arbeit", job_id)
            self.repository.stage_new_search_results([item])
            self.repository.enrich_from_detail(job_id, item)
        self.assertEqual(len(self.repository.list_jobs(limit=10, offset=0)), 2)

    def test_shared_direct_application_url_links_cross_source_posting(self) -> None:
        url = "https://employer.example/jobs/req-123"
        first = posting("Agentur für Arbeit", "BA-1", url)
        second = posting("Other Board", "OTHER-1", url + "?utm_source=board")
        description = "Build production Python services and own their deployment. " * 3
        first.job_description = description
        second.job_description = description
        for item in (first, second):
            self.repository.stage_new_search_results([item])
            self.repository.enrich_from_detail(item.source_job_id, item)
        self.assertEqual(len(self.repository.list_jobs(limit=10, offset=0)), 1)
        self.assertEqual(
            self.repository.get_processing_status("Other Board", "OTHER-1"), "Merged"
        )

    def test_shared_url_with_distinct_descriptions_waits_for_review(self) -> None:
        url = "https://employer.example/jobs/req-123"
        first = posting("Agentur für Arbeit", "BA-1", url)
        second = posting("Other Board", "OTHER-1", url)
        first.job_description = "Build Python APIs for the payments platform. " * 3
        second.job_description = "Build Python APIs for the analytics platform. " * 3
        for item in (first, second):
            self.repository.stage_new_search_results([item])
            self.repository.enrich_from_detail(item.source_job_id, item)
        self.assertEqual(len(self.repository.list_jobs(limit=10, offset=0)), 1)
        self.assertEqual(
            self.repository.get_processing_status(second.source, second.source_job_id),
            "NeedsReview",
        )

    def test_reimport_cannot_reopen_review_or_published_job(self) -> None:
        first = posting("Agentur für Arbeit", "BA-1", "https://a.example/jobs/1")
        second = posting("Other Board", "OTHER-1", "https://b.example/jobs/2")
        for item in (first, second):
            self.repository.stage_new_search_results([item])
            self.repository.enrich_from_detail(item.source_job_id, item)
        changed = second.model_copy(update={"title": "Completely Different Job"})
        self.repository.enrich_from_detail(second.source_job_id, changed)
        self.assertEqual(
            self.repository.get_processing_status(second.source, second.source_job_id),
            "NeedsReview",
        )
        self.repository.enrich_from_detail(first.source_job_id, changed.model_copy(
            update={"source": first.source, "source_job_id": first.source_job_id}
        ))
        self.assertEqual(self.repository.list_jobs(limit=10, offset=0)[0]["title"], first.title)

    def test_review_can_choose_among_multiple_possible_jobs(self) -> None:
        for job_id in ("BA-1", "BA-2"):
            item = posting("Agentur für Arbeit", job_id)
            self.repository.stage_new_search_results([item])
            self.repository.enrich_from_detail(job_id, item)
        other = posting("Other Board", "OTHER-1")
        self.repository.stage_new_search_results([other])
        self.repository.enrich_from_detail(other.source_job_id, other)
        self.assertEqual(len(self.repository.list_possible_duplicates()), 2)
        with self.assertRaises(ValueError):
            self.repository.resolve_possible_duplicate(
                other.source, other.deduplication_key, link_existing=True
            )
        resolved = self.repository.resolve_possible_duplicate(
            other.source, other.deduplication_key, link_existing=True,
            possible_source="Agentur für Arbeit", possible_key="arbeitsagentur:BA-2",
        )
        self.assertEqual(resolved["job"]["source_job_id"], "BA-2")

    def test_similar_cross_source_posting_waits_for_review(self) -> None:
        first = posting("Agentur für Arbeit", "BA-1", "https://a.example/jobs/1")
        second = posting("Other Board", "OTHER-1", "https://b.example/jobs/2")
        for item in (first, second):
            self.repository.stage_new_search_results([item])
            self.repository.enrich_from_detail(item.source_job_id, item)
        self.assertEqual(len(self.repository.list_jobs(limit=10, offset=0)), 1)
        self.assertEqual(len(self.repository.list_possible_duplicates()), 1)
        result = self.repository.resolve_possible_duplicate(
            second.source, second.deduplication_key, link_existing=False
        )
        self.assertEqual(result["resolution"], "separate")
        self.assertEqual(len(self.repository.list_jobs(limit=10, offset=0)), 2)

    def test_search_run_records_insert_then_duplicate_without_a_cap(self) -> None:
        html = '''<script type="application/ld+json">{"@type":"JobPosting",
          "title":"Python Engineer","identifier":"BA-1",
          "url":"https://www.arbeitsagentur.de/jobsuche/jobdetail/BA-1",
          "hiringOrganization":{"name":"Example GmbH"}}</script>'''

        async def fetch(_url: str) -> str:
            return html

        search = SEARCHES[0]
        for expected_inserted in (1, 0):
            run_id = claim_search(self.repository, search, None)
            self.assertIsNotNone(run_id)
            asyncio.run(execute_search(self.repository, search, run_id, fetch))
            latest = self.repository.list_search_runs(1)[0]
            self.assertEqual(latest["status"], "Completed")
            self.assertEqual(latest["inserted"], expected_inserted)
            if expected_inserted:
                self.repository.enrich_from_detail("BA-1", posting("Agentur für Arbeit", "BA-1"))
                self.assertEqual(
                    self.repository.get_job("BA-1")["search_run_id"], run_id
                )

    def test_explicit_contract_is_not_published(self) -> None:
        item = posting("Agentur für Arbeit", "BA-1")
        item.employment_type = "Contract"
        self.repository.stage_new_search_results([item])
        self.repository.enrich_from_detail("BA-1", item)
        self.assertEqual(self.repository.get_processing_status(item.source, "BA-1"), "Filtered")
        self.assertEqual(self.repository.list_jobs(limit=10, offset=0), [])

    def test_review_can_link_second_source_without_new_tracker_job(self) -> None:
        first = posting("Agentur für Arbeit", "BA-1", "https://a.example/jobs/1")
        second = posting("Other Board", "OTHER-1", "https://b.example/jobs/2")
        for item in (first, second):
            self.repository.stage_new_search_results([item])
            self.repository.enrich_from_detail(item.source_job_id, item)
        result = self.repository.resolve_possible_duplicate(
            second.source, second.deduplication_key, link_existing=True
        )
        self.assertEqual(result["resolution"], "linked")
        self.assertEqual(len(self.repository.list_jobs(limit=10, offset=0)), 1)
        self.assertEqual(self.repository.get_processing_status(second.source, "OTHER-1"), "Merged")


if __name__ == "__main__":
    unittest.main()
