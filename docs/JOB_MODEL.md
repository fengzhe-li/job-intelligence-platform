# Job Model

## Canonical Job

`Job` contains:

- title
- company
- raw description
- locations
- salary where present
- raw source location
- source observations
- role-track profile
- skill requirements
- sponsorship evidence
- graduation-year evidence
- eligibility evidence
- workflow status

## Source Observations

Each source observation retains:

- source name
- source job ID
- original URL
- first seen time
- last seen time
- posted time
- raw description
- raw payload
- canonical application URL

This allows deduplication later while preserving every observed source.

## Deduplication

Phase 2 deduplication uses:

1. exact source and ATS IDs
2. exact canonical application URL
3. company + normalized title + location
4. fuzzy title matching with same company and location

When sources disagree, direct company/ATS sources are preferred for the canonical application URL while all observations are retained.

## Evidence Preservation

Sponsorship, graduation-year, and eligibility conclusions store original English evidence text wherever a phrase was detected. Unknown sponsorship remains visible.
