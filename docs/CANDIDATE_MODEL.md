# Candidate Model

## Entities

- `CandidateProfile`
- `Capability`
- `Skill`
- `Technology`
- `Project`
- `Education`
- `Experience`
- `CVVersion`
- `EvidenceSource`
- `CapabilityEvidence`

## Provenance

Every capability retains evidence:

- source type
- source title
- optional URI
- quote
- confidence
- optional project ID

Example:

```text
Capability: PostgreSQL
Evidence source: Financial Knowledge Intelligence Platform
Quote: Implemented PostgreSQL persistence with Alembic migrations.
```

## Evidence Priority

CV versions are useful, but they are role-biased. Project descriptions, GitHub README exports, manual project notes, education, and experience descriptions are preserved independently so matching can use real technical evidence instead of only CV phrasing.

