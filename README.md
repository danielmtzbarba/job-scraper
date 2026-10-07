# Job Scraper

A Python job discovery and application-tracking agent. SQLite remains the local debugging store; Cloud SQL for PostgreSQL is the selected hosted store. The API and MCP tools use the selected store directly.

The [architecture and GCP resource inventory](docs/architecture.md) records the current deployment decisions, provisioned resources, and remaining prerequisites.

## Cloud Run service

The private `job-scraper` service is deployed in `europe-west3` at `https://job-scraper-201142510726.europe-west3.run.app`. Cloud Run IAM authentication is required. The service runs one always-on instance with Cloud SQL, the reviewed Cloud Storage profile release, scheduled BA collection, automatic classification, and automatic scoring. Runtime values are tracked in [`deploy/cloudrun.env.yaml`](deploy/cloudrun.env.yaml); this file contains no credentials. The ignored local `.env` is not uploaded.

To inspect the service from the confirmed `deatheater.dm@gmail.com` CLI account, run:

```sh
gcloud run services proxy job-scraper \
  --project=jobsearch-danielmtz-2026 --region=europe-west3
```

The first revision was deployed from this repository with `gcloud run deploy --source .`, using the Dockerfile, the tracked environment file, runtime service account `job-scraper-run@jobsearch-danielmtz-2026.iam.gserviceaccount.com`, an IAM-only ingress policy, 1 vCPU, 2 GiB memory, min/max one instance, unthrottled CPU, and a read-only Cloud Storage volume mounted at `/app/.local/profiles` with `only-dir=releases/d91feaf2493767d2`. Preserve these settings on subsequent revisions. The in-process scheduler still needs restart-safe catch-up and DST review, and remote MCP client authentication has not been configured.

## Local prerequisites

- Python 3.12
- [`uv`](https://docs.astral.sh/uv/)
- Git

The local HTML parser uses Beautiful Soup (`beautifulsoup4`), managed through `uv`.

## Local setup

```sh
uv sync --cache-dir .local/uv-cache
uv run --cache-dir .local/uv-cache playwright install chromium
uv run --cache-dir .local/uv-cache python main.py
```

`uv sync` creates the project environment and lockfile. Playwright installs Chromium separately in its browser cache so the BA collector can load every result page. The repository-local uv cache keeps Python dependency downloads under `.local`. Keep any local secrets in `.env` and private profile/CV files under ignored local paths. Never commit credentials or personal CV data.

### Cloud SQL backend

The GCP project has a single-zone PostgreSQL 17 `db-f1-micro` instance, `jobsearch-postgres`, in Frankfurt, with a 10 GB SSD, scheduled backups, encrypted connections, and storage auto growth disabled. The database is `jobsearch`. The checked-in schema is [`job_scraper/storage/postgres_schema.sql`](job_scraper/storage/postgres_schema.sql). The Cloud SQL Python connector uses Application Default Credentials and IAM database authentication, so no database password is stored in the app.

Set these variables in the ignored `.env` to use Cloud SQL instead of local SQLite:

```dotenv
JOB_SCRAPER_STORAGE=cloudsql
CLOUD_SQL_INSTANCE=jobsearch-danielmtz-2026:europe-west3:jobsearch-postgres
CLOUD_SQL_DATABASE=jobsearch
CLOUD_SQL_IAM_USER=deatheater.dm@gmail.com
```

The IAM user has `USAGE` and `CREATE` on the `public` schema. This narrow grant was applied by a one-time administrator login, then the administrator password was rotated and discarded:

```sql
GRANT USAGE, CREATE ON SCHEMA public TO "deatheater.dm@gmail.com";
```

All eight application tables and their indexes were created and verified. Starting the API or scoring CLI checks that the tables exist; schema creation stays with the table owner rather than the restricted runtime identity. The Cloud Run service account `job-scraper-run@jobsearch-danielmtz-2026.iam.gserviceaccount.com` uses database username `job-scraper-run@jobsearch-danielmtz-2026.iam` in `CLOUD_SQL_IAM_USER`. It has the Cloud SQL Client and Instance User IAM roles and `SELECT, INSERT, UPDATE, DELETE` on the eight existing application tables, without schema creation rights. A keyless connection and application initialization succeeded as that identity. On Python installations without a usable system CA bundle, put `SSL_CERT_FILE` in the ignored `.env` and set it to the absolute path printed by `.venv/bin/python -c 'import certifi; print(certifi.where())'`. The API and CLI load this setting before importing the Cloud SQL connector, which is necessary because its `aiohttp` dependency creates a TLS context at import time. Certificate verification stays enabled.

On 2026-10-04, the one-time import copied and verified 714 jobs, 775 processing records, 24 score provenance records, 714 source aliases, and 7 search runs from `.local/jobs.db`. It maps old `ReadyToSync`/`Synced` processing states to `Completed` and omits Airtable-only sync fields. The SQLite file remains intact. To preview or repeat the import after configuring the Cloud SQL variables above:

```sh
uv run --cache-dir .local/uv-cache job-scraper-import-sqlite
uv run --cache-dir .local/uv-cache job-scraper-import-sqlite --apply
```

The import writes in one transaction, verifies every copied row, and accepts an exact repeat as a no-op. It stops if Cloud SQL has different or partial data. This is a one-time import command; the API selects one backend per process and does not continuously synchronize SQLite with PostgreSQL.

## Local FastAPI import service

The API can import saved HTML and includes direct fetch endpoints for Arbeitsagentur pages. Search results enter `job_processing`; after detail enrichment, canonical records enter `jobs` in the same database. SQLite is the default at `.local/jobs.db`. Set `JOB_SCRAPER_DB_PATH` to use a different local path, or select Cloud SQL as described above. `JOB_SCRAPER_DETAIL_FETCH_INTERVAL` changes the detail worker interval in seconds (default 60). No Airtable worker runs.

Start the service on localhost:

```sh
uv run uvicorn job_scraper.api:app --host 127.0.0.1 --port 8000 --reload
```

Application logs use structured `structlog`: local console lines use `[ INFO ] : DD.MM.YYYY HH:MM:SS.mmm : [ event_name ] : key=value`, with matching colors for each level and event label (green INFO, yellow WARNING, red ERROR), a muted slate date, soft blue time and keys, and values in the terminal's default color; `ENV=production` selects JSON. Logs go to stdout, redact fields whose names indicate credentials or tokens, and suppress routine HTTP client/access chatter.

The MCP SDK's Streamable HTTP session messages use the same format. `mcp_session_created` and `mcp_session_terminated` mark session lifecycle events. `mcp_session_rejected` is a DEBUG event (hidden at the default INFO level): it means a client sent an unknown or expired session ID; the request receives HTTP 404 and the client must initialize a new session. Repeated rejections for one ID usually mean the client is still retrying a stale session after a restart or termination.

Open `/status` for the read-only daily workflow view. It shows today's ten scheduled searches in Europe/Berlin, marks a slot missed ten minutes after its scheduled time if no run was recorded, flags runs still in progress after 90 minutes, and summarizes the current detail, classification, and scoring queues. The Unapplied roles card counts jobs whose application status is `Saved`. `/api/status` returns the same data as JSON. The page reads the existing database; it does not start or retry work. The displayed worker failures are recent historical records and may include attempts that later succeeded. This route is currently implemented locally and requires a new deployment before it appears on Cloud Run.

Open `http://127.0.0.1:8000/docs` for the interactive API documentation. Import a saved search-results page with all its cards:

```sh
curl -F 'file=@.local/dresden-results.html' \
  -F 'source_url=https://www.arbeitsagentur.de/jobsuche/suche?suchbereich=jobs&wo=Dresden' \
  http://127.0.0.1:8000/imports/search-results
```

That local `curl` command uploads a file to your local API; it does not request the BA page. The API parses each card and inserts it only when the source/job ID deduplication key is new. New entries start with `Saved` application status and `Pending` fit status.

### Scored jobs UI prototype

The throwaway top-score table has three layout variants and reads scored jobs from Cloud SQL without starting collection or model workers. Run:

```sh
uv run --cache-dir .local/uv-cache python job_scraper/prototypes/serve_scored_jobs.py
```

Then open `http://127.0.0.1:8765/jobs`. Use the bottom switcher or left/right arrow keys to compare layouts. The table shows at most 50 scored jobs, sorted by score by default with posting date as the tie-breaker. Its Application column shows the saved application status for each job; this column has not yet been deployed to Cloud Run.

The deployed API serves the same read-only prototype. With the authenticated Cloud Run proxy above running on port 8088, open `http://127.0.0.1:8088/jobs`. The page loads scored jobs from `/api/jobs` through the same proxy. Existing API callers can still request JSON from `/jobs` by sending `Accept: application/json`.

After importing a results page, the background worker queues its jobs for detail enrichment. You can also enrich one staged job with a manually saved detail page:

```sh
curl -F 'file=@.local/symate-detail.html' \
  -F 'source_url=https://www.arbeitsagentur.de/jobsuche/jobdetail/11956-3034291790978401-S' \
  http://127.0.0.1:8000/jobs/11956-3034291790978401-S/detail
```

Inspect completed job rows at `GET /jobs` or `GET /jobs/{source_job_id}`. Re-importing a known search result leaves its existing source and application fields untouched. `job_processing` stores the validated staging payload and fetch state; `jobs` contains canonical job and application fields. Successful detail parsing writes `jobs` and marks processing `Completed`. Application status, notes, and scores are updated in the same database.


### Scheduled Agentur für Arbeit searches

While the local API is running, ten keyword searches start every day at 07:00, 07:15, ..., 09:15 Europe/Berlin. A search starts on time even if an earlier one is still running. The search definitions and their BA URLs are in `job_scraper/application/search_schedule.py`. Missed slots are not backfilled when the API starts later in the day.

Use `GET /searches` to inspect the schedule, `POST /searches/{search_id}/run` to start one search immediately, and `GET /search-runs` to inspect its outcome. Each search inserts only source posting IDs that are new to the selected database; known postings are ignored without recording another appearance. There is no daily result cap. New BA postings enter the detail worker. Explicit contract and freelance postings are filtered after detail parsing.

The BA search collector uses Chromium to load all results behind “Weitere Ergebnisse”, then parses the rendered HTML. It sorts by newest publication. The sort parameter and `Deutschland (Land)` location were checked against BA's rendered search UI; a read-only local browser check parsed all 206 results for one Python Entwickler query. Search runs fail rather than silently truncating results if pagination cannot complete. Run outcomes are stored in the selected database.

For future sources, `source_aliases` links a source posting to one canonical tracker job. Automatic linking requires a shared direct application URL, matching employer and title, and closely matching full descriptions. Similar postings with uncertain identity are held for review. Inspect every possible match with `GET /duplicate-reviews`, then use `POST /duplicate-reviews/{source}/{deduplication_key}/resolve` with `{"decision":"link_existing","possible_source":"Agentur für Arbeit","possible_key":"arbeitsagentur:BA-123"}` or `{"decision":"keep_separate"}`. Specify the match identity when more than one is listed. Source and key path segments must be URL-encoded. Search-result uploads and direct fetches are insert-only; detail imports enrich only pending postings.

## Local MCP server

The FastAPI service also hosts the MCP adapter at `http://127.0.0.1:8000/mcp` using Streamable HTTP. Start FastAPI with the command above, then register that URL with Codex:

```sh
codex mcp add job-scraper --url http://127.0.0.1:8000/mcp
codex mcp list
```

Keep the FastAPI process running while using the tools in Codex. Bind it to `127.0.0.1` for local use; the HTTP endpoint has no user authentication. The API and MCP tools use the same selected database and private profile store in one process. An optional standalone stdio transport remains available for MCP hosts that launch their own server process:

```sh
uv run --cache-dir .local/uv-cache job-scraper-mcp
```

An MCP host should launch the stdio command with the repository as its working directory. Use `uv run --cache-dir .local/uv-cache mcp dev job_scraper/mcp/server.py` to inspect the registered tools during local development.

Available tools:

- `search_jobs` filters by text, source, and fit status. Use `fit_status="Pending"` to find jobs awaiting scoring.
- `list_scoring_profiles` lists the five private role profiles and their review status.
- `get_scoring_profile` returns one complete profile for review.
- `get_job_for_scoring` requires a `profile_id` and returns one fully rendered prompt containing the JD, selected profile, and rubric, plus their version metadata. It never reads the CV dossier at scoring time.
- `save_fit_assessment` requires current profile, rubric, and prompt versions. It validates the two 0–100 scores and category, calculates the 50/50 overall score, and records score provenance.
- `mark_fit_needs_review` records why the JD cannot be scored reliably and clears any earlier numeric score.
- `update_application_status` updates application status and optionally replaces notes (pass an empty string to clear them).

The deployed service hosts Streamable HTTP at `/mcp` behind Cloud Run IAM authentication. A remote Codex/ChatGPT client authentication path has not yet been configured; the localhost URL above remains for local development.

The versioned rubric and prompt are defined in `job_scraper/application/scoring_prompt.py`. Rubric/prompt v2.1.0 gives direct credit for demonstrated engineering capabilities on functionally equivalent tools, reserving a smaller deduction for required platform-specific operation. A named-tool gap is not deducted again from semantic experience. Language and education are excluded from both fit scores; the user's German-language and education/degree/grade requirements are treated as met and are not reported as gaps. The 70/20/10 skill and 60/25/15 experience subweights, 50/50 overall average, and category boundaries remain the same. Rubrics v1.0.0 and v2.0.0 remain in the source for comparison; existing scores are not changed automatically. If the JD lacks essential information, the assistant uses `mark_fit_needs_review` instead of inventing scores. The prompt treats JD and profile content as data, cites evidence IDs, and reports key gaps.

### Classification after enrichment

Once detail enrichment saves a canonical job, it queues classification. An external application link is optional. Filtered jobs, merged duplicates, and unresolved possible duplicates are not queued. When `JOB_SCRAPER_AUTO_CLASSIFY=1`, the FastAPI process starts a separate worker thread. Every 15 seconds, it claims at most one unclassified `Pending` canonical job, newest posting first, and calls Vertex AI without holding a request or database transaction open. Jobs imported before the worker was enabled are eligible even if they have no `job_classifications` row; the claim creates that row atomically. It stores the selected profile, version, reason, and evaluation run. A match becomes ready for scoring while keeping Fit Status `Pending`; a clear nonmatch becomes `OutOfScope` and skips scoring. Failed calls retry up to three claims with delays. The flag defaults to off, so no classification calls begin merely because the code changed.

Set the project, location, model, `JOB_SCRAPER_INPUT_PRICE_PER_MILLION`, and `JOB_SCRAPER_OUTPUT_PRICE_PER_MILLION` in the ignored `.env` before enabling the worker. The server does not expose a classification trigger through REST or MCP.

### Background and triggered Vertex AI scoring

Set `JOB_SCRAPER_AUTO_SCORE=1` to start a scoring worker in the FastAPI process. Every 15 seconds it claims at most one classified job whose Fit Status is still `Pending`, ordered newest posting first, and scores it with the saved reviewed profile. It uses the same model, project, location, and input/output token prices configured for classification. The worker logs `scoring_completed` with both dimension scores, overall score/category (or review outcome), token counts, evaluation ID, and estimated USD cost. Gemini calls remain off unless this flag is enabled. If both workers are enabled, classification and scoring run independently; only classified jobs are eligible for scoring.

The scoring CLI is also available. Preview eligible jobs without making model calls:

```sh
uv run --cache-dir .local/uv-cache job-scraper-score --dry-run
```

A live run requires the intended GCP project, model, location, Application Default Credentials, enabled Vertex AI access, and current input/output prices for that exact model and location. Put `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION`, and `JOB_SCRAPER_VERTEX_MODEL` in the ignored `.env`, then trigger at most ten newest classified, ready-for-scoring jobs:

```sh
uv run --cache-dir .local/uv-cache job-scraper-score \
  --input-price-per-million INPUT_USD \
  --output-price-per-million OUTPUT_USD
```

Replace `INPUT_USD` and `OUTPUT_USD` with numeric USD per million token rates from the selected model's [current Google Cloud price sheet](https://cloud.google.com/gemini-enterprise-agent-platform/generative-ai/pricing). `--limit` may lower the maximum from ten. The CLI uses the saved classification and does not classify again; if the selected profile has a newer reviewed version, it queues reclassification instead of scoring with stale evidence. Unclear JDs become `NeedsReview`. The SDK retries transient 408/429/5xx responses up to three HTTP attempts; failed scoring jobs remain Pending for a later triggered run. Existing scored jobs are never selected automatically.

The GCP project is `jobsearch-danielmtz-2026` under `deatheater.dm@gmail.com`. Local `.env` and the deployed service select the `eu` Vertex endpoint and `gemini-3.1-flash-lite`; the database and Cloud Run service use Frankfurt (`europe-west3`). A synthetic SDK request and one exploratory classification succeeded on 2026-10-04. Local Application Default Credentials were refreshed for the intended account and project with `gcloud auth application-default login deatheater.dm@gmail.com --project=jobsearch-danielmtz-2026`; this replaces the machine's previous ADC configuration. The account's actual trial credit balance and expiration must be checked in Cloud Billing. The project has a recurring €50 budget with 50%, 80%, and 100% alerts on gross usage before credits; alerts do not stop spending. For the model's EU Standard PayGo text rate checked on 2026-10-07, use `--input-price-per-million 0.275 --output-price-per-million 1.65`, then recheck current pricing before later runs. See [Vertex AI pricing and trial research](docs/research/vertex-ai-trial-pricing.md).

The selected database's `evaluation_runs` table records every classification and scoring request, including failures, model/project/location, prompt and rubric versions, profile and version for scoring, response metadata, token counts, price rates, and estimated cost. `fit_assessment_provenance.evaluation_run_id` links the current automated score to its request; older manual scores have no model run. The cost estimate uses prompt tokens plus candidate and thinking tokens at the supplied rates, so it is an estimate rather than a billing statement. A batch reports calls whose cost could not be estimated separately. The CLI does not start an Airtable worker or create GCP resources.

### Private career profiles

Five user-reviewed profiles live under the ignored `.local/profiles/` directory: `swe`, `applied_ai_fde`, `ai_engineer`, `backend`, and `platform_devops`. Each has a `v1.json` file; `current.json` selects one version per role. Their JSON documents share the Pydantic contract in `job_scraper/models/profiles.py`. Each skill refers to evidence embedded in the same profile. The evidence carries source references for human review, but scoring does not load those sources. Profile files are deliberately excluded from Git because they contain personal career information.

The private `.local/profiles/build.py` script compiles initial drafts from selected entries in the candidate dossier at `/Users/danielmtz/Data/cv/agentic-cv-creator`; it uses that repository's Python environment for PyYAML. Re-running it overwrites the reviewed v1 profiles, so preserve changes in a new version. New drafts require user review before changing their `status` to `reviewed`; scoring with a draft is blocked. Set `JOB_SCRAPER_PROFILE_DIR` to load profiles from another private directory. Validate all five with:

```sh
.venv/bin/python -c 'from pathlib import Path; from job_scraper.application.profiles import ProfileStore; print([(p.id, p.version, len(p.evidence)) for p in ProfileStore(Path(".local/profiles")).list()])'
```

The first reviewed set is also stored privately in the Frankfurt bucket `gs://jobsearch-danielmtz-2026-profiles` under `releases/d91feaf2493767d2/`. That prefix contains only `current.json` and the five role `v1.json` files. The bucket enforces public access prevention and uniform bucket-level IAM, with object versioning enabled. The Cloud Run service account `job-scraper-run@jobsearch-danielmtz-2026.iam.gserviceaccount.com` has read-only object access on this bucket. The service mounts this release prefix read-only at `/app/.local/profiles`; the existing `JOB_SCRAPER_PROFILE_DIR` default then works without changing profile-loading code. Publish a new immutable release prefix for reviewed updates and point a new Cloud Run revision at it.

## Implementation sequence

1. Build the canonical job workflow in SQLite, then use the same API/MCP operations with Cloud SQL for hosted persistence.
2. Calibrate the triggered Gemini/Vertex AI classifier and scorer against real JDs before considering unattended scoring. The local MCP tools continue to accept assistant-generated scores during interactive sessions.
3. Sort and report scored results; then add Gmail success digests and Slack failure alerts.
4. Calibrate the five reviewed career profiles and rubric against real JDs; add personal-account authentication before any remote deployment.
5. Verify actor/source costs and monitor the €50/month alert budget. Review the first Cloud Run worker and scheduled collection results, then resolve the remaining runtime items in the architecture record.

See [AGENTS.md](AGENTS.md) for working guidance and [state.md](state.md) for the confirmed requirements and open implementation decisions.

## Manually parse saved Arbeitsagentur HTML

The offline parser accepts an HTML file that you provide (for example, a page saved from your browser). It does not open URLs, make network requests, or invoke Apify. It recognizes Schema.org `JobPosting` JSON-LD when present, then tries common result-card and detail-page patterns. It also extracts clearly labelled external job-posting links (such as “Externe Seite öffnen”) separately from the Arbeitsagentur detail URL and stores that URL as `application_url`. The Arbeitsagentur result-page markup was calibrated against a manually supplied sample. Search cards can show shortened titles and may omit descriptions or external job links; use a saved detail page when you need those fields.

```sh
uv run python -m job_scraper.sources.arbeitsagentur.html_parser ./path/to/saved-page.html
```

If the saved page contains relative links, pass its original Arbeitsagentur URL so those links can be resolved locally:

```sh
uv run python -m job_scraper.sources.arbeitsagentur.html_parser ./path/to/saved-page.html \
  --source-url 'https://www.arbeitsagentur.de/jobsuche/jobdetail/EXAMPLE'
```

To print the parser's legacy Airtable-shaped field mapping:

```sh
uv run python -m job_scraper.sources.arbeitsagentur.html_parser ./path/to/saved-page.html --airtable-fields
```

The parser emits a JSON array. Missing values stay empty; relative dates are preserved as source text instead of being guessed into a calendar date. Keep personal or account-specific saved pages in an ignored local directory such as `.local/` and do not commit them.
