# Personal UK International Graduate Job Intelligence Platform PRD

## Purpose

Build a personal job-search intelligence system for a 2026 international graduate targeting UK technical roles. The MVP should help decide which roles to apply for first, why they are promising or risky, and which CV/project evidence should be emphasized.

## Phase 1 Scope

Implemented foundation:

- candidate evidence model with provenance
- project-first capability repository
- multi-track role taxonomy
- canonical job model
- deterministic initial job enrichment
- configurable ranking preferences
- explainable candidate/job matcher
- realistic job fixtures
- PostgreSQL-ready schema contract
- regression tests

Not in Phase 1:

- dashboard UI
- live source ingestion
- LinkedIn/Indeed scraping
- Spark or Airflow execution
- CV rewriting
- application submission automation

## Core Principles

- Broad discovery, personalized ranking.
- Project evidence and GitHub/README evidence are preserved separately from CV wording.
- Jobs may match several tracks at once.
- Sponsorship unknown is not treated as no sponsorship.
- Graduation-year friction downranks by default rather than deleting jobs.
- London is preferred, not permanently hard-filtered.

