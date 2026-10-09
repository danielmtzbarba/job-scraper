-- Apply as the table-owning Cloud SQL IAM user before deploying code
-- that writes application audit events. No foreign key to attempts: discard must
-- preserve the content-free timeline after private artifacts and attempt rows go.
CREATE TABLE IF NOT EXISTS application_audit_events (
    id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL,
    source TEXT NOT NULL,
    deduplication_key TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    event_type TEXT NOT NULL,
    outcome TEXT NOT NULL,
    actor_kind TEXT NOT NULL,
    request_id TEXT,
    from_status TEXT,
    to_status TEXT,
    reason_code TEXT,
    action_kind TEXT,
    target_id TEXT,
    review_version INTEGER,
    model_id TEXT,
    prompt_tokens INTEGER,
    candidate_tokens INTEGER
);
CREATE INDEX IF NOT EXISTS idx_application_audit_attempt
    ON application_audit_events (attempt_id, occurred_at, id);
GRANT SELECT, INSERT ON application_audit_events
    TO "job-scraper-run@jobsearch-danielmtz-2026.iam";
