"""Background Airtable outbox worker shared by API and MCP entry points."""

from __future__ import annotations

import asyncio

import structlog

from job_scraper.integrations.airtable import AirtableClient, AirtableSettings
from job_scraper.storage.sqlite_jobs import SQLiteJobRepository

logger = structlog.get_logger("job-scraper-airtable-sync")


async def airtable_sync_worker(
    repository: SQLiteJobRepository, settings: AirtableSettings
) -> None:
    """Drain queued Airtable creates and patches until shutdown is requested."""
    client = AirtableClient(settings)
    logger.info("airtable_sync", action="worker_started")
    try:
        while True:
            try:
                item = repository.claim_next_airtable_sync()
                if item is None:
                    await asyncio.sleep(settings.poll_interval_seconds)
                    continue
                try:
                    record_id, action = await client.sync(item)
                    repository.mark_airtable_synced(
                        item.source,
                        item.deduplication_key,
                        record_id,
                        item.additional_fields,
                    )
                    logger.info(
                        "airtable_sync",
                        action=action,
                        deduplication_key=item.deduplication_key,
                    )
                except asyncio.CancelledError:
                    repository.mark_airtable_sync_failed(
                        item.source,
                        item.deduplication_key,
                        "Sync worker stopped during an Airtable request.",
                    )
                    raise
                except Exception as exc:
                    repository.mark_airtable_sync_failed(
                        item.source, item.deduplication_key, str(exc)
                    )
                    logger.warning(
                        "airtable_sync",
                        action="job_sync_failed",
                        deduplication_key=item.deduplication_key,
                    )
            except asyncio.CancelledError:
                break
            except Exception:
                logger.error("airtable_sync", action="worker_iteration_failed")
                await asyncio.sleep(settings.poll_interval_seconds)
    finally:
        await client.close()
        logger.info("airtable_sync", action="worker_stopped")
