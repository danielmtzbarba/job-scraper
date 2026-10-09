-- Apply as the table-owning Cloud SQL IAM user before deploying submitted history.
ALTER TABLE application_attempts ADD COLUMN IF NOT EXISTS submitted_at TEXT;
ALTER TABLE application_attempts DROP CONSTRAINT IF EXISTS application_attempts_status_check;
ALTER TABLE application_attempts ADD CONSTRAINT application_attempts_status_check
    CHECK (status IN ('Selected', 'Inspecting', 'Draft', 'NeedsInput',
                     'ReadyForReview', 'Submitting', 'SubmissionUnverified', 'Submitted'));
