"""Small async Airtable Web API client for idempotent job synchronization."""

from __future__ import annotations

import os
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from job_scraper.models.jobs import AirtableSyncWorkItem

DEFAULT_BASE_ID = "appGfIPFSvLA893Nc"
DEFAULT_TABLE_ID = "tblex7acMFKUi38RH"
_AIRTABLE_API_ROOT = "https://api.airtable.com/v0"
_SOURCE_FIELDS = {
    "Title",
    "Company",
    "Source",
    "Source Job ID",
    "Job URL",
    "Deduplication Key",
    "Job Description",
    "Location",
    "Work Mode",
    "Employment Type",
    "Application URL",
    "Posted At",
}


class AirtableSettings(BaseModel):
    """Validated Airtable connection settings loaded from the environment."""

    model_config = ConfigDict(frozen=True)

    token: SecretStr
    base_id: str = DEFAULT_BASE_ID
    table_id: str = DEFAULT_TABLE_ID
    timeout_seconds: float = 20.0
    poll_interval_seconds: int = Field(default=5, ge=1)

    @classmethod
    def from_environment(cls) -> AirtableSettings | None:
        token = os.getenv("AIRTABLE_TOKEN")
        if not token:
            return None
        return cls(
            token=SecretStr(token),
            base_id=os.getenv("AIRTABLE_BASE_ID", DEFAULT_BASE_ID),
            table_id=os.getenv("AIRTABLE_TABLE_ID", DEFAULT_TABLE_ID),
            poll_interval_seconds=int(os.getenv("AIRTABLE_SYNC_POLL_SECONDS", "5")),
        )


class AirtableSyncError(RuntimeError):
    """An Airtable request failed without including credentials in its message."""


class AirtableClient:
    def __init__(self, settings: AirtableSettings) -> None:
        self.settings = settings
        self._url = f"{_AIRTABLE_API_ROOT}/{settings.base_id}/{settings.table_id}"
        self._client = httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {settings.token.get_secret_value()}",
                "Content-Type": "application/json",
            },
            timeout=settings.timeout_seconds,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def sync(self, item: AirtableSyncWorkItem) -> tuple[str, str]:
        """Create a new row or update its existing counterpart without clobbering user fields."""
        full_fields = item.record.to_airtable_fields()
        source_fields = {
            key: value for key, value in full_fields.items() if key in _SOURCE_FIELDS
        }

        if item.airtable_record_id:
            try:
                record_id = await self._update(item.airtable_record_id, source_fields)
                return record_id, "updated"
            except AirtableSyncError as exc:
                if "HTTP 404" not in str(exc):
                    raise

        existing_ids = await self._find_record_ids(
            item.source, item.deduplication_key
        )
        if len(existing_ids) > 1:
            raise AirtableSyncError(
                "Multiple Airtable rows match the source and deduplication key."
            )
        if existing_ids:
            record_id = await self._update(existing_ids[0], source_fields)
            return record_id, "updated"

        record_id = await self._create(full_fields)
        return record_id, "created"

    async def _find_record_ids(self, source: str, key: str) -> list[str]:
        formula = (
            f"AND({{Source}}={_formula_string(source)}, "
            f"{{Deduplication Key}}={_formula_string(key)})"
        )
        response = await self._request(
            "GET", self._url, params={"filterByFormula": formula, "pageSize": 2}
        )
        return [str(record["id"]) for record in response.get("records", [])]

    async def _create(self, fields: dict[str, Any]) -> str:
        response = await self._request("POST", self._url, json={"fields": fields})
        return str(response["id"])

    async def _update(self, record_id: str, fields: dict[str, Any]) -> str:
        response = await self._request(
            "PATCH", f"{self._url}/{record_id}", json={"fields": fields}
        )
        return str(response["id"])

    async def _request(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = await self._client.request(method, url, **kwargs)
        except httpx.RequestError as exc:
            raise AirtableSyncError(f"Airtable network request failed ({type(exc).__name__}).") from exc
        if response.is_error:
            raise AirtableSyncError(f"Airtable returned HTTP {response.status_code}.")
        try:
            return response.json()
        except ValueError as exc:
            raise AirtableSyncError("Airtable returned an invalid JSON response.") from exc


def _formula_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
    return f"'{escaped}'"
