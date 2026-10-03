# Project State

## Purpose

Build an agent-assisted job scraping and tracking application. The user wants to develop and deploy it on Google Cloud using Vertex AI and Gemini, with Python and `uv`. They want to design an MCP server, use Apify for scraping/collection, and use Airtable to track results.

This is the evolving implementation brief. Brainstorming is complete; the user has authorized a local-first start before cloud deployment.

## Current status

- Phase: local-first implementation; offline HTML import API and SQLite persistence are implemented.
- Repository: initialized; planning documents, a minimal `uv` app skeleton, local setup guide, and `.gitignore` are present.
- Application behavior: the manual Arbeitsagentur HTML parser, two-table SQLite flow, and Airtable sync worker are implemented. Search candidates are staged in `job_processing`; the detail worker publishes completed Airtable-shaped records to `jobs`; when `AIRTABLE_TOKEN` is configured, a second lifespan worker syncs those records to the existing Airtable `Jobs` table. Apify collection, scoring, and MCP workflow are not implemented yet. `main.py` remains `uv` starter boilerplate.
- Third-party dependencies: Beautiful Soup, FastAPI, Uvicorn, `python-multipart`, `pydantic`, `python-dotenv`, and `structlog` are declared in `pyproject.toml` and locked by `uv.lock`.
- Local environment: `.venv` and `uv.lock` are managed with `uv`; use the repository-local `.local/uv-cache` for the uv cache when syncing.
- GCP resources: none created or verified.
- External integrations: an Airtable token is configured in the ignored local `.env` file; the sync worker uses it without logging or displaying the value. Airtable writes occur when the API is started and a job is ready to sync; no live write was issued during this implementation. The user reports verbal developer authorization for BA detail retrieval; no Actor has been built or run for this source.
- Deployment: not started.
- Manual HTML processing: `job_scraper.sources.arbeitsagentur.html_parser` reads a local HTML file and emits normalized JSON or Airtable-shaped fields. It makes no network requests and does not invoke Apify. The user-provided Dresden search-results sample was parsed successfully (25 unique result cards), and extraction uses the BA result-card structure for title, employer, location, employment, work mode, and posted date. Clearly labelled external employer-posting links are extracted separately from BA `Job URL` and mapped to Airtable `Application URL`. A separately supplied Symate detail-page HTML was parsed and yielded the external listing URL `https://www.empfehlungsbund.de/job/303429`; search cards alone do not contain that link. Search cards can truncate titles and omit descriptions; a detail page may be needed for those fields too.
- Local API and storage: `job_scraper.api` exposes `/imports/search-results`, `/jobs/{source_job_id}/detail`, `GET /jobs`, `GET /jobs/{source_job_id}`, and `/health`, plus direct-fetch endpoints for search and detail pages. `job_scraper.storage.sqlite_jobs` now maintains two application tables: `job_processing` contains validated staging payloads and fetch/Airtable-sync workflow state; `jobs` mirrors the Airtable `Jobs` data fields and only contains records ready to sync. Existing mixed-table rows are migrated at startup; completed legacy rows populate `jobs` with Airtable sync pending. New final records default to Application Status `Saved` and Fit Status `Pending`. API request/response, parser posting, staging payload, and Airtable-shaped job models use Pydantic.
- Detail and Airtable sync workers: the API lifespan starts a local detail worker for queued Agentur für Arbeit jobs, using `_fetch_html_smart` and the parser path. Jobs transition through `Pending`, `Processing`, `ReadyToSync`, `Synced`, or `Failed` in `job_processing`, separately from the Airtable-shaped `jobs` record. Fetch failures retry up to three times; Airtable failures retry with exponential backoff up to eight attempts. Interrupted processing and sync claims return to retryable states on startup. Detail interval uses `JOB_SCRAPER_DETAIL_FETCH_INTERVAL` (default 60 seconds); Airtable poll interval uses `AIRTABLE_SYNC_POLL_SECONDS` (default 5 seconds). Existing Airtable record IDs are saved in `job_processing`; source+deduplication-key lookup avoids duplicate rows when no record ID is known. Updates to existing Airtable rows leave user-managed application status, notes, and scoring fields alone. The user reports verbal developer authorization for BA detail retrieval; no cloud resources were created.
- Logging: `job_scraper.logging_config` configures structured stdout logging using `structlog` and the standard-library bridge. Local output is `[ INFO ] : DD.MM.YYYY HH:MM:SS.mmm : [ event_name ] : key=value`, with green INFO, yellow WARNING, red ERROR, the date and values in the terminal's default color, and cyan event labels and keys; `ENV=production` selects JSON. Airtable worker logs include only `action` and, for job-specific events, `deduplication_key`. Sensitive-key fields are redacted and routine `httpx`/`httpcore`/Uvicorn access logs are reduced. API and background worker events use stable event names and structured fields.
- Local tooling checked: Python 3.12.3, `uv` 0.8.3, and `gcloud` 575.0.0 are installed. No `gcloud` identity/project or credentials have been checked.
- Reviewed the previous Airtable base `JobBoard Base` and, with the user's approval, created a unified `Jobs` table (`tblex7acMFKUi38RH`). Existing tables and records were left unchanged.
- Created the first manual-parser-derived Airtable row in the unified `Jobs` table: Symate GmbH's Fullstack Web Developer posting (`11956-3034291790978401-S`). It is `Saved`, `Pending` scoring, marked Hybrid from the posting's home-office option, and contains the BA detail URL plus the external Empfehlungsbund posting URL. No duplicate was found by deduplication key.
- The `Jobs` table contains core job, source/deduplication, role, application lifecycle, fit scoring/explanation, fit status, and run tracking fields. `Posted At` is its only dedicated date field, added by the user. Status-change dates (for example, Applied on or Rejected on) and follow-up details should be recorded as text in `Application Notes`; no separate first-seen, last-seen, status-date, or follow-up-date columns are wanted.

## Confirmed context

- Language: Python.
- Dependency/environment manager: `uv`.
- Intended cloud platform: Google Cloud Platform (GCP).
- AI platform/model family: Vertex AI and Gemini.
- Collection integration: Apify.
- Results tracking integration: Airtable.
- An MCP server is part of the desired design.
- The user wants help brainstorming, then developing and deploying the application on GCP.
- Searches should support both user-triggered and scheduled runs.
- Separate collection/persistence from AI scoring: first fetch jobs from Apify and write normalized, deduplicated records to Airtable; then score records with Gemini as a separate stage; finally sort/report scored results.
- Initial job source targets: LinkedIn, Indeed, Germany's Federal Employment Agency job search (`https://www.arbeitsagentur.de/jobsuche/`), and englishjobs.de. Automated collection from LinkedIn and the BA portal remains conditional on an authorized access path.
- Source priority under the USD 10/month constraint: (1) LinkedIn, (2) Agentur für Arbeit (Federal Employment Agency job search), (3) Indeed, (4) englishjobs.de.
- Gemini should extract job data, score fit, and explain the score.
- Present fit using both a numeric score and a qualitative category, with an explanation.
- In fit scoring, alignment with the user's listed skills matters more than an exact target-title match.
- Fit scoring must include at least two distinguishable dimensions: (1) skills/technology stack match, with Python weighted above FastAPI, LangGraph, and GCP; and (2) semantic similarity to the user's experience, including adjacent experience, using the base CV as evidence.
- Weight direct skills/stack match and semantic similarity/adjacent experience equally (50/50) in the overall fit score.
- Confirmed matching design: use the structured profile/database for direct skill and stack matching, and use the base CV as evidence for semantic similarity and adjacent-experience fit. The profile should be derived from the CV and reviewed/corrected by the user.
- The base CV will be provided as structured Markdown, avoiding PDF/DOCX decompilation or more complex document parsing.
- Store the base CV and matching profile in private GCP storage; keep Airtable focused on job and application tracking.
- Keep versioned CV artifacts in private GCP storage. Support role-specific CV variants when available; select the relevant variant and its derived structured profile for matching, and fall back to the main CV/profile pair when no specialized variant exists.
- If multiple versions exist for a role, use the newest version marked active.
- Explicitly tag each specialized CV with the role families it applies to; use those tags for deterministic variant selection.
- The MCP server is intended to be used by another agent.
- Intended MCP clients are ChatGPT or Codex sessions. Exact connection mechanism and client-side setup remain to be confirmed against the chosen hosting and supported MCP transport.
- The MCP server should be exposed as a remote endpoint hosted on GCP for ChatGPT/Codex sessions to connect to.
- MCP access is personal-only and should be restricted to the user's own ChatGPT/Codex sessions; no other people or agents should be granted access. Authentication must enforce that restriction rather than relying on endpoint secrecy.
- Airtable should track the broader application process, not only discovery and review.
- In planning discussions, ask one question at a time.
- Initial role targets, in priority order: AI Engineer, Forward Deployed Engineer, Applied AI Engineer, Software Engineer, Backend Engineer, DevOps Engineer, Full-Stack Engineer.
- Primary programming language: Python. Strong skills to match: FastAPI, LangGraph, and GCP. Familiarity to recognize: TypeScript and React.js.
- Exclude roles only when Java, Spring, or C# are stated as required/core technologies; incidental mentions do not disqualify a posting.
- Initial location/work-mode priorities, in order: (1) remote roles anywhere in Germany; (2) remote, hybrid, or on-site roles in Dresden; (3) hybrid or on-site roles anywhere in Germany; (4) roles in Poland, France, the UK, Switzerland, or the Netherlands (work-mode preference for these countries is not yet specified).
- Initial seniority priorities, in order: mid-level first, senior second, junior third.
- Work authorization: eligible to work in Germany; a non-EU citizen. Work authorization requirements/eligibility for other European countries are unknown and should not be assumed. For now, surface those postings without inferring eligibility.
- Language preference: English-language roles first; German-speaking roles are also acceptable.
- Employment type: permanent full-time or part-time roles only; exclude contract and freelance work.
- No compensation threshold is set.
- No additional role constraints are set.
- Scheduled search cadence: every day at 07:00 Europe/Berlin (timezone inferred from the current project context; confirm if a different timezone is intended).
- Process at most 100 unique results per scheduled run, deduplicating before applying the cap.
- Posting recency is a high-priority ranking dimension alongside role fit; the newest relevant postings should rank higher.
- Sort results by fit first, then recency; a newer posting should not outrank a materially better-fitting role solely because it is newer.
- Daily run results should be stored in Airtable and accompanied by a Gmail digest. Initial digest concept: concise summary of the run with links to Airtable; exact contents remain open.
- Send the Gmail digest only when the daily run finds new jobs; do not send zero-result digests.
- Send scheduled-run failure alerts to a private Slack channel. The exact Slack workspace/channel and message detail remain open. This is separate from the Gmail digest for successful runs with new jobs.
- Slack failure alert content should be a brief failure summary with a link to relevant logs; do not include detailed error text in the channel.
- Target total operating cost: at most USD 10 per month across Apify, Vertex AI/Gemini, GCP hosting/scheduling, and notifications, including after GCP trial credits expire. Initial GCP resources will be created using the GCP trial.
- Airtable should support an application lifecycle with these initial statuses: Saved, Applied, Interview, Offer, Rejected, Withdrawn, and Ignored. Ignored postings must remain recorded and be excluded from future surfaced results, even if found again by a later search.
- New matching postings should be written to Airtable automatically with status Saved by default.
- The user's ChatGPT/Codex sessions should be able to search/read job results and update job/application statuses in Airtable.
- The user's ChatGPT/Codex sessions should be able to edit application notes, including dated status history and follow-up details.

## Previous Airtable attempt (read-only reference)

- Base: `JobBoard Base`.
- Tables found: `LinkedIn` (100 records), `AI Engineer` (100 records), and `AI Engineer Skills Analysis` (103 records).
- The two job tables share fields for Title, Job Description, Job Date, Job Link, Job Match (integer), Status, Error Message, Notes, Location, and Apply Link.
- Existing job-status choices are `new`, `applied`, `interviewing`, `rejected`, and `error`. These differ from the confirmed lifecycle (`Saved`, `Applied`, `Interview`, `Offer`, `Rejected`, `Withdrawn`, `Ignored`); migration/mapping needs to be designed if reusing these tables.
- Jobs are separated into source/role tables rather than a unified canonical jobs table. For new collection across multiple sources and role families, prefer a unified `Jobs` table with source and target-role metadata to support cross-source deduplication, unless later schema review suggests a reason to reuse the separate tables.
- Current fields do not include a stable external source ID, explicit collection timestamp, fit category/explanation, separate skills and experience scores, or follow-up date. The existing integer `Job Match` field has no documented scale/meaning.
- One sampled LinkedIn row has `Job Date` = `1970-01-01`, a likely date-parsing fallback/error. Invalid or missing source dates should remain unknown rather than being coerced to an epoch date.
- `AI Engineer Skills Analysis` has normalized skill, classification, JD count/percentage, profile-evidence category, claim/evidence, and notes. This can inform the future structured career profile, but it is distinct from per-job fit scoring.

## Arbeitsagentur source access prerequisite

- Official BA Nutzungsbedingungen (section 2a(3), checked 2026-09-28) prohibit robots/web spiders and using existing interfaces contrary to the BA purpose to read portal content for collection and analysis. Section 4 allows account or portal access to be disabled for misuse. See `docs/research/arbeitsagentur-job-search-source.md` for research and sources.
- Official BA public pages document the human-facing Jobsuche and job-detail routes, but no official machine-readable job-search API contract for automated retrieval was found. A third-party/reverse-engineered API description does not establish authorization.
- Do not build or run an Arbeitsagentur Apify scraping Actor unless the BA provides an authorized retrieval API/access route or grants written permission. The user requested an Actor, but permission/access remains unresolved; no Actor code or requests were made.
- The separately implemented local parser only processes HTML the user manually supplies. It does not automate retrieval, and using it does not resolve the prerequisite for automated collection.

## Apify Actor candidate: LinkedIn

- Candidate identified by the user: [LinkedIn Jobs Scraper | Remove Duplicate Jobs | Pay Per Result](https://apify.com/cheap_scraper/linkedin-job-scraper), Actor ID `cheap_scraper/linkedin-job-scraper`. This is a candidate only; it has not been selected or run.
- The listing documents keyword and start-URL searches, job ID-based deduplication (`saveOnlyUniqueItems`), and structured job fields including title, company, location, job URL, description, and published date.
- Current listing pricing is $0.70 / 1,000 results on its free tier, down to $0.35 / 1,000 at the Gold tier, plus a stated run start fee (up to $0.02 at the default 4 GB memory). Pay-per-result runs require at least 150 results. The 150-result minimum exceeds the desired 100-result total daily cap, so output would need to be trimmed after collection and billed volume may exceed surfaced results.
- Some filters run after jobs are collected and billed. The listing also warns that LinkedIn's newer AI-powered search may ignore work type, job type, and seniority filters and instead rewrites the search query in natural language.
- The Actor is community-maintained. The listing displayed a 4.1 rating (41 reviews) and a modification about a month before this review; these signals can change.
- Policy concern remains unresolved: the Actor listing claims public-page scraping is legal, but LinkedIn's current User Agreement prohibits use of software or processes to scrape or copy LinkedIn services. An Actor's claim does not grant permission or override LinkedIn's terms. Do not run this Actor in production unless an authorized access path or permission is established.

## Unified Airtable table

- User approved creating a unified `Jobs` table in `JobBoard Base`; table ID: `tblex7acMFKUi38RH`.
- It uses `Title` as its primary field and includes company/source/source ID/job URL/deduplication key/description/location/work mode/employment type/seniority/role matches/application URL and lifecycle status/application notes/fit dimension scores/overall fit/category/explanation/fit status/search run ID.
- New rows are intended to start with Application Status `Saved` and Fit Status `Pending`.
- `Posted At` is the only dedicated date field. Record status history as dated text in `Application Notes` (for example, `Applied on: YYYY-MM-DD` or `Rejected on: YYYY-MM-DD`); use the same notes field for follow-up details. Do not add separate first-seen, last-seen, status-date, or follow-up-date columns.
- No job records were written and no source runs were started.

## Initial conceptual flow

The following is a starting hypothesis for discussion, not a settled architecture:

1. A user or daily 07:00 Europe/Berlin scheduled workflow supplies the saved search profile.
2. The collection stage invokes prioritized Apify actors/tasks and retrieves dataset items, within the 100 unique result cap.
3. Python normalizes and deduplicates items into canonical job records.
4. The first local vertical slice writes those records to Airtable with Saved status and a pending/unscored state, preserving existing application statuses.
5. A separate scoring stage uses Vertex AI/Gemini to extract/assess skill-stack fit and CV-based semantic/adjacent experience, then updates records with numeric/qualitative fit and evidence-based explanation.
6. Reporting sorts by fit first, then recency; a Gmail digest summarizes only newly found results. Failed runs send a brief alert to a private Slack channel with a log link.
7. A remote, personal-only GCP MCP endpoint lets the user's ChatGPT/Codex sessions call the same use cases to search/read jobs and update application statuses and notes, including dated status history and follow-up details.

### Confirmed CV matching approach

- Keep a base CV as the source evidence and derive a compact, structured career profile from it (skills/proficiency, roles, dates, projects, accomplishments, and evidence snippets). Have the user review/correct the extracted profile.
- Score explicit stack overlap against the structured profile, with Python carrying the greatest skill weight. Separately assess semantic similarity and adjacent experience by comparing job requirements with relevant experience evidence from the CV/profile.
- Have Gemini return dimension scores, a combined fit score/category, and a concise explanation grounded in specific CV evidence. Sort primarily by fit; use recency second.
- Start with a small profile and targeted evidence retrieval rather than committing to a managed vector database. Reassess retrieval/storage only if the CV or job history grows enough to justify it.
- Benefits: more consistent and compact scoring than resending the entire CV for every job while retaining evidence and nuance for adjacent-experience matches. Risks: CV-to-profile extraction can omit nuance or introduce errors, so keep provenance and user review in the loop.
- The structured profile and the original CV have distinct matching roles as described above; the CV remains source evidence while the profile supports direct comparisons.

## Open design questions for implementation planning

### Product and workflow

- Confirm Europe/Berlin as the scheduler timezone when configuring GCP Scheduler; it was inferred from project context.
- Current scope is discovery, ranking, and application tracking. Decide separately if application-material drafting is ever added.

### Agent and model responsibilities

- Select the Vertex AI model and validate pricing against the USD 10/month total ceiling.
- Tune subweights within the 50/50 dimensions; keep Python the highest-weight individual skill and skill overlap ahead of exact title match.
- Define qualitative score bands and examples.
- Specify handling for missing CV evidence, malformed model output, prompt injection in job descriptions, and retries.

### MCP design

- Verify remote MCP transport/authentication support for the user's ChatGPT and Codex clients at implementation time.
- Define authorization and confirmation behavior for Airtable writes and any future tool that triggers paid Apify runs.
- Restrict the endpoint to the user's own ChatGPT/Codex sessions.

### Integrations and data

- Evaluate the user-identified LinkedIn Actor candidate (`cheap_scraper/linkedin-job-scraper`) against cost, output, and source terms; it is not selected and has not been run. Establish an authorized LinkedIn access path before any run. Then select the first usable Apify Actor or saved Task and document its input/output contract.
- Do not automate BA collection without authorization; ask the BA whether a permitted machine retrieval API is available. Research actor coverage, terms, and costs for LinkedIn, Indeed, and englishjobs.de before integration.
- Finalize stable cross-source deduplication keys and how stale/removed postings are represented.
- Define the private GCP storage layout for versioned Markdown CVs and paired profiles, including role tags and active-version metadata.
- Confirm Slack channel and Gmail sender/recipient during setup; keep credentials and webhook URLs out of source control.

### GCP operations

- Confirm the GCP project, region, billing setup, and trial status before creating resources; enforce the USD 10/month ceiling after trial credits expire.
- Compare Cloud Run and Cloud Scheduler against Apify runtime needs and the cost target.
- Choose least-privilege identities, Secret Manager-backed credentials, log retention, and cost alerts.

## Working principles

- The user has moved the project from brainstorming into local-first implementation; keep implementation within the explicitly agreed scope and defer cloud deployment until separately authorized.
- Preserve distinctions between confirmed choices, hypotheses, and open questions.
- Prefer a narrow initial workflow and explicit human control over broad autonomous actions.
- Treat job posting text and scraped content as untrusted input.
- Make external calls, paid scraping runs, writes, and cloud deployments observable and bounded.
- Keep credentials out of the repository and use least privilege for deployed identities.

## Suggested next planning step

Implement the local Apify-to-Airtable vertical slice using a chosen first Actor, then add scoring and reporting as separate stages. Before live calls, obtain credentials and confirm the Actor input/output. The unified `Jobs` table is ready for schema integration; `Posted At` is the only dedicated date field, while dated status history and follow-up details belong in `Application Notes`. Keep all cloud deployment work later.
