-- Apply as the table-owning Cloud SQL IAM user before enabling application tools remotely.
-- This migration is intentionally separate from the runtime service startup.
CREATE TABLE IF NOT EXISTS application_attempts (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    deduplication_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN
        ('Selected', 'Inspecting', 'Draft', 'NeedsInput',
         'ReadyForReview', 'Submitting', 'SubmissionUnverified')),
    profile_id TEXT NOT NULL,
    profile_version INTEGER NOT NULL,
    cv_variant TEXT,
    artifact_ref TEXT NOT NULL,
    review_version INTEGER NOT NULL DEFAULT 0,
    review_digest TEXT,
    approved_at TEXT,
    submit_started_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (source, deduplication_key),
    FOREIGN KEY (source, deduplication_key)
        REFERENCES jobs (source, deduplication_key)
);

GRANT SELECT, INSERT, UPDATE, DELETE ON application_attempts
    TO "job-scraper-run@jobsearch-danielmtz-2026.iam";
