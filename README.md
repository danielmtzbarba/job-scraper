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

The Airtable Personal Access Token is read from `AIRTABLE_TOKEN` in `.env` via `python-dotenv`. The base and table default to this project's existing `JobBoard Base` / `Jobs`; override them with `AIRTABLE_BASE_ID` and `AIRTABLE_TABLE_ID` if needed. `AIRTABLE_SYNC_POLL_SECONDS` controls how often the worker checks for ready records. `.env` is gitignored; never commit the token.

## Implementation sequence

1. Define canonical job records and build the local Apify → normalize/deduplicate → Airtable vertical slice for one source.
2. Add Gemini scoring as a separate stage that updates stored jobs with skill/stack and CV-based experience scores.
3. Sort and report scored results; then add Gmail success digests and Slack failure alerts.
4. Expose the same use cases to ChatGPT/Codex through MCP; add FastAPI only if REST endpoints are independently useful.
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
