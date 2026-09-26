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

## Contact/Identity Fields

`CandidateProfile.email`, `.phone`, `.location`, `.linkedin_url`, `.github_url` are set only via `profile_ingestion.set_contact_details` (CLI: `profile-set-contact`), stored in `data/local/profile/contact.json` (git-ignored), and merged into the profile on every `profile-rebuild`. They are never inferred or fabricated -- a field left unset stays `None`, and `email`/`phone` being unset is treated as a required-field gap by the CV generation review gate (see [CV_GENERATION.md](CV_GENERATION.md)).

## Evidence Tier Calibration

`Capability.confidence` rewards corroboration from multiple independent sources (it does not simply average per-evidence confidence), so a single piece of evidence caps out well below 1.0 by design. `Capability.evidence_tier` (`strong`/`partial`/`missing`) is calibrated against that realistic range, not an arbitrary 0-1 split: a single confident (1.0), direct piece of evidence -- the common case, one README/manual/GitHub-sync quote -- lands at "strong"; a single weaker-confidence or indirect mention (e.g. a GitHub language-breakdown share, an education/experience-description mention) lands at "partial". See `Capability.evidence_tier`'s docstring for the exact thresholds.

