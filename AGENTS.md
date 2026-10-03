# Project Guidance

## Project purpose

This repository is for a Python job discovery and tracking application built around Google Cloud, Vertex AI, and Gemini. The intended design includes an MCP server, Apify for job-source collection, and Airtable as a results tracker. The user has concluded brainstorming and authorized local-first implementation. GCP resource creation and deployment are not yet authorized; establish and verify the local application before cloud work.

## How to work in this repository

- Read `state.md` before making project decisions. Keep it current when the user confirms a decision, changes scope, or completes a meaningful milestone.
- During planning discussions, ask the user one question at a time. Do not bundle multiple questions into one message.
- Treat ideas recorded as possibilities or open questions in `state.md` as unresolved. Do not present them as approved architecture.
- Use Python 3.12 for application code and `uv` for Python dependency and environment management, as requested by the user.
- Keep early designs small and explain trade-offs before introducing infrastructure, services, or abstractions.
- For cloud-facing work, establish the target GCP project, region, identity, budget, and deployment boundary before creating resources or deploying. Never assume the active `gcloud` account or project is the intended one.
- Keep secrets out of source control. Plan for Secret Manager or another explicitly selected secret store before integrating credentials.
- Prefer documented, reproducible local development and deployment steps. Update the appropriate docs when those workflows are established.
- Implement only the agreed scope, starting locally. Do not create GCP resources, deploy, or send data to external services until the user explicitly asks for that step and required project/account details are confirmed.
- Prefer local fixtures or dry-run behavior while integrations are being developed. Keep credentials and personal CV/profile artifacts out of source control.
- Do not send messages or publish data to external services without explicit authorization.

## Current system boundaries

- **Collection:** Apify is the planned source integration, prioritized LinkedIn, Agentur für Arbeit, Indeed, then englishjobs.de; validate actor availability, source terms, and cost before live runs.
- **Local workflow:** Python owns normalization, deduplication, hard filters, and orchestration; Gemini on Vertex AI extracts/scorers/explains where language understanding helps.
- **MCP interface:** remote GCP endpoint for the user's own ChatGPT/Codex sessions, with search/read and Airtable updates; verify transport/auth compatibility before implementation.
- **Persistence/tracking:** Airtable holds jobs and application state. Private GCP storage holds versioned Markdown CV artifacts and their paired structured profiles.
- **Hosting:** start locally. Cloud Run/Cloud Scheduler are candidates, not yet selected; no GCP resource creation or deployment until the user explicitly asks.

## Engineering expectations once implementation is authorized

- Validate external inputs and handle retries, rate limits, and partial failures at service boundaries.
- Make collection and Airtable writes idempotent where practical; retain source URLs and collection timestamps for traceability.
- Minimize stored personal data and respect source terms, access controls, and applicable policies.
- Separate pure logic from integrations so normalization, matching, and deduplication can be reasoned about independently.
- Develop in vertical slices and keep the first local workflow usable without live Apify, Airtable, Gmail, Slack, or GCP credentials.
- Before adding tests or running a test suite, follow the user's instructions for that task.
