CREATE TABLE candidate_profiles (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    graduation_year INTEGER NOT NULL,
    -- Never fabricated -- remain NULL until explicitly set (profile-set-contact).
    email TEXT,
    phone TEXT,
    location TEXT,
    linkedin_url TEXT,
    github_url TEXT
);

CREATE TABLE evidence_sources (
    id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL,
    title TEXT NOT NULL,
    uri TEXT,
    collected_at DATE
);

CREATE TABLE capabilities (
    id BIGSERIAL PRIMARY KEY,
    candidate_id TEXT NOT NULL REFERENCES candidate_profiles(id),
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    technology TEXT,
    domain TEXT,
    last_verified DATE,
    role_relevance JSONB NOT NULL DEFAULT '{}',
    UNIQUE (candidate_id, lower(name))
);

CREATE TABLE projects (
    id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL REFERENCES candidate_profiles(id),
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    repository_full_name TEXT,
    role_relevance JSONB NOT NULL DEFAULT '{}'
);

CREATE TABLE capability_evidence (
    id BIGSERIAL PRIMARY KEY,
    capability_id BIGINT NOT NULL REFERENCES capabilities(id),
    evidence_source_id TEXT NOT NULL REFERENCES evidence_sources(id),
    project_id TEXT REFERENCES projects(id),
    quote TEXT NOT NULL,
    confidence NUMERIC(4, 3) NOT NULL
);

-- Per-repository GitHub sync state (mirrors storage/repository_store.py). Not
-- required for evidence to exist -- capabilities/projects sourced from a repo are
-- already captured above via evidence_sources/capability_evidence/projects. This
-- table only tracks *sync bookkeeping* (what's been fetched, so re-syncs are
-- incremental), same purpose as the local repositories.json cache.
CREATE TABLE repository_sync_state (
    full_name TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL REFERENCES candidate_profiles(id),
    last_synced_sha TEXT,
    last_synced_at TIMESTAMPTZ NOT NULL,
    evidence_source_id TEXT REFERENCES evidence_sources(id)
);

CREATE TABLE jobs (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    company TEXT NOT NULL,
    description TEXT NOT NULL,
    salary TEXT,
    raw_location TEXT,
    workflow_status TEXT NOT NULL DEFAULT 'new',
    role_track_profile JSONB NOT NULL DEFAULT '[]',
    skill_requirements JSONB NOT NULL DEFAULT '[]',
    sponsorship JSONB,
    graduation_year JSONB,
    eligibility JSONB NOT NULL DEFAULT '[]'
);

CREATE TABLE job_locations (
    id BIGSERIAL PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    city TEXT,
    country TEXT NOT NULL,
    region TEXT,
    work_mode TEXT NOT NULL,
    raw TEXT
);

CREATE TABLE source_observations (
    id BIGSERIAL PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    source_name TEXT NOT NULL,
    source_job_id TEXT NOT NULL,
    original_url TEXT NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL,
    posted_at TIMESTAMPTZ,
    raw_description TEXT NOT NULL,
    raw_payload JSONB NOT NULL DEFAULT '{}',
    canonical_application_url TEXT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    latest_observed_state TEXT NOT NULL DEFAULT 'active',
    -- Only set when this specific source explicitly stated a deadline -- never
    -- inferred. Kept per-observation (not on jobs) so two sources disagreeing on
    -- deadline for the same vacancy are both preserved, not silently resolved.
    deadline TIMESTAMPTZ,
    UNIQUE (source_name, source_job_id)
);

CREATE TABLE raw_job_snapshots (
    id BIGSERIAL PRIMARY KEY,
    source_name TEXT NOT NULL,
    source_job_id TEXT NOT NULL,
    source_url TEXT NOT NULL,
    canonical_application_url TEXT NOT NULL,
    raw_payload JSONB NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    posted_at TIMESTAMPTZ,
    first_seen_at TIMESTAMPTZ,
    last_seen_at TIMESTAMPTZ
);

-- Application lifecycle tracker (mirrors models/application.py + storage/application_store.py).
CREATE TABLE applications (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    company TEXT NOT NULL,
    role_title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'discovered',
    sources JSONB NOT NULL DEFAULT '[]',
    jd_snapshot TEXT NOT NULL DEFAULT '',
    discovered_at TIMESTAMPTZ,
    deadline TIMESTAMPTZ,
    applied_at TIMESTAMPTZ,
    next_action TEXT,
    next_action_deadline TIMESTAMPTZ,
    notes TEXT NOT NULL DEFAULT '',
    application_url TEXT,
    cv_version_id TEXT,
    selected_project_ids JSONB NOT NULL DEFAULT '[]',
    evidence_ids_used JSONB NOT NULL DEFAULT '[]',
    updated_at TIMESTAMPTZ
);

-- Append-only: a row is written on every status transition and never updated or
-- deleted, so "what status was this application in on a given date" stays answerable.
CREATE TABLE application_status_history (
    id BIGSERIAL PRIMARY KEY,
    application_id TEXT NOT NULL REFERENCES applications(id),
    from_status TEXT,
    to_status TEXT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);

-- Append-only: every `generate_cv` call is a new, immutable row -- never updated or
-- deleted -- so this can answer "what exact CV did I submit to this company?" even
-- after the Evidence Bank or ranking config later changes. Mirrors
-- storage/cv_artifact_store.py's artifacts.jsonl.
CREATE TABLE cv_artifacts (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    candidate_id TEXT NOT NULL REFERENCES candidate_profiles(id),
    application_id TEXT REFERENCES applications(id),
    generated_at TIMESTAMPTZ NOT NULL,
    jd_snapshot TEXT NOT NULL,
    selected_project_ids JSONB NOT NULL DEFAULT '[]',
    evidence_ids_used JSONB NOT NULL DEFAULT '[]',
    bullets JSONB NOT NULL DEFAULT '[]',
    skills_included JSONB NOT NULL DEFAULT '[]',
    cv_text TEXT NOT NULL,
    pdf_path TEXT,
    page_count INTEGER NOT NULL,
    fits_one_page BOOLEAN NOT NULL,
    render_margin_mm NUMERIC(5, 2) NOT NULL,
    render_body_pt NUMERIC(4, 2) NOT NULL,
    review_status TEXT NOT NULL,
    review_reasons JSONB NOT NULL DEFAULT '[]'
);

CREATE INDEX idx_jobs_company_title ON jobs (company, title);
CREATE INDEX idx_source_observations_seen ON source_observations (last_seen_at);
CREATE INDEX idx_job_locations_country_city ON job_locations (country, city);
CREATE INDEX idx_raw_job_snapshots_source_seen ON raw_job_snapshots (source_name, observed_at);
CREATE INDEX idx_projects_candidate ON projects (candidate_id);
CREATE INDEX idx_applications_status ON applications (status);
CREATE INDEX idx_application_status_history_application ON application_status_history (application_id, occurred_at);
CREATE INDEX idx_cv_artifacts_job ON cv_artifacts (job_id);
CREATE INDEX idx_cv_artifacts_application ON cv_artifacts (application_id);
