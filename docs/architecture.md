# Architecture and GCP resource inventory

Updated 2026-10-04. This is the current architecture record. `state.md` tracks implementation history and open work; `docs/research/` contains time-bound investigations. A proposed component is not a deployed resource.

## Decisions and current boundaries

| Area | Decision | Current state and reason |
| --- | --- | --- |
| Application | Python 3.12 with `uv`; FastAPI serves REST and Streamable HTTP MCP. | Implemented locally. One application owns the job workflow and exposes the same selected store to REST, MCP, and the scoring CLI. |
| Collection | The Agentur für Arbeit (BA) collector and ten staggered daily searches are the first source. Apify is a future integration, with no Actor selected or run. | Local collector and scheduler implemented. Search slots are 07:00–09:15 Europe/Berlin; missed slots are not backfilled after startup. Live unattended collection has not been run. |
| Job workflow | Python normalizes, deduplicates, filters, stores, and coordinates work. Detail retrieval, classification, and scoring are separate stages. | Implemented locally. Ambiguous cross-source duplicates wait for review. When enabled, classification claims one unclassified Pending job every 15 seconds, including imported backlog; scoring is user-triggered and limited to ten jobs per invocation by default. |
| Job database | SQLite for local debugging; Cloud SQL for PostgreSQL is the hosted job and application tracker. Select one with `JOB_SCRAPER_STORAGE`. | Both backends exist. Airtable was the former tracker and its sync worker no longer starts. A one-time SQLite import completed; there is no ongoing SQLite ↔ Cloud SQL synchronization. |
| AI | Gemini through Vertex AI classifies jobs and scores against one reviewed profile, recording model/version/cost provenance. | The selected local configuration uses the `eu` Vertex endpoint and `gemini-3.1-flash-lite`. `POST /vertex/smoke-test` makes a synthetic model call and returns token counts without touching jobs; it succeeded locally. Automated classification is enabled in the ignored local `.env`; the user observed completed classifications. |
| Private profiles | Versioned, user-reviewed profile JSON stays out of Git. A private Cloud Storage release is the source for the future hosted runtime. | Six objects are uploaded to the release prefix below. A read-only mount at `/app/.local/profiles` is planned; no service or mount exists. The broader versioned Markdown CV store is not provisioned. |
| Hosting | First deployment: one always-on Cloud Run service containing FastAPI/MCP, the scheduler, and workers, backed by Cloud SQL. | Selected direction, not deployed. Planned minimum and maximum instance count are both 1, with CPU available outside requests so background work can run. A separate VM or Cloud Scheduler is not part of this first deployment. |
| Identity and secrets | Use a dedicated runtime service account, IAM/ADC for Cloud SQL and Vertex AI, and Secret Manager for third-party credentials when those integrations are added. | Runtime identity, bucket read grant, and Cloud SQL IAM/database access exist. No Secret Manager secrets exist. Remote MCP authentication and Vertex AI runtime access are still open. |
| Delivery | Build and deploy the first container manually with `gcloud` after prerequisites are checked. | `Dockerfile` and `.gcloudignore` are present. No image, registry, build pipeline, GitHub Actions workflow, or deployment exists. CI/CD can follow a working first deployment. |
| Cost | Project-scoped €50 monthly budget with gross-spend alerts at 50%, 80%, and 100%. | Budget exists. Alerts do not stop charges. Cloud SQL is an accepted recurring cost; actual trial credit balance and expiry remain unverified. |

The planned hosted path is: BA collection → staging and detail enrichment → canonical Cloud SQL jobs → optional Vertex classification → triggered Vertex scoring → REST/MCP review and application updates. Gmail digests and Slack failure alerts are future integrations. All jobs and application updates remain in the selected database.

## GCP resource inventory

Snapshot verified with read-only `gcloud` commands on 2026-10-04 for project `jobsearch-danielmtz-2026` (project number `201142510726`). The active CLI account was `deatheater.dm@gmail.com`; commands used an explicit project because the machine's global default project is stale. This inventory lists provisioned app resources and relevant project configuration. Google-managed internal resources may appear in the console in addition to these entries.

| Resource | Identifier / location | Purpose and configuration |
| --- | --- | --- |
| GCP project | `jobsearch-danielmtz-2026` / `201142510726` | Dedicated application project; future hosting target is Frankfurt, `europe-west3`. |
| Linked billing account | `0138E2-3F8888-E1AF68` | EUR billing for the project. |
| Billing budget | `billingAccounts/0138E2-3F8888-E1AF68/budgets/76e89eb8-999d-423f-9bac-b2436190b8d0` | `Job Search Assistant monthly`; €50 per calendar month, restricted to this project, excluding credits in spend calculation, current-spend alerts at 50/80/100%. No hard cap. |
| Cloud SQL instance | `jobsearch-danielmtz-2026:europe-west3:jobsearch-postgres` | PostgreSQL 17 Enterprise, `db-f1-micro`, single zone `europe-west3-a`, 10 GB SSD, no storage auto resize, deletion protection on, public IP with encrypted-only connections, IAM database auth on. Daily backup window starts 02:00; seven backups and seven days of transaction logs are configured. |
| Cloud SQL databases | `jobsearch`; `postgres` | `jobsearch` is the app database with eight application tables. A one-time import copied and verified 714 jobs, 775 processing rows, 24 fit provenance rows, 714 aliases, and seven search runs. `postgres` is the default database. |
| Cloud SQL users | `deatheater.dm@gmail.com` (`CLOUD_IAM_USER`); `job-scraper-run@jobsearch-danielmtz-2026.iam` (`CLOUD_IAM_SERVICE_ACCOUNT`); `postgres` (built-in) | The human IAM user owns the eight app tables and has `USAGE, CREATE` on the `public` schema. The runtime user has `SELECT, INSERT, UPDATE, DELETE` on those eight existing tables; effective `CONNECT` on `jobsearch` and `USAGE` on `public` are present. It has no schema `CREATE` or table `TRUNCATE`. The one-time administrator password was rotated and discarded. |
| Cloud Storage bucket | `gs://jobsearch-danielmtz-2026-profiles` / `europe-west3` | Standard storage; uniform bucket-level access, enforced public access prevention, object versioning, and seven-day soft delete. Private reviewed scoring profiles only. |
| Cloud Storage objects | `releases/d91feaf2493767d2/current.json`; `releases/d91feaf2493767d2/{ai_engineer,applied_ai_fde,backend,platform_devops,swe}/v1.json` | Six uploaded objects, 52,385 bytes total. The release prefix is treated as immutable; a new reviewed set should receive a new prefix. |
| User-managed service account | `job-scraper-run@jobsearch-danielmtz-2026.iam.gserviceaccount.com` | Intended Cloud Run runtime identity. Granted `roles/storage.objectViewer` on the profile bucket and `roles/cloudsql.client` plus `roles/cloudsql.instanceUser` on the project. Not attached to a deployed service. |
| Cloud SQL service agent | `p201142510726-lbi0nt@gcp-sa-cloud-sql.iam.gserviceaccount.com` | Google-managed identity reported by the Cloud SQL instance. |

Enabled APIs at this snapshot: `aiplatform.googleapis.com`, `analyticshub.googleapis.com`, `apptopology.googleapis.com`, `bigquery.googleapis.com`, `bigqueryconnection.googleapis.com`, `bigquerydatapolicy.googleapis.com`, `bigquerydatatransfer.googleapis.com`, `bigquerymigration.googleapis.com`, `bigqueryreservation.googleapis.com`, `bigquerystorage.googleapis.com`, `billingbudgets.googleapis.com`, `cloudapis.googleapis.com`, `cloudtrace.googleapis.com`, `dataform.googleapis.com`, `dataplex.googleapis.com`, `datastore.googleapis.com`, `logging.googleapis.com`, `monitoring.googleapis.com`, `secretmanager.googleapis.com`, `servicemanagement.googleapis.com`, `serviceusage.googleapis.com`, `sql-component.googleapis.com`, `sqladmin.googleapis.com`, `storage-api.googleapis.com`, `storage-component.googleapis.com`, `storage.googleapis.com`, and `telemetry.googleapis.com`. An enabled API is not evidence that an application resource for that product was created.

**Absent as of this snapshot:** no Cloud Run service or job, Cloud Scheduler job, Artifact Registry repository, Cloud Build deployment, Secret Manager secret, or CV bucket. Cloud Run, Cloud Scheduler, and Artifact Registry APIs are not enabled. No profile bucket mount exists. The only user-managed service account returned by the project service-account listing is `job-scraper-run`.

## Before the first Cloud Run deployment

1. Cloud SQL runtime access is complete. On 2026-10-04, a keyless, short-lived impersonation test connected as the runtime database user, read 714 jobs, performed a zero-row update, and passed `PostgresJobRepository.initialize()`. The temporary impersonation grant and test-only IAM Credentials API enablement were removed afterward. Keep schema changes under the human migration identity; application startup now checks existing tables instead of creating them.
2. Verify container startup against Cloud SQL and mount `releases/d91feaf2493767d2/` read-only at `/app/.local/profiles`. Preserve the versioned prefix in the deployment configuration.
3. Make the in-process scheduler safe across restarts and review timezone/DST behavior. Keep one service instance while the scheduler runs in-process.
4. Choose and verify remote MCP/client authentication before exposing REST or MCP endpoints. Keep the service private until this works.
5. Verify the imported Cloud SQL data through the application. The one-time SQLite import is complete, but subsequent changes do not synchronize. Calibrate classification and scoring against real jobs before unattended model calls.
6. Enable the deployment APIs, build an image, deploy manually, then check health, logs, budget, and the runtime's database and profile access. No service launch is authorized by this documentation change.

When a GCP resource is created, changed, or deleted, update this inventory with its identifier, region, purpose, access scope, and current status. Record the corresponding milestone in `state.md`.
