# Job Scraper

A Python job discovery and application-tracking agent. The first milestone is local development; GCP resources and external integrations will be added only after the local workflow is shaped and credentials are configured deliberately.

## Local prerequisites

- Python 3.12
- [`uv`](https://docs.astral.sh/uv/)
- Git

The local HTML parser uses Beautiful Soup (`beautifulsoup4`), managed through `uv`.

## Local setup

```sh
uv sync --cache-dir .local/uv-cache
uv run --cache-dir .local/uv-cache python main.py
```

`uv sync` creates the project environment and lockfile. The repository-local cache path keeps setup self-contained. Keep any local secrets in `.env` and private profile/CV files under ignored local paths. Never commit credentials or personal CV data.

## Local FastAPI import service

The API can import saved HTML and includes direct fetch endpoints for Arbeitsagentur pages. Search results enter a SQLite processing queue; after detail enrichment, normalized job records are written into a separate Airtable-shaped SQLite table and synced to the existing Airtable `Jobs` table. SQLite is the local store at `.local/jobs.db` by default. Set `JOB_SCRAPER_DB_PATH` to use a different path. Set `JOB_SCRAPER_DETAIL_FETCH_INTERVAL` to change the detail worker interval in seconds (default 60).

Start the service on localhost:

```sh
uv run uvicorn job_scraper.api:app --host 127.0.0.1 --port 8000 --reload
```

Application logs use structured `structlog`: local console lines use `[ INFO ] : DD.MM.YYYY HH:MM:SS.mmm : [ event_name ] : key=value`, with green INFO, yellow WARNING, red ERROR, the date and values in the terminal's default color, and cyan event labels and keys; `ENV=production` selects JSON. Airtable worker logs include only `action` and, for job-specific events, `deduplication_key`. Logs go to stdout, redact fields whose names indicate credentials or tokens, and suppress routine HTTP client/access chatter.

Open `http://127.0.0.1:8000/docs` for the interactive API documentation. Import a saved search-results page with all its cards:

```sh
curl -F 'file=@.local/dresden-results.html' \
  -F 'source_url=https://www.arbeitsagentur.de/jobsuche/suche?suchbereich=jobs&wo=Dresden' \
  http://127.0.0.1:8000/imports/search-results
```

That local `curl` command uploads a file to your local API; it does not request the BA page. The API parses each card and upserts it into SQLite using the source/job ID deduplication key. New entries start with `Saved` application status and `Pending` fit status.

After importing a results page, the background worker queues its jobs for detail enrichment. You can also enrich one staged job with a manually saved detail page:

```sh
curl -F 'file=@.local/symate-detail.html' \
  -F 'source_url=https://www.arbeitsagentur.de/jobsuche/jobdetail/11956-3034291790978401-S' \
  http://127.0.0.1:8000/jobs/11956-3034291790978401-S/detail
```

Inspect completed local job rows at `GET /jobs` or `GET /jobs/{source_job_id}`. Re-importing updates known source fields without replacing application or fit statuses. SQLite has two application tables: `job_processing` stores a Pydantic-validated JSON payload and fetch/sync state; `jobs` mirrors the Airtable `Jobs` data fields and contains only publishable job records. Successful detail parsing moves a job into `jobs` and marks it `ReadyToSync`; the Airtable sync worker then creates or updates the matching record and marks it `Synced`. Airtable-managed application status, notes, and scoring fields are preserved when updating an existing row. Failed Airtable requests retry with exponential backoff, up to eight attempts.


## Local MCP server

The FastAPI service also hosts the MCP adapter at `http://127.0.0.1:8000/mcp` using Streamable HTTP. Start FastAPI with the command above, then register that URL with Codex:

```sh
codex mcp add job-scraper --url http://127.0.0.1:8000/mcp
codex mcp list
```

Keep the FastAPI process running while using the tools in Codex. Bind it to `127.0.0.1` for local use; the HTTP endpoint has no user authentication. The API and MCP tools use the same SQLite repository, private profile store, and Airtable sync worker in one process. An optional standalone stdio transport remains available for MCP hosts that launch their own server process:

```sh
uv run --cache-dir .local/uv-cache job-scraper-mcp
```

An MCP host should launch the stdio command with the repository as its working directory. Use `uv run --cache-dir .local/uv-cache mcp dev job_scraper/mcp/server.py` to inspect the registered tools during local development.

Available tools:

- `search_jobs` filters by text, source, and fit status. Use `fit_status="Pending"` to find jobs awaiting scoring.
- `list_scoring_profiles` lists the five private role profiles and their review status.
- `get_scoring_profile` returns one complete profile for review.
- `get_job_for_scoring` requires a `profile_id` and returns one fully rendered prompt containing the JD, selected profile, and rubric, plus their version metadata. It never reads the CV dossier at scoring time.
- `save_fit_assessment` requires current profile, rubric, and prompt versions. It validates the two 0–100 scores and category, calculates the 50/50 overall score, records local score provenance, and queues the Airtable update.
- `mark_fit_needs_review` records why the JD cannot be scored reliably and clears any earlier numeric score.
- `update_application_status` updates application status and optionally replaces notes (pass an empty string to clear them); the update is queued for the same retryable Airtable sync worker.

FastAPI starts one Airtable sync worker when `AIRTABLE_TOKEN` is configured. The standalone stdio server starts its own worker if run separately. Local Streamable HTTP is available now; remote hosting and personal-account authentication are deferred until the GCP deployment phase.

The versioned rubric and prompt are defined in `job_scraper/application/scoring_prompt.py`. Skill/stack fit assigns up to 70 points to required skills, 20 to preferred skills, and 10 to evidence of using the stack together. Experience fit assigns up to 60 points to direct responsibilities, 25 to comparable delivery and ownership, and 15 to transferable adjacent work. Overall categories are Strong (85–100), Good (70–<85), Stretch (50–<70), and Low (<50). If the JD lacks essential information, the assistant uses `mark_fit_needs_review` instead of inventing scores. The prompt tells the assistant to treat JD and profile content as data, cite evidence IDs, and report key gaps. The local server does not call Gemini automatically.

### Private career profiles

Five user-reviewed profiles live under the ignored `.local/profiles/` directory: `swe`, `applied_ai_fde`, `ai_engineer`, `backend`, and `platform_devops`. Each has a `v1.json` file; `current.json` selects one version per role. Their JSON documents share the Pydantic contract in `job_scraper/models/profiles.py`. Each skill refers to evidence embedded in the same profile. The evidence carries source references for human review, but scoring does not load those sources. Profile files are deliberately excluded from Git because they contain personal career information.

The private `.local/profiles/build.py` script compiles initial drafts from selected entries in the candidate dossier at `/Users/danielmtz/Data/cv/agentic-cv-creator`; it uses that repository's Python environment for PyYAML. Re-running it overwrites the reviewed v1 profiles, so preserve changes in a new version. New drafts require user review before changing their `status` to `reviewed`; scoring with a draft is blocked. Set `JOB_SCRAPER_PROFILE_DIR` to load profiles from another private directory. Validate all five with:

```sh
.venv/bin/python -c 'from pathlib import Path; from job_scraper.application.profiles import ProfileStore; print([(p.id, p.version, len(p.evidence)) for p in ProfileStore(Path(".local/profiles")).list()])'
```

## Implementation sequence

1. Define canonical job records and build the local Apify → normalize/deduplicate → Airtable vertical slice for one source.
2. Add unattended Gemini/Vertex AI scoring as a separate worker; the local MCP tools already accept assistant-generated scores during an interactive session.
3. Sort and report scored results; then add Gmail success digests and Slack failure alerts.
4. Calibrate the five reviewed career profiles and rubric against real JDs; add personal-account authentication before any remote deployment.
5. Verify actor/source costs and the $10/month ceiling, then plan GCP deployment separately.

See [AGENTS.md](AGENTS.md) for working guidance and [state.md](state.md) for the confirmed requirements and open implementation decisions.

## Manually parse saved Arbeitsagentur HTML

The offline parser accepts an HTML file that you provide (for example, a page saved from your browser). It does not open URLs, make network requests, or invoke Apify. It recognizes Schema.org `JobPosting` JSON-LD when present, then tries common result-card and detail-page patterns. It also extracts clearly labelled external job-posting links (such as “Externe Seite öffnen”) separately from the Arbeitsagentur detail URL and maps the external URL to Airtable's `Application URL`. The Arbeitsagentur result-page markup was calibrated against a manually supplied sample. Search cards can show shortened titles and may omit descriptions or external job links; use a saved detail page when you need those fields.

```sh
uv run python -m job_scraper.sources.arbeitsagentur.html_parser ./path/to/saved-page.html
```

If the saved page contains relative links, pass its original Arbeitsagentur URL so those links can be resolved locally:

```sh
uv run python -m job_scraper.sources.arbeitsagentur.html_parser ./path/to/saved-page.html \
  --source-url 'https://www.arbeitsagentur.de/jobsuche/jobdetail/EXAMPLE'
```

To print fields mapped to the existing Airtable `Jobs` schema:

```sh
uv run python -m job_scraper.sources.arbeitsagentur.html_parser ./path/to/saved-page.html --airtable-fields
```

The parser emits a JSON array. Missing values stay empty; relative dates are preserved as source text instead of being guessed into a calendar date. Keep personal or account-specific saved pages in an ignored local directory such as `.local/` and do not commit them.
