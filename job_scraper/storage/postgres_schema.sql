-- PostgreSQL schema for the job tracker. All timestamps remain ISO-8601 text for parity with SQLite.
CREATE TABLE IF NOT EXISTS jobs (
    source TEXT NOT NULL,
    source_job_id TEXT,
    deduplication_key TEXT NOT NULL,
    title TEXT,
    company TEXT,
    job_url TEXT,
    job_description TEXT,
    location TEXT,
    work_mode TEXT,
    employment_type TEXT,
    seniority TEXT,
    role_matches TEXT NOT NULL DEFAULT '[]',
    application_url TEXT,
    application_status TEXT NOT NULL DEFAULT 'Saved',
    application_notes TEXT,
    skill_stack_fit DOUBLE PRECISION,
    semantic_experience_fit DOUBLE PRECISION,
    overall_fit DOUBLE PRECISION,
    fit_category TEXT,
    fit_explanation TEXT,
    fit_status TEXT NOT NULL DEFAULT 'Pending',
    search_run_id TEXT,
    posted_at TEXT,
    PRIMARY KEY (source, deduplication_key)
);
CREATE TABLE IF NOT EXISTS job_processing (
    source TEXT NOT NULL,
    deduplication_key TEXT NOT NULL,
    source_job_id TEXT,
    payload_json TEXT NOT NULL,
    processing_status TEXT NOT NULL DEFAULT 'Pending',
    fetch_attempts INTEGER NOT NULL DEFAULT 0,
    fetch_error TEXT,
    fetch_next_retry_at TEXT,
    scoring_claimed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (source, deduplication_key)
);
CREATE TABLE IF NOT EXISTS fit_assessment_provenance (
    source TEXT NOT NULL,
    deduplication_key TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    profile_version INTEGER NOT NULL,
    rubric_version TEXT,
    prompt_version TEXT,
    evaluation_run_id TEXT,
    assessed_at TEXT NOT NULL,
    PRIMARY KEY (source, deduplication_key)
);
CREATE TABLE IF NOT EXISTS source_aliases (
    source TEXT NOT NULL,
    deduplication_key TEXT NOT NULL,
    canonical_source TEXT NOT NULL,
    canonical_key TEXT NOT NULL,
    PRIMARY KEY (source, deduplication_key)
);
CREATE TABLE IF NOT EXISTS possible_duplicates (
    source TEXT NOT NULL,
    deduplication_key TEXT NOT NULL,
    possible_source TEXT NOT NULL,
    possible_key TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (source, deduplication_key, possible_source, possible_key)
);
CREATE TABLE IF NOT EXISTS search_runs (
    id TEXT PRIMARY KEY,
    search_id TEXT NOT NULL,
    slot_date TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    found INTEGER NOT NULL DEFAULT 0,
    inserted INTEGER NOT NULL DEFAULT 0,
    duplicate INTEGER NOT NULL DEFAULT 0,
    skipped INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    UNIQUE (search_id, slot_date)
);
CREATE TABLE IF NOT EXISTS evaluation_runs (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL,
    source TEXT NOT NULL,
    deduplication_key TEXT NOT NULL,
    stage TEXT NOT NULL CHECK (stage IN ('classification', 'scoring')),
    status TEXT NOT NULL CHECK (status IN ('Running', 'Completed', 'Failed')),
    model_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    location TEXT NOT NULL,
    classifier_prompt_version TEXT,
    profile_id TEXT,
    profile_version INTEGER,
    rubric_version TEXT,
    prompt_version TEXT,
    input_price_per_million DOUBLE PRECISION NOT NULL,
    output_price_per_million DOUBLE PRECISION NOT NULL,
    prompt_tokens INTEGER,
    candidate_tokens INTEGER,
    thought_tokens INTEGER,
    estimated_cost_usd DOUBLE PRECISION,
    result_json TEXT,
    error_type TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS job_classifications (
    source TEXT NOT NULL,
    deduplication_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN
        ('Pending', 'Running', 'Classified', 'OutOfScope', 'Failed')),
    profile_id TEXT,
    profile_version INTEGER,
    reason TEXT,
    classifier_prompt_version TEXT,
    evaluation_run_id TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    claimed_at TEXT,
    next_retry_at TEXT,
    error_type TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (source, deduplication_key)
);
CREATE TABLE IF NOT EXISTS application_attempts (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    deduplication_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN
        ('Selected', 'Inspecting', 'Draft', 'NeedsInput',
         'ReadyForReview', 'Submitting', 'SubmissionUnverified', 'Submitted')),
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
    submitted_at TEXT,
    UNIQUE (source, deduplication_key),
    FOREIGN KEY (source, deduplication_key)
        REFERENCES jobs (source, deduplication_key)
);
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

CREATE INDEX IF NOT EXISTS idx_job_processing_queue
    ON job_processing (source, processing_status, fetch_next_retry_at);
CREATE INDEX IF NOT EXISTS idx_jobs_source_job_id
    ON jobs (source, source_job_id);
CREATE INDEX IF NOT EXISTS idx_jobs_company_title
    ON jobs (company, title);
CREATE INDEX IF NOT EXISTS idx_evaluation_runs_job
    ON evaluation_runs (source, deduplication_key, started_at);
CREATE INDEX IF NOT EXISTS idx_evaluation_runs_batch
    ON evaluation_runs (batch_id);
CREATE INDEX IF NOT EXISTS idx_job_classifications_queue
    ON job_classifications (status, next_retry_at, created_at);
