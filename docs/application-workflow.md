# Job application workflow

Status: the original review route and attempt table are deployed. The LLM-guided redesign, applications index, and submitted-history migration are local changes only. Cloud Run attempt creation remains disabled until persistent private artifact storage is selected. No real application has been submitted with this workflow.

## Goal

Start from a scored job row, use its saved classification to choose the prepared CV variant, inspect the live application form, and prepare a user-reviewable application. The same application module should be callable from the local MCP server and from a future Vertex AI / Google ADK agent. The agent is an orchestrator; the shared module owns application state and deterministic safety gates.

## Sequence

1. **Select jobs.** The user selects one or more scored rows (the initial discovery batch may be 10–20). Verify the stored fit result, posting metadata, and application URL. Do not infer the role from fit score.
2. **Check classification.** Load the persisted classification result and its profile/version. Stop and ask the user if classification is absent, stale, conflicting, or unclear. Do not substitute `role_matches` or infer a CV from the score.
3. **Create an attempt.** Insert small process metadata in `application_attempts` and create a private per-job attempt under `.local/application/attempts/<attempt-id>/`. The database stores no form answers or screenshots. The private artifacts are retained only until the user explicitly marks the attempt **Submitted** or **Discarded**.
4. **Inspect the form.** Navigate to the application URL and discover all visible and conditional steps before calling the form ready. Record each question, whether required, field type/options, and the page/step where it appeared. Do not invent answers. If a question or its meaning is unclear, pause and ask the user; retain the attempt while waiting.
5. **Prepare answers.** Read reusable, user-approved facts from `.local/application/answers.yaml`. Apply only facts whose meaning matches the form field. Record per-field provenance (fact key or user response) and confidence in the attempt. Leave unknowns unanswered and ask. Employer-specific choices (including talent-pool consent) are not reusable facts.
6. **Choose and stage the CV.** Resolve the classified profile to a prepared PDF using `.local/application/cv-map.json`. Verify the file exists and show its variant and filename in the review. Never upload a different variant silently. Missing or ambiguous mapping pauses the attempt.
7. **Fill, but do not submit.** Fill fields and attach the selected PDF. For the first pilot, capture the completed form and a readable list of every answer, selected option, consent, and uploaded filename. Present that review to the user.
8. **Wait for explicit final approval.** Do not click a final submit control until the user explicitly approves this specific application after reviewing the captured form. A general preference or earlier approval of facts is not submit approval. If the user requests a change, revise and show the updated review.
9. **Submit and verify.** After approval, persist the approved digest and `Submitting` state before the browser click. Submit once, inspect the confirmation/result, and update the job's application status only when submission is confirmed. The durable job outcome is `jobs.application_status = Applied`. The minimal submitted history row retains the attempt ID, job identity, selected profile/CV variant, timestamps, and `Submitted` state; it contains no form answers, captures, or review digest.
10. **Close and erase the attempt.** On explicit **Submitted** or **Discarded**, delete that attempt's temporary files and browser session. A submitted attempt keeps only its redacted process row; a discarded attempt deletes its row. A timeout, browser failure, unclear answer, or waiting state is not an explicit discard and does not authorize deletion. If submission fails, ask whether to retry or discard; keep the attempt until the user decides.

## Retention and privacy

- Reusable approved facts live in the ignored private `.local/application/answers.yaml` and grow only as the user approves additional answers. Keep fact source, approval status/date, and any field-specific limits with each fact.
- Per-application data is isolated under `.local/application/attempts/<attempt-id>/` and retained only until the user explicitly marks the application submitted or discarded. Deletion includes screenshots, downloaded/captured page content, draft answers, temporary uploads, and browser/session artifacts owned by the attempt.
- Keep only the minimum durable submission/tracking record after successful submission; do not retain a copy of the completed form or its screenshot.
- Never put personal facts, contact details, CVs, screenshots, or application payloads in tracked docs, logs, model prompts beyond the active task's needs, or source control.
- Optional talent-pool membership defaults on in the future general workflow, but the pilot requires explicit employer-specific consent before selecting it. Group-sharing consent is separate and has no default yet. Final submission still requires the pilot's explicit review approval.

## Private files and CV source

All paths below are local-only and covered by `.gitignore`'s `.local/` rule:

- `.local/application/answers.yaml` — progressive user-approved answer facts.
- `.local/application/cv-map.json` — mapping from saved classification/profile identifiers to prepared PDF paths.
- `.local/application/cvs/` — prepared, compiled PDF variants to upload. No compiled PDFs were found in the project's existing `.local/` files during this design pass; the user's current source directory is not yet known. Copy or reference the already-prepared PDFs here only after their location is supplied. Do not regenerate them as part of the pilot.
- `.local/application/attempts/` — temporary per-application records, subject to the explicit submit/discard deletion rule above.

The map should point to the existing classified profile/CV variant; it must not be built from fit score. If a profile maps to more than one plausible CV, ask before proceeding.

## Shared module boundary (MCP and ADK)

The shared Python implementation is `job_scraper.application.application_workflow.ApplicationWorkflow`; MCP tools adapt its readiness, attempt, inspect, answer, review, submit, and discard operations. MCP tools and the optional ADK adapter call the same module so classification checks, consent gates, retention, and submit confirmation cannot diverge by client. Playwright is the browser driver. The guided path can advance through multiple form steps. The older selector path is available only to attempts created before the redesign. The guided code is deployed to Cloud Run but remains gated there by missing durable private artifact storage; no real employer application has been run.

The implementation records active process state, profile and CV variant, review version and digest, and approval timestamps in `application_attempts`. SQLite creates this table locally; [the migration](../deploy/migrations/2026-10-09-application-attempts.sql) was applied to Cloud SQL on 2026-10-09. The table enforces one active attempt per job. The review page and MCP tools call the same module. An explicit approval from the review page is persisted for the exact digest; submission atomically claims that digest before the browser click. A possible click without verified confirmation leaves `SubmissionUnverified`, which cannot be retried or edited. Confirming submission sets the job outcome to `Applied` and redacts the attempt row into minimal submitted history; discard deletes the row without changing the job outcome. Both closure paths erase the private artifacts.

The general MCP `update_application_status` tool refuses `Applied`; that transition goes through the shared workflow's confirmation path. The `/jobs` Application column remains the job outcome and offers state-specific actions, with a visible blocker for a missing or unclear classification, missing URL, or remote artifact storage. `/applications` lists active, review-needed, unverified, and submitted rows with filters. The review page shows the captured form, exact proposed answers, CV filename, employer-specific consent choices, and explicit submit/discard actions. Responses containing attempt data have `Cache-Control: no-store`.

MCP tools currently include `check_application_readiness`, `start_application_attempt`, `inspect_application_form`, `get_application_facts`, `add_approved_application_fact`, `save_application_answers`, `prepare_application_review`, `submit_application`, `confirm_application_submitted`, and `discard_application_attempt`. The caller must use only approved facts and ask the user about unclear fields and employer-specific consent. `add_approved_application_fact` appends a new key and will not overwrite an existing fact. Selecting a talent pool or group-sharing option requires a separate approved selector in the pilot.

New attempts use the guided browser path. The service observes visible controls and page text, filters cookie chrome, captures a screenshot, asks the existing Vertex Gemini client for one structured decision, validates the target against the current observation, executes one bounded Playwright action, and observes again. A live browser session is held in the application process; a lost session after navigation fails closed. The existing selector-based path is retained only for attempts created before this redesign. `application/adk_tools.py` exposes the same operations as optional Google ADK function tools; MCP uses its existing adapter. No second workflow was added. No live employer form has been exercised in this implementation pass. Cloud Run attempts fail closed because `.local` artifacts would be lost on instance replacement. Before remote use, select persistent private artifact storage and map the prepared PDFs. The guided path passed an in-memory mock form; a real pilot still needs persistent private artifacts and mapped PDFs.


## Guided tool contract and policy

- `observe_application_page` returns a current observation ID, URL, title, clipped visible text, typed control IDs/labels/options, and a private screenshot. Browser action target IDs are valid only for that observation.
- `guide_application_step` asks Vertex Gemini for `page_kind`, typed application questions with confidence/provenance, one `click`/`fill`/`select`/`scroll`/`pause` action, and an explanation or question for the user. The page is untrusted data. The model receives only current page details and fact values whose keys match current control labels; it never receives the CV binary. The service rejects invalid model output or unknown targets.
- `act_on_application_page` accepts only a typed action on the current observation. Fill/select values must come from approved facts or a user-saved answer. Final submit, navigation links, and cookie controls are excluded. The service records the next observation after every action.
- Cookie chrome is excluded from application questions. The browser first rejects optional cookies. It accepts all only when a visible modal cookie dialog still blocks the page after rejection. The chosen cookie policy is stored as a short action name in private attempt metadata. Employer-specific consent is separate: a selected choice needs explicit approval for the observed control and is checked again at review.
- Guided review uses the live browser page, checks required visible fields and employer consent, uploads the mapped PDF, and binds the captured answers and PDF hash to a review digest. Submission uses that same page and atomically claims the digest after an explicit review-page approval before one click. A lost session or changed form fails closed; a click with uncertain confirmation becomes `SubmissionUnverified` and cannot be retried automatically.
- The app uses the existing `google-genai` Vertex client. The optional `create_adk_agent` adapter registers plain Python functions with Google ADK when that package is installed; the default runtime does not require ADK. Submit still requires a review-page approval persisted for the exact digest, so an MCP or ADK model argument alone cannot authorize the click. Local deployment requires the configured Vertex project, region, and model for guided steps.

## Deployment and validation boundary

The PostgreSQL migration `deploy/migrations/2026-10-09-application-submitted-history.sql` was applied to the selected Cloud SQL database on 2026-10-09. Private Cloud Run revision `job-scraper-00004-gn2` serves the new code, but rejects attempt creation until persistent private artifact storage is approved and configured. A local Chromium integration test uses an in-memory two-step form, fixture answers, and a fixture PDF. It verifies observation/action IDs, review approval, one submit click, job outcome, and temporary-data erasure; it never contacts an employer. Run it with `JOB_SCRAPER_BROWSER_TEST=1 uv run python -m unittest tests.test_application_browser_integration` in an environment that allows Chromium. The ordinary suite skips this optional browser test.

## Application audit pilot

The local application records allowlisted, content-free events in `application_audit_events`. Events identify the attempt, timestamp, event/outcome, channel (`web`, `mcp`, `adk`, or `internal`), and request correlation ID where available. Bounded fields include state transition, action kind/control ID, review version, reason code, model ID, and token counts. The schema rejects arbitrary answer, URL, prompt, page, CV, or exception payloads. The event table has no foreign key to the temporary attempt row, so discarding an attempt retains its timeline. Submitted and discarded attempts remain visible in `/applications`; the attempt page reads `/api/applications/{attempt_id}/audit` to show the history without private content. Operational logs contain only a pointer to the committed event ID and the same safe identifiers.

Attempt creation, state transitions, review approval, final submission confirmation, and discard write their audit event in the same database transaction as the state mutation. Page observations, browser actions, model decisions, answer saves, consent approvals, and artifact erasure write separate bounded events. A `started` browser action is persisted before execution; a failed or uncertain action records its outcome without exception text. The final submit claim is persisted before the employer click. A crash after a browser side effect can leave a `started` event without a completion event, which must be treated as unresolved; the application already prevents automatic resubmission after an uncertain submit.

This is a pilot audit trail, not a tamper-proof ledger. The channel is known, but an authenticated human identity is not yet attached. Local database administrators can modify rows. The standalone MCP process does not supply an HTTP request correlation ID. Reusable fact-sheet changes are outside per-attempt audit scope. Retention duration, hosted read access, backups, and alerting remain to be set before remote use. Model Armor and Sensitive Data Protection are deferred. The Cloud SQL migration `deploy/migrations/2026-10-09-application-audit-events.sql` was applied on 2026-10-09. The runtime database user has `SELECT` and `INSERT` on the audit table; it cannot update or delete audit rows.
Existing attempts are not backfilled with invented events; their timelines begin when the new code first acts on them. A failed private-artifact erase produces a failed audit event and requires local cleanup.

## Questions for later design

- Where are the prepared PDF variants currently stored, and what are their exact filenames/profile identifiers?
- What browser-control implementation will be available to both the local MCP process and the ADK runtime?
- Which durable submission fields should be written to the job tracker after confirmation?
- For scalar salary fields, use the already-approved private salary rule; for ranges, currency/period mismatches, or compensation components the rule does not directly answer, ask rather than fabricate a value.

## Survey reference

The initial ten-form question survey, selection criteria, and visibility limitations are in [application-form-survey.md](application-form-survey.md). It is design research, not a guarantee that a live form has not changed.
