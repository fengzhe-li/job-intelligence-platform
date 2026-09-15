CREATE TABLE candidate_profiles (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    graduation_year INTEGER NOT NULL
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
    role_relevance JSONB NOT NULL DEFAULT '{}',
    UNIQUE (candidate_id, lower(name))
);

CREATE TABLE capability_evidence (
    id BIGSERIAL PRIMARY KEY,
    capability_id BIGINT NOT NULL REFERENCES capabilities(id),
    evidence_source_id TEXT NOT NULL REFERENCES evidence_sources(id),
    project_id TEXT,
    quote TEXT NOT NULL,
    confidence NUMERIC(4, 3) NOT NULL
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

CREATE INDEX idx_jobs_company_title ON jobs (company, title);
CREATE INDEX idx_source_observations_seen ON source_observations (last_seen_at);
CREATE INDEX idx_job_locations_country_city ON job_locations (country, city);
CREATE INDEX idx_raw_job_snapshots_source_seen ON raw_job_snapshots (source_name, observed_at);
