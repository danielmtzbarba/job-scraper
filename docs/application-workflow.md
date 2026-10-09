# Job application workflow

Status: MCP workflow and `/applications/{attempt_id}` review routes deployed to Cloud Run on 2026-10-09. The PostgreSQL attempt-table migration is applied. Cloud Run attempt creation remains disabled until private artifact storage is persistent. No real application has been submitted with this workflow.

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
9. **Submit and verify.** After approval, persist the approved digest and `Submitting` state before the browser click. Submit once, inspect the confirmation/result, and update the job's application status only when submission is confirmed. The current durable outcome is `jobs.application_status = Applied`; submission time, confirmation reference, and CV variant are not yet stored after closure.
10. **Close and erase the attempt.** On explicit **Submitted** or **Discarded**, delete that attempt's temporary files and state. A timeout, browser failure, unclear answer, or waiting state is not an explicit discard and does not authorize deletion. If submission fails, ask whether to retry or discard; keep the attempt until the user decides.

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

The shared Python implementation is `job_scraper.application.application_workflow.ApplicationWorkflow`; MCP tools adapt its readiness, attempt, inspect, answer, review, submit, and discard operations. MCP tools and a future ADK adapter should call the same module so classification checks, consent gates, retention, and submit confirmation cannot diverge by client. Playwright is the initial browser driver. The current implementation inspects only the visible page and does not advance multi-step forms. The code is deployed, but Cloud Run attempt creation is disabled and it has not been run against a real application.

The implementation records active process state, profile and CV variant, review version and digest, and approval timestamps in `application_attempts`. SQLite creates this table locally; [the migration](../deploy/migrations/2026-10-09-application-attempts.sql) was applied to Cloud SQL on 2026-10-09. The table enforces one active attempt per job. The review page and MCP tools call the same module. Final approval atomically claims the persisted review digest before the browser click. A possible click without verified confirmation leaves `SubmissionUnverified`, which cannot be retried or edited. Confirming submission sets the job outcome to `Applied` and deletes the attempt row; discard deletes the row without changing the job outcome. Both closure paths erase the private artifacts.

The general MCP `update_application_status` tool refuses `Applied`; that transition goes through the shared workflow's confirmation path. The `/jobs` Application column remains the job outcome and offers **Start or continue** for eligible rows. The review page shows the captured form, exact proposed answers, CV filename, employer-specific consent choices, and explicit submit/discard actions. Responses containing attempt data have `Cache-Control: no-store`.

MCP tools currently include `check_application_readiness`, `start_application_attempt`, `inspect_application_form`, `get_application_facts`, `add_approved_application_fact`, `save_application_answers`, `prepare_application_review`, `submit_application`, `confirm_application_submitted`, and `discard_application_attempt`. The caller must use only approved facts and ask the user about unclear fields and employer-specific consent. `add_approved_application_fact` appends a new key and will not overwrite an existing fact. Selecting a talent pool or group-sharing option requires a separate approved selector in the pilot.

Known implementation limits: form inspection does not advance to later steps; automated filling currently supports inspected CSS-addressable controls and the first file input; no live form has been exercised in this implementation pass. Cloud Run attempts fail closed because `.local` artifacts would be lost on instance replacement. Before remote use, select persistent private artifact storage and map the prepared PDFs. The browser route is deployed, but an attempt cannot start there yet. Verify end-to-end browser behavior on a safe test form before a real pilot.

## Questions for later design

- Where are the prepared PDF variants currently stored, and what are their exact filenames/profile identifiers?
- What browser-control implementation will be available to both the local MCP process and the ADK runtime?
- Which durable submission fields should be written to the job tracker after confirmation?
- For scalar salary fields, use the already-approved private salary rule; for ranges, currency/period mismatches, or compensation components the rule does not directly answer, ask rather than fabricate a value.

## Survey reference

The initial ten-form question survey, selection criteria, and visibility limitations are in [application-form-survey.md](application-form-survey.md). It is design research, not a guarantee that a live form has not changed.
