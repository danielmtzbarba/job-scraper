"""Check that an unreviewed profile produces an actionable MCP tool error."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from job_scraper.mcp.server import ServerContext, create_server


class _Jobs:
    def get_job(self, source_job_id: str) -> object:
        return object()


class _Profiles:
    def get(self, profile_id: str) -> SimpleNamespace:
        return SimpleNamespace(status="draft")


class DraftProfileToolTest(unittest.IsolatedAsyncioTestCase):
    async def test_draft_profile_explains_review_step(self) -> None:
        server = create_server(lambda: ServerContext(_Jobs(), _Profiles()))
        app = server.streamable_http_app(json_response=True)

        async with app.router.lifespan_context(app):
            async with httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=app),
                base_url="http://127.0.0.1:8000",
            ) as http:
                async with streamable_http_client(
                    "http://127.0.0.1:8000/mcp", http_client=http
                ) as (read, write):
                    async with ClientSession(read, write) as client:
                        await client.initialize()
                        result = await client.call_tool(
                            "get_job_for_scoring",
                            {"source_job_id": "fixture", "profile_id": "swe"},
                        )

        self.assertTrue(result.is_error)
        self.assertIn("swe is a draft", result.content[0].text)
        self.assertIn("get_scoring_profile", result.content[0].text)
